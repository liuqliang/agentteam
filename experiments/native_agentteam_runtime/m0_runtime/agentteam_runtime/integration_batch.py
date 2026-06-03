import json
import subprocess
from pathlib import Path

from .integration_queue import read_integration_queue


INTEGRATION_BATCHES_SCHEMA_VERSION = "integration_batches.v1"
DEFAULT_BATCH_QUEUE_STATUSES = ("pending", "applied", "verified", "committed")


def integration_batches_path(output_dir):
    return Path(output_dir) / "state" / "integration_batches.json"


def read_integration_batches(output_dir):
    path = integration_batches_path(output_dir)
    if not path.exists():
        return _empty_registry()
    registry = json.loads(path.read_text(encoding="utf-8"))
    registry.setdefault("batch_schema_version", INTEGRATION_BATCHES_SCHEMA_VERSION)
    registry.setdefault("items", [])
    return registry


def verify_integration_batch(
    project_root,
    output_dir,
    batch_id,
    verification_command,
    queue_statuses=None,
):
    if not verification_command:
        raise ValueError("verification_command must not be empty")

    output_dir = Path(output_dir)
    selected_statuses = tuple(queue_statuses or DEFAULT_BATCH_QUEUE_STATUSES)
    queue_items = _selected_queue_items(output_dir, selected_statuses)
    base_result = _base_batch_result(
        project_root,
        output_dir,
        batch_id,
        verification_command,
        selected_statuses,
        queue_items,
    )
    if not queue_items:
        result = {
            **base_result,
            "batch_status": "empty",
            "verification_status": "not_requested",
        }
        upsert_integration_batch_result(output_dir, result)
        return result

    worktree_result = _create_batch_worktree(project_root, output_dir, batch_id)
    base_result.update(worktree_result)

    applied_item_ids = []
    for item in queue_items:
        apply_result = _apply_queue_patch(
            base_result["batch_worktree_path"],
            item,
        )
        if apply_result["patch_apply_status"] != "applied":
            result = {
                **base_result,
                **apply_result,
                "batch_status": "blocked",
                "applied_queue_item_ids": applied_item_ids,
                "verification_status": "not_requested",
            }
            upsert_integration_batch_result(output_dir, result)
            return result
        applied_item_ids.append(item["queue_item_id"])

    verification = _run_batch_verification(
        verification_command,
        base_result["batch_worktree_path"],
    )
    result = {
        **base_result,
        **verification,
        "batch_status": (
            "verified" if verification["verification_status"] == "passed" else "failed"
        ),
        "applied_queue_item_ids": applied_item_ids,
        "patch_apply_status": "applied",
        "failed_queue_item_id": None,
        "patch_apply_stdout": "",
        "patch_apply_stderr": "",
    }
    upsert_integration_batch_result(output_dir, result)
    return result


def upsert_integration_batch_result(output_dir, result):
    path = integration_batches_path(output_dir)
    registry = read_integration_batches(output_dir)
    items = [
        item
        for item in registry["items"]
        if item.get("batch_id") != result["batch_id"]
    ]
    items.append(result)
    registry["items"] = items
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "integration_batches_path": str(path),
        "batch_status": result["batch_status"],
    }


def _selected_queue_items(output_dir, queue_statuses):
    allowed = set(queue_statuses)
    queue = read_integration_queue(output_dir)
    return [
        item
        for item in queue["items"]
        if item.get("queue_status") in allowed and item.get("patch_path")
    ]


def _base_batch_result(
    project_root,
    output_dir,
    batch_id,
    verification_command,
    selected_statuses,
    queue_items,
):
    return {
        "batch_id": batch_id,
        "batch_status": "running",
        "project_root": str(project_root),
        "selected_queue_statuses": list(selected_statuses),
        "queue_item_ids": [item["queue_item_id"] for item in queue_items],
        "patch_paths": [item["patch_path"] for item in queue_items],
        "verification_command": list(verification_command),
        "batch_branch": None,
        "batch_worktree_path": None,
        "applied_queue_item_ids": [],
        "failed_queue_item_id": None,
        "patch_apply_status": "not_requested",
        "patch_apply_stdout": "",
        "patch_apply_stderr": "",
        "verification_status": "not_requested",
        "verification_exit_code": None,
        "verification_stdout": "",
        "verification_stderr": "",
    }


def _create_batch_worktree(project_root, output_dir, batch_id):
    safe_batch_id = str(batch_id).replace("/", "-")
    batch_branch = f"agentteam/integration-batch/{safe_batch_id}"
    batch_worktree = Path(output_dir) / "integration_batches" / safe_batch_id / "worktree"
    batch_worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "worktree",
            "add",
            "-b",
            batch_branch,
            str(batch_worktree),
            "HEAD",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "batch_branch": batch_branch,
        "batch_worktree_path": str(batch_worktree),
    }


def _apply_queue_patch(batch_worktree_path, item):
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(batch_worktree_path),
            "apply",
            item["patch_path"],
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "patch_apply_status": "applied" if completed.returncode == 0 else "failed",
        "failed_queue_item_id": (
            None if completed.returncode == 0 else item["queue_item_id"]
        ),
        "patch_apply_stdout": completed.stdout,
        "patch_apply_stderr": completed.stderr,
    }


def _run_batch_verification(command, batch_worktree_path):
    completed = subprocess.run(
        list(command),
        cwd=batch_worktree_path,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "verification_status": "passed" if completed.returncode == 0 else "failed",
        "verification_exit_code": completed.returncode,
        "verification_stdout": completed.stdout,
        "verification_stderr": completed.stderr,
    }


def _empty_registry():
    return {
        "batch_schema_version": INTEGRATION_BATCHES_SCHEMA_VERSION,
        "items": [],
    }

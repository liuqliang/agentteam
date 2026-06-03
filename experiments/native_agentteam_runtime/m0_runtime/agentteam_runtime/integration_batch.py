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
    merge_verified_batch=False,
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
    if merge_verified_batch:
        result = merge_verified_integration_batch(project_root, output_dir, batch_id)
    return result


def merge_verified_integration_batch(project_root, output_dir, batch_id):
    registry = read_integration_batches(output_dir)
    batch = _batch_by_id(registry, batch_id)
    if not batch:
        raise ValueError(f"integration batch not found: {batch_id}")
    merge = _merge_verified_batch(project_root, batch)
    result = {
        **batch,
        **merge,
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
        "batch_commit_status": "not_requested",
        "batch_commit_sha": None,
        "batch_commit_message": None,
        "batch_commit_reason": None,
        "batch_commit_stdout": "",
        "batch_commit_stderr": "",
        "merge_status": "not_requested",
        "merge_reason": None,
        "merge_target_branch": None,
        "merge_commit_sha": None,
        "source_head_before": None,
        "source_head_after": None,
        "merge_stdout": "",
        "merge_stderr": "",
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


def _merge_verified_batch(project_root, batch):
    if batch.get("batch_status") != "verified":
        return _merge_result("rejected", reason="batch_not_verified")
    project_root = Path(project_root)
    batch_worktree_path = batch.get("batch_worktree_path")
    if not batch_worktree_path:
        return _merge_result("rejected", reason="batch_worktree_missing")
    if _git_status_porcelain(project_root):
        return _merge_result("rejected", reason="source_dirty")

    source_head_before = _git_stdout(project_root, ["rev-parse", "HEAD"])
    source_branch = _git_stdout(project_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit = _commit_batch_worktree(batch_worktree_path, batch["batch_id"])
    if commit["batch_commit_status"] == "failed":
        return _merge_result(
            "failed",
            reason="batch_commit_failed",
            source_head_before=source_head_before,
            merge_target_branch=source_branch,
            **commit,
        )

    merge_ref = commit["batch_commit_sha"]
    completed = subprocess.run(
        ["git", "-C", str(project_root), "merge", "--ff-only", merge_ref],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    source_head_after = _git_stdout(project_root, ["rev-parse", "HEAD"])
    if completed.returncode != 0:
        return _merge_result(
            "failed",
            reason="git_merge_failed",
            source_head_before=source_head_before,
            source_head_after=source_head_after,
            merge_target_branch=source_branch,
            merge_commit_sha=merge_ref,
            merge_stdout=completed.stdout,
            merge_stderr=completed.stderr,
            **commit,
        )

    return _merge_result(
        "merged",
        source_head_before=source_head_before,
        source_head_after=source_head_after,
        merge_target_branch=source_branch,
        merge_commit_sha=merge_ref,
        merge_stdout=completed.stdout,
        merge_stderr=completed.stderr,
        **commit,
    )


def _commit_batch_worktree(batch_worktree_path, batch_id):
    batch_worktree_path = Path(batch_worktree_path)
    if not _git_status_porcelain(batch_worktree_path):
        sha = _git_stdout(batch_worktree_path, ["rev-parse", "HEAD"])
        return _batch_commit_result("skipped", sha=sha, reason="no_changes")

    message = f"AgentTeam batch integration {batch_id}"
    subprocess.run(
        ["git", "-C", str(batch_worktree_path), "add", "--all"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    completed = subprocess.run(
        ["git", "-C", str(batch_worktree_path), "commit", "-m", message],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return _batch_commit_result(
            "failed",
            reason="git_commit_failed",
            message=message,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    sha = _git_stdout(batch_worktree_path, ["rev-parse", "HEAD"])
    return _batch_commit_result(
        "committed",
        sha=sha,
        message=message,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _batch_by_id(registry, batch_id):
    for item in registry["items"]:
        if item.get("batch_id") == batch_id:
            return item
    return None


def _git_status_porcelain(repo):
    return _git_stdout(repo, ["status", "--porcelain=v1", "--untracked-files=all"])


def _git_stdout(repo, args):
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _batch_commit_result(
    status,
    sha=None,
    reason=None,
    message=None,
    stdout="",
    stderr="",
):
    return {
        "batch_commit_status": status,
        "batch_commit_sha": sha,
        "batch_commit_message": message,
        "batch_commit_reason": reason,
        "batch_commit_stdout": stdout,
        "batch_commit_stderr": stderr,
    }


def _merge_result(
    status,
    reason=None,
    merge_target_branch=None,
    merge_commit_sha=None,
    source_head_before=None,
    source_head_after=None,
    merge_stdout="",
    merge_stderr="",
    batch_commit_status="not_requested",
    batch_commit_sha=None,
    batch_commit_message=None,
    batch_commit_reason=None,
    batch_commit_stdout="",
    batch_commit_stderr="",
):
    return {
        "merge_status": status,
        "merge_reason": reason,
        "merge_target_branch": merge_target_branch,
        "merge_commit_sha": merge_commit_sha,
        "source_head_before": source_head_before,
        "source_head_after": source_head_after,
        "merge_stdout": merge_stdout,
        "merge_stderr": merge_stderr,
        "batch_commit_status": batch_commit_status,
        "batch_commit_sha": batch_commit_sha,
        "batch_commit_message": batch_commit_message,
        "batch_commit_reason": batch_commit_reason,
        "batch_commit_stdout": batch_commit_stdout,
        "batch_commit_stderr": batch_commit_stderr,
    }


def _empty_registry():
    return {
        "batch_schema_version": INTEGRATION_BATCHES_SCHEMA_VERSION,
        "items": [],
    }

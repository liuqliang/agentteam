import json
from pathlib import Path


INTEGRATION_QUEUE_SCHEMA_VERSION = "integration_queue.v1"


def integration_queue_path(output_dir):
    return Path(output_dir) / "state" / "integration_queue.json"


def read_integration_queue(output_dir):
    path = integration_queue_path(output_dir)
    if not path.exists():
        return _empty_queue()
    queue = json.loads(path.read_text(encoding="utf-8"))
    queue.setdefault("queue_schema_version", INTEGRATION_QUEUE_SCHEMA_VERSION)
    queue.setdefault("items", [])
    return queue


def upsert_integration_queue_item(output_dir, attempt):
    path = integration_queue_path(output_dir)
    if not _attempt_should_be_queued(attempt):
        return {
            "integration_queue_status": "not_queued",
            "integration_queue_item_id": None,
            "integration_queue_path": str(path),
        }

    item = build_integration_queue_item(attempt)
    queue = read_integration_queue(output_dir)
    items = [
        existing
        for existing in queue["items"]
        if existing.get("queue_item_id") != item["queue_item_id"]
    ]
    items.append(item)
    queue["items"] = items
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "integration_queue_status": item["queue_status"],
        "integration_queue_item_id": item["queue_item_id"],
        "integration_queue_path": str(path),
    }


def build_integration_queue_item(attempt):
    queue_item_id = f"{attempt['task_id']}:{attempt['attempt_id']}"
    return {
        "queue_item_id": queue_item_id,
        "queue_status": integration_queue_status_for_attempt(attempt),
        "task_id": attempt["task_id"],
        "attempt_id": attempt["attempt_id"],
        "lease_id": attempt.get("lease_id"),
        "patch_path": attempt.get("patch_path"),
        "attempt_branch": attempt.get("branch"),
        "attempt_worktree_path": attempt.get("worktree_path"),
        "integration_status": attempt.get("integration_status", "not_requested"),
        "integration_branch": attempt.get("integration_branch"),
        "integration_worktree_path": attempt.get("integration_worktree_path"),
        "integration_verification_status": attempt.get(
            "integration_verification_status",
            "not_requested",
        ),
        "integration_verification_exit_code": attempt.get(
            "integration_verification_exit_code"
        ),
        "integration_commit_status": attempt.get(
            "integration_commit_status",
            "not_requested",
        ),
        "integration_commit_sha": attempt.get("integration_commit_sha"),
        "integration_commit_reason": attempt.get("integration_commit_reason"),
        "integration_base_ref": attempt.get("integration_base_ref"),
        "integration_base_sha": attempt.get("integration_base_sha"),
        "integration_baseline_branch": attempt.get("integration_baseline_branch"),
        "integration_baseline_worktree_path": attempt.get(
            "integration_baseline_worktree_path"
        ),
        "integration_baseline_commit_status": attempt.get(
            "integration_baseline_commit_status",
            "not_requested",
        ),
        "integration_baseline_commit_sha": attempt.get(
            "integration_baseline_commit_sha"
        ),
        "integration_baseline_commit_reason": attempt.get(
            "integration_baseline_commit_reason"
        ),
    }


def integration_queue_status_for_attempt(attempt):
    commit_status = attempt.get("integration_commit_status", "not_requested")
    verification_status = attempt.get("integration_verification_status", "not_requested")
    integration_status = attempt.get("integration_status", "not_requested")

    if commit_status == "committed":
        return "committed"
    if commit_status == "failed" or verification_status == "failed":
        return "blocked"
    if verification_status == "passed":
        return "verified"
    if integration_status == "applied":
        return "applied"
    return "pending"


def _attempt_should_be_queued(attempt):
    return attempt.get("validation_status") == "accepted" and bool(attempt.get("patch_path"))


def _empty_queue():
    return {
        "queue_schema_version": INTEGRATION_QUEUE_SCHEMA_VERSION,
        "items": [],
    }

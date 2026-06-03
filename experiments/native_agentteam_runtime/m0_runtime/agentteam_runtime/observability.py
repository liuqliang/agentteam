import json
from collections import Counter
from pathlib import Path

from .integration_queue import read_integration_queue
from .m0_runtime import read_scheduler_state_index, replay_events


OBSERVABILITY_VIEWS = {
    "summary",
    "backlog",
    "leases",
    "events",
    "sessions",
    "workers",
    "integration-queue",
}


def build_runtime_observability(output_dir, view="summary"):
    if view not in OBSERVABILITY_VIEWS:
        raise ValueError(f"unknown observability view: {view}")
    output_dir = Path(output_dir)
    state_index = read_scheduler_state_index(output_dir)
    events_path = output_dir / "events.jsonl"
    snapshot = replay_events(events_path)
    integration_queue = read_integration_queue(output_dir)
    worker_registry = _read_worker_registry(output_dir)

    base = {
        "observability_status": "ready",
        "view": view,
        "output_dir": str(output_dir),
        "events_path": str(events_path),
        "state_db_path": state_index["state_db_path"],
        "event_count": state_index["event_count"],
        "latest_event": state_index["latest_event"],
    }
    if view == "backlog":
        return {**base, "tasks": state_index["tasks"]}
    if view == "leases":
        return {**base, "leases": state_index["leases"]}
    if view == "events":
        return {**base, "events": _read_events(events_path)}
    if view == "sessions":
        return {**base, "runtime_sessions": state_index["runtime_sessions"]}
    if view == "workers":
        return {**base, "workers": worker_registry.get("workers", [])}
    if view == "integration-queue":
        return {
            **base,
            "integration_queue": integration_queue,
            "integration_queue_items": integration_queue["items"],
        }

    return {
        **base,
        "task_counts": _count_values(snapshot["tasks"].values(), "task_status"),
        "attempt_counts": _count_values(
            snapshot["attempts"].values(),
            "attempt_status",
        ),
        "lease_counts": _count_values(snapshot["leases"].values(), "lease_status"),
        "runtime_session_counts": _count_values(
            snapshot["runtime_sessions"].values(),
            "session_status",
        ),
        "integration_queue_counts": _count_values(
            integration_queue["items"],
            "queue_status",
        ),
        "worker_counts": _count_values(
            worker_registry.get("workers", []),
            "worker_status",
        ),
        "blocked_task_ids": _task_ids_by_status(snapshot, "blocked"),
        "latest_failures": _latest_failures(snapshot),
    }


def _count_values(items, key):
    return dict(sorted(Counter(item.get(key) or "unknown" for item in items).items()))


def _task_ids_by_status(snapshot, status):
    return sorted(
        task_id
        for task_id, task in snapshot["tasks"].items()
        if task.get("task_status") == status
    )


def _latest_failures(snapshot, limit=5):
    failures = []
    for attempt_id, attempt in snapshot["attempts"].items():
        failure_category = attempt.get("failure_category")
        validation_status = attempt.get("validation_status")
        attempt_status = attempt.get("attempt_status")
        if not (
            failure_category
            or validation_status == "rejected"
            or attempt_status in {"failed", "timed_out", "cancelled"}
        ):
            continue
        failures.append(
            {
                "attempt_id": attempt_id,
                "task_id": attempt.get("task_id"),
                "attempt_status": attempt_status,
                "validation_status": validation_status,
                "failure_category": failure_category,
            }
        )
    return failures[-limit:]


def _read_events(events_path):
    return [
        json.loads(line)
        for line in Path(events_path).read_text(encoding="utf-8").splitlines()
    ]


def _read_worker_registry(output_dir):
    for path in [
        output_dir / "state" / "worker_process_registry.json",
        output_dir / "state" / "worker_registry.json",
    ]:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {"workers": []}

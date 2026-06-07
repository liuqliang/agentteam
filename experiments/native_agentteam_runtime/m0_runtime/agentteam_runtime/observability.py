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
    "repo-contexts",
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
    scheduler_state = _read_scheduler_state(output_dir)
    current_milestone = _current_milestone(scheduler_state)

    base = {
        "observability_status": "ready",
        "view": view,
        "output_dir": str(output_dir),
        "events_path": str(events_path),
        "state_db_path": state_index["state_db_path"],
        "event_count": state_index["event_count"],
        "latest_event": state_index["latest_event"],
        "current_milestone": current_milestone,
        "next_decomposition": _next_decomposition(
            scheduler_state,
            current_milestone,
        ),
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
    if view == "repo-contexts":
        repo_contexts = _read_repo_contexts(
            output_dir,
            state_index["attempts"],
            snapshot["attempts"],
        )
        return {
            **base,
            "repo_context_count": len(repo_contexts),
            "repo_contexts": repo_contexts,
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
        "runtime_profile_source_counts": _count_values(
            snapshot["runtime_sessions"].values(),
            "runtime_profile_source",
        ),
        "integration_queue_counts": _count_values(
            integration_queue["items"],
            "queue_status",
        ),
        "manual_gate_counts": _count_values(
            snapshot.get("manual_gates", {}).values(),
            "gate_status",
        ),
        "permission_request_counts": _count_values(
            snapshot.get("permission_requests", {}).values(),
            "request_status",
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


def _read_repo_contexts(output_dir, attempts=None, replay_attempts=None):
    repo_context_dir = Path(output_dir) / "repo_contexts"
    if not repo_context_dir.exists():
        return []
    attempt_by_context_path = {
        attempt.get("repo_context_path"): attempt
        for attempt in attempts or []
        if attempt.get("repo_context_path")
    }
    replay_attempts = replay_attempts or {}
    contexts = []
    for path in sorted(repo_context_dir.glob("*.json")):
        try:
            context = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            contexts.append({"path": str(path), "read_status": "invalid_json"})
            continue
        selected_files = context.get("selected_files", [])
        candidate_tests = context.get("candidate_tests", [])
        attempt = attempt_by_context_path.get(str(path), {})
        replay_attempt = replay_attempts.get(attempt.get("attempt_id"), {})
        hit_metrics = _repo_context_hit_metrics(selected_files, replay_attempt)
        contexts.append(
            {
                "path": str(path),
                "read_status": "ok",
                "repo_context_schema_version": context.get(
                    "repo_context_schema_version"
                ),
                "attempt_id": attempt.get("attempt_id"),
                "task_id": context.get("task_id"),
                "agent_role": context.get("agent_role"),
                "selected_file_count": len(selected_files),
                "selected_files": [
                    {
                        "path": selected_file.get("path"),
                        "language": selected_file.get("language"),
                        "category": selected_file.get("category"),
                        "selection_reasons": selected_file.get(
                            "selection_reasons",
                            [],
                        ),
                    }
                    for selected_file in selected_files
                ],
                "candidate_test_count": len(candidate_tests),
                "candidate_tests": [
                    {
                        "path": candidate_test.get("path"),
                        "language": candidate_test.get("language"),
                        "selection_reasons": candidate_test.get(
                            "selection_reasons",
                            [],
                        ),
                    }
                    for candidate_test in candidate_tests
                ],
                "omitted_file_count": context.get("omitted_file_count", 0),
                "repo_map_manifest_path": context.get("repo_map_manifest_path"),
                "warning_count": len(context.get("warnings", [])),
                **hit_metrics,
            }
        )
    return contexts


def _repo_context_hit_metrics(selected_files, attempt):
    selected_paths = {
        selected_file.get("path")
        for selected_file in selected_files
        if selected_file.get("path")
    }
    diff_audit = attempt.get("diff_audit") or {}
    actual_changed_files = sorted(diff_audit.get("actual_changed_files") or [])
    changed_selected_files = [
        path for path in actual_changed_files if path in selected_paths
    ]
    changed_unselected_files = [
        path for path in actual_changed_files if path not in selected_paths
    ]
    hit_rate = (
        len(changed_selected_files) / len(actual_changed_files)
        if actual_changed_files
        else None
    )
    return {
        "actual_changed_file_count": len(actual_changed_files),
        "changed_selected_file_count": len(changed_selected_files),
        "changed_selected_files": changed_selected_files,
        "changed_unselected_files": changed_unselected_files,
        "selected_file_hit_rate": hit_rate,
    }


def _read_scheduler_state(output_dir):
    path = output_dir / "state" / "two_phase_scheduler_state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _current_milestone(scheduler_state):
    milestones = list(scheduler_state.get("milestones", {}).values())
    if not milestones:
        return None
    return sorted(milestones, key=_milestone_sort_key)[0]


def _milestone_sort_key(milestone):
    status = milestone.get("milestone_status")
    if status == "active":
        rank = 0
    elif status in {"blocked", "max_waves_reached"}:
        rank = 1
    else:
        rank = 2
    return (rank, milestone.get("milestone_id", ""))


def _next_decomposition(scheduler_state, current_milestone):
    if not current_milestone:
        return None
    task_id = current_milestone.get("current_decomposition_task_id")
    if not task_id:
        return None
    for task in scheduler_state.get("backlog", {}).get("items", []):
        if task.get("task_id") != task_id:
            continue
        return {
            "task_id": task["task_id"],
            "task_status": task.get("backlog_status"),
            "milestone_id": task.get("milestone_id"),
            "decomposition_wave": task.get("decomposition_wave"),
            "required_role": task.get("required_role"),
            "planner_context_path": task.get("planner_context_path"),
        }
    return None

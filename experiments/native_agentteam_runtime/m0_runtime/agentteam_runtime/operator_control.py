import hashlib
import json
import os
import signal
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from .experiment_contract import canonical_json_sha256


_RUNNING_WORKER_STATUSES = {"running", "started", "idle", "busy"}
_RUNNING_SCHEDULER_STATUSES = {"running", "waiting", "max_ticks_reached"}
_EXPERIMENT_OPERATOR_EVENT_SOURCES = {
    "operator_answer_received": (
        "manual_gate",
        "manual_gate_required",
        True,
    ),
    "permission_request_resolved": (
        "permission_request",
        "permission_request_required",
        True,
    ),
    "run_stop_requested": ("run_stop", "run_stop_requested", False),
    "run_resume_requested": (
        "run_resume",
        "run_resume_requested",
        False,
    ),
    "corrective_guidance_received": (
        "corrective_guidance",
        "corrective_guidance_received",
        False,
    ),
}
_NON_OPERATOR_ACTION_EVENTS = {
    "status_inspected",
    "explain_status_inspected",
    "controller_tick",
    "scheduler_tick",
    "budget_warning",
    "budget_exhausted",
    "worker_dispatched",
    "worker_completed",
}
_EXPERIMENT_AUTHORIZATION_TOKEN = object()


class _ExperimentMutationAuthorization:
    __slots__ = (
        "_token",
        "action_id",
        "controller_reference",
        "identity_value",
        "experiment_run_id",
        "request_source",
        "target_path_sha256",
    )

    def __init__(
        self,
        token,
        *,
        action_id,
        controller_reference,
        identity_value,
        experiment_run_id,
        request_source,
        target_path_sha256,
    ):
        if token is not _EXPERIMENT_AUTHORIZATION_TOKEN:
            raise PermissionError(
                "experiment mutation authorization is controller-owned"
            )
        self._token = token
        self.action_id = action_id
        self.controller_reference = deepcopy(controller_reference)
        self.identity_value = identity_value
        self.experiment_run_id = experiment_run_id
        self.request_source = request_source
        self.target_path_sha256 = target_path_sha256


def validate_experiment_mutation_authorization(
    authorization,
    *,
    controller_reference,
    request_source,
    identity_value,
    experiment_run_id,
    target_path_sha256,
):
    if (
        not isinstance(authorization, _ExperimentMutationAuthorization)
        or authorization._token is not _EXPERIMENT_AUTHORIZATION_TOKEN
        or authorization.controller_reference != controller_reference
        or authorization.request_source != request_source
        or authorization.identity_value != identity_value
        or authorization.experiment_run_id != experiment_run_id
        or authorization.target_path_sha256 != target_path_sha256
        or not authorization.action_id
    ):
        raise PermissionError(
            "experiment operator input requires a bound ledger authorization"
        )
    from .experiment_controller import load_experiment_controller

    controller = load_experiment_controller(controller_reference)
    entries = controller._operator_action_ledger().replay()["entries"]
    if not any(
        entry.get("action_id") == authorization.action_id
        and entry.get("request_source") == request_source
        and entry.get("experiment_run_id") == experiment_run_id
        for entry in entries
    ):
        raise PermissionError(
            "experiment mutation authorization lacks ledger authority"
        )
    return authorization


def _experiment_mutation_authorization(
    controller,
    action,
    *,
    request_source,
    identity_value,
    run_manifest,
    target_path_sha256,
):
    entry = action.get("entry") if isinstance(action, dict) else None
    if (
        not isinstance(entry, dict)
        or entry.get("request_source") != request_source
    ):
        raise PermissionError(
            "experiment ledger action cannot authorize this mutation"
        )
    return _ExperimentMutationAuthorization(
        _EXPERIMENT_AUTHORIZATION_TOKEN,
        action_id=entry["action_id"],
        controller_reference=controller.reference,
        experiment_run_id=run_manifest["experiment_run_id"],
        identity_value=identity_value,
        request_source=request_source,
        target_path_sha256=target_path_sha256,
    )


def _validate_experiment_target_run(
    controller,
    *,
    run_manifest,
    run_dir,
):
    run_dir = Path(run_dir).resolve()
    state_path = _scheduler_state_path(run_dir)
    state = _read_json_if_exists(state_path)
    expected = {
        "experiment_controller_reference": controller.reference,
        "experiment_run_id": run_manifest["experiment_run_id"],
        "experiment_run_manifest_sha256": canonical_json_sha256(
            run_manifest
        ),
        "experiment_target_path_sha256": _path_digest(run_dir),
    }
    if not isinstance(state, dict) or any(
        state.get(key) != value for key, value in expected.items()
    ):
        raise PermissionError(
            "experiment target run binding is absent or inconsistent"
        )
    return expected


def record_experiment_operator_event(
    controller,
    *,
    protocol,
    run_manifest,
    run_dir,
    event_type,
    request_event,
    response_event=None,
    requested_at=None,
    answered_at=None,
    related_task_id=None,
    related_attempt_id=None,
):
    """Normalize one supported operator event without retaining raw content."""
    if event_type in _NON_OPERATOR_ACTION_EVENTS:
        return {
            "record_status": "ignored_non_operator_action",
            "event_type": event_type,
        }
    try:
        (
            request_source,
            expected_request_type,
            response_required,
        ) = _EXPERIMENT_OPERATOR_EVENT_SOURCES[event_type]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"unsupported experiment operator event: {event_type!r}"
        ) from exc

    request_event = _operator_event_object(request_event, "request_event")
    response_event = (
        _operator_event_object(response_event, "response_event")
        if response_event is not None
        else None
    )
    if request_event.get("event_type") != expected_request_type:
        raise ValueError(
            "operator request event does not match its action source"
        )
    if response_required and response_event is None:
        raise ValueError(
            "operator action requires an authoritative response event"
        )
    if not response_required and response_event is not None:
        raise ValueError(
            "request-only operator action cannot include a response event"
        )
    if (
        response_event is not None
        and response_event.get("event_type") != event_type
    ):
        raise ValueError(
            "operator response event does not match its action source"
        )
    requested_at = (
        requested_at
        or request_event.get("time")
        or request_event.get("requested_at")
    )
    answered_at = (
        answered_at
        or (
            response_event.get("time")
            or response_event.get("answered_at")
            if response_event is not None
            else None
        )
    )
    payload = request_event.get("payload")
    if isinstance(payload, dict):
        related_task_id = related_task_id or payload.get("task_id")
        related_attempt_id = (
            related_attempt_id or payload.get("attempt_id")
        )
    return controller.record_operator_action(
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=run_dir,
        request_source=request_source,
        request=request_event,
        response=response_event,
        requested_at=requested_at,
        answered_at=answered_at,
        related_task_id=related_task_id,
        related_attempt_id=related_attempt_id,
    )


def answer_experiment_manual_gate(
    controller,
    *,
    protocol,
    run_manifest,
    output_dir,
    question_id,
    answer,
    operator="operator",
    clock=None,
):
    """Enforce experiment accounting before applying a manual-gate answer."""
    _validate_experiment_target_run(
        controller,
        run_manifest=run_manifest,
        run_dir=output_dir,
    )
    request_event = _find_authoritative_operator_request(
        Path(output_dir) / "events.jsonl",
        event_type="manual_gate_required",
        identity_field="question_id",
        identity_value=question_id,
    )
    answered_at = _operator_now(clock)
    response_payload = {
        "question_id": question_id,
        "answer": answer,
        "operator": operator,
    }
    response_event = {
        "event_id": _operator_input_event_id(
            "manual-gate-answer",
            request_event,
            response_payload,
        ),
        "event_type": "operator_answer_received",
        "time": answered_at,
        "payload": response_payload,
    }
    action = record_experiment_operator_event(
        controller,
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=output_dir,
        event_type="operator_answer_received",
        request_event=request_event,
        response_event=response_event,
    )
    from .m0_runtime import answer_manual_gate

    result = answer_manual_gate(
        output_dir,
        question_id,
        answer,
        operator=operator,
        clock=clock,
    )
    return {**result, "operator_action": action}


def resolve_experiment_permission_request(
    controller,
    *,
    protocol,
    run_manifest,
    output_dir,
    request_id,
    decision,
    operator="operator",
    reason=None,
    clock=None,
):
    """Enforce experiment accounting before applying a permission answer."""
    _validate_experiment_target_run(
        controller,
        run_manifest=run_manifest,
        run_dir=output_dir,
    )
    request_event = _find_authoritative_operator_request(
        Path(output_dir) / "events.jsonl",
        event_type="permission_request_required",
        identity_field="request_id",
        identity_value=request_id,
    )
    answered_at = _operator_now(clock)
    response_payload = {
        "request_id": request_id,
        "decision": decision,
        "operator": operator,
        "reason": reason,
    }
    response_event = {
        "event_id": _operator_input_event_id(
            "permission-answer",
            request_event,
            response_payload,
        ),
        "event_type": "permission_request_resolved",
        "time": answered_at,
        "payload": response_payload,
    }
    action = record_experiment_operator_event(
        controller,
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=output_dir,
        event_type="permission_request_resolved",
        request_event=request_event,
        response_event=response_event,
    )
    from .m0_runtime import resolve_permission_request

    result = resolve_permission_request(
        output_dir,
        request_id,
        decision,
        operator=operator,
        reason=reason,
        clock=clock,
    )
    return {**result, "operator_action": action}


def stop_experiment_run(
    controller,
    *,
    protocol,
    run_manifest,
    run_dir,
    action_request_id,
    operator="operator",
    requested_at=None,
    grace_seconds=5,
    force=False,
):
    """Account for an operator interruption before stopping runtime work."""
    target_binding = _validate_experiment_target_run(
        controller,
        run_manifest=run_manifest,
        run_dir=run_dir,
    )
    requested_at = requested_at or _utc_now()
    action = record_experiment_operator_event(
        controller,
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=run_dir,
        event_type="run_stop_requested",
        request_event={
            "event_id": str(action_request_id),
            "event_type": "run_stop_requested",
            "time": requested_at,
            "payload": {
                "operator": operator,
                "run_dir_digest": _path_digest(run_dir),
            },
        },
    )
    if controller.controller_status == "active":
        controller.interrupt()
    elif controller.controller_status != "interrupted":
        raise ValueError(
            "experiment stop requires active or interrupted controller"
        )
    authorization = _experiment_mutation_authorization(
        controller,
        action,
        request_source="run_stop",
        identity_value=_path_digest(run_dir),
        run_manifest=run_manifest,
        target_path_sha256=target_binding[
            "experiment_target_path_sha256"
        ],
    )
    result = stop_run(
        run_dir,
        grace_seconds=grace_seconds,
        force=force,
        operator=operator,
        experiment_operator_authorization=authorization,
    )
    return {**result, "operator_action": action}


def resume_experiment_run(
    controller,
    *,
    protocol,
    run_manifest,
    run_dir,
    action_request_id,
    operator="operator",
    requested_at=None,
    **resume_binding,
):
    """Account for an operator resume before reacquiring experiment authority."""
    _validate_experiment_target_run(
        controller,
        run_manifest=run_manifest,
        run_dir=run_dir,
    )
    requested_at = requested_at or _utc_now()
    action = record_experiment_operator_event(
        controller,
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=run_dir,
        event_type="run_resume_requested",
        request_event={
            "event_id": str(action_request_id),
            "event_type": "run_resume_requested",
            "time": requested_at,
            "payload": {"operator": operator},
        },
    )
    if (
        action["record_status"] == "already_recorded"
        and controller.controller_status == "active"
    ):
        result = controller.snapshot()
        result["resume_status"] = "already_resumed"
    else:
        result = controller.resume_interrupted(**resume_binding)
    return {**result, "operator_action": action}


def record_experiment_corrective_guidance(
    controller,
    *,
    protocol,
    run_manifest,
    run_dir,
    action_request_id,
    guidance,
    operator="operator",
    requested_at=None,
    related_task_id=None,
    related_attempt_id=None,
):
    """Account for corrective guidance while retaining only its digest."""
    _validate_experiment_target_run(
        controller,
        run_manifest=run_manifest,
        run_dir=run_dir,
    )
    requested_at = requested_at or _utc_now()
    return record_experiment_operator_event(
        controller,
        protocol=protocol,
        run_manifest=run_manifest,
        run_dir=run_dir,
        event_type="corrective_guidance_received",
        request_event={
            "event_id": str(action_request_id),
            "event_type": "corrective_guidance_received",
            "time": requested_at,
            "payload": {
                "operator": operator,
                "guidance": guidance,
                "task_id": related_task_id,
                "attempt_id": related_attempt_id,
            },
        },
        related_task_id=related_task_id,
        related_attempt_id=related_attempt_id,
    )


def _operator_event_object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _find_authoritative_operator_request(
    events_path,
    *,
    event_type,
    identity_field,
    identity_value,
):
    if not events_path.is_file() or events_path.is_symlink():
        raise FileNotFoundError(
            f"missing authoritative runtime events: {events_path}"
        )
    found = None
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            if (
                isinstance(event, dict)
                and event.get("event_type") == event_type
                and isinstance(event.get("payload"), dict)
                and event["payload"].get(identity_field) == identity_value
            ):
                found = event
    if found is None:
        raise ValueError(
            f"authoritative operator request not found: {identity_value}"
        )
    return found


def _operator_now(clock):
    return clock.now() if clock is not None else _utc_now()


def _path_digest(path):
    return hashlib.sha256(
        str(Path(path).resolve()).encode("utf-8")
    ).hexdigest()


def _operator_input_event_id(kind, request_event, payload):
    request_identity = (
        request_event.get("event_id")
        or request_event.get("payload", {}).get("question_id")
        or request_event.get("payload", {}).get("request_id")
        or "unknown"
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "kind": kind,
                "payload": payload,
                "request_identity": request_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"OPERATOR-INPUT-{digest}"


def build_run_liveness_summary(run_dir, profile=None):
    run_dir = Path(run_dir).resolve()
    state = _read_json_if_exists(_scheduler_state_path(run_dir))
    registry = _read_json_if_exists(_worker_registry_path(run_dir))
    workers = registry.get("workers") if isinstance(registry, dict) else []
    if not isinstance(workers, list):
        workers = []
    scheduler_status = state.get("scheduler_status") if isinstance(state, dict) else None
    registry_status = registry.get("registry_status") if isinstance(registry, dict) else None
    processes = _process_counts(workers)
    liveness_status = _liveness_status(
        scheduler_status=scheduler_status,
        registry_status=registry_status,
        workers=workers,
        processes=processes,
    )
    return {
        "liveness_status": liveness_status,
        "scheduler_status": scheduler_status,
        "registry_status": registry_status,
        "processes": processes,
        "runtime_release": _runtime_release_summary(state, profile),
    }


def read_event_records_since(events_path, cursor=0, max_records=None):
    events_path = Path(events_path)
    if not events_path.exists():
        return cursor, []
    records = []
    with events_path.open(encoding="utf-8") as stream:
        stream.seek(cursor)
        for line in stream:
            stripped = line.strip()
            if not stripped:
                continue
            records.append(json.loads(stripped))
            if max_records is not None and len(records) >= max_records:
                break
        return stream.tell(), records


def stop_run(
    run_dir,
    grace_seconds=5,
    force=False,
    stale_only=False,
    operator="operator",
    experiment_operator_authorization=None,
):
    run_dir = Path(run_dir).resolve()
    now = _utc_now()
    state_path = _scheduler_state_path(run_dir)
    registry_path = _worker_registry_path(run_dir)
    state = _read_json_if_exists(state_path)
    if (
        isinstance(state, dict)
        and state.get("experiment_controller_reference") is not None
    ):
        validate_experiment_mutation_authorization(
            experiment_operator_authorization,
            controller_reference=state[
                "experiment_controller_reference"
            ],
            request_source="run_stop",
            identity_value=_path_digest(run_dir),
            experiment_run_id=state.get("experiment_run_id"),
            target_path_sha256=state.get(
                "experiment_target_path_sha256"
            ),
        )
    registry = _read_json_if_exists(registry_path)
    workers = registry.get("workers") if isinstance(registry, dict) else []
    if not isinstance(workers, list):
        workers = []

    if stale_only and not _run_is_stale(state, registry, workers):
        return _stop_summary(
            "not_stale",
            run_dir,
            state_path,
            registry_path,
            workers,
            skipped_live=_live_worker_count(workers),
        )

    updated_workers = []
    for worker in workers:
        if isinstance(worker, dict):
            updated_workers.append(
                _cleanup_stale_worker(worker, now)
                if stale_only
                else _stop_worker(worker, now, grace_seconds=grace_seconds, force=force)
            )
        else:
            updated_workers.append(worker)

    stop_status = "stopped"
    if any(
        isinstance(worker, dict) and worker.get("worker_status") == "stop_requested"
        for worker in updated_workers
    ):
        stop_status = "stop_requested"
    stop_mode = "stale_cleanup" if stale_only else "stop"

    if isinstance(registry, dict):
        registry["registry_status"] = stop_status
        registry["worker_count"] = len(updated_workers)
        registry["workers"] = updated_workers
        registry["stop_requested_at"] = now
        registry["stop_operator"] = operator
        registry["stop_mode"] = stop_mode
        _write_json(registry_path, registry)

    if isinstance(state, dict) and state:
        previous_status = state.get("scheduler_status")
        state["previous_scheduler_status"] = previous_status
        state["scheduler_status"] = stop_status
        state["stop_requested_at"] = now
        state["stop_operator"] = operator
        state["stop_mode"] = stop_mode
        _write_json(state_path, state)
    stop_request = write_run_stop_request(
        run_dir,
        stop_status=stop_status,
        requested_at=now,
        operator=operator,
        mode=stop_mode,
    )

    return _stop_summary(
        stop_status,
        run_dir,
        state_path,
        registry_path,
        updated_workers,
        stop_request_path=stop_request["stop_request_path"],
    )


def read_run_stop_request(run_dir):
    request = _read_json_if_exists(_run_stop_request_path(run_dir))
    return request if isinstance(request, dict) else {}


def write_run_stop_request(run_dir, stop_status, requested_at, operator, mode):
    path = _run_stop_request_path(run_dir)
    request = {
        "stop_request_path": str(path),
        "stop_status": stop_status,
        "stop_requested_at": requested_at,
        "stop_operator": operator,
        "stop_mode": mode,
    }
    _write_json(path, request)
    return request


def cleanup_stale_runs(profile, operator="operator"):
    work_root = Path(profile["work_root"]).resolve()
    run_root = work_root / "runs"
    if not run_root.exists():
        return {
            "stop_status": "no_runs",
            "run_count": 0,
            "runs": [],
            "run_root": str(run_root),
        }
    runs = []
    for run_dir in sorted(path for path in run_root.iterdir() if path.is_dir()):
        state = _read_json_if_exists(_scheduler_state_path(run_dir))
        if (
            isinstance(state, dict)
            and state.get("experiment_controller_reference") is not None
        ):
            runs.append(
                {
                    "stop_status": "experiment_gateway_required",
                    "run_dir": str(run_dir.resolve()),
                }
            )
            continue
        runs.append(
            stop_run(
                run_dir,
                stale_only=True,
                operator=operator,
            )
        )
    cleaned = [run for run in runs if run["stop_status"] == "stopped"]
    return {
        "stop_status": "stale_cleaned" if cleaned else "not_stale",
        "run_count": len(runs),
        "cleaned_count": len(cleaned),
        "runs": runs,
        "run_root": str(run_root),
    }


def _run_is_stale(state, registry, workers):
    scheduler_status = state.get("scheduler_status") if isinstance(state, dict) else None
    registry_status = registry.get("registry_status") if isinstance(registry, dict) else None
    claims_running = scheduler_status in _RUNNING_SCHEDULER_STATUSES or registry_status == "running"
    if not claims_running:
        return False
    running_workers = [
        worker
        for worker in workers
        if isinstance(worker, dict)
        and worker.get("worker_status") in _RUNNING_WORKER_STATUSES
    ]
    if not running_workers:
        return True
    return not any(_pid_is_running(_worker_pid(worker)) for worker in running_workers)


def _liveness_status(scheduler_status, registry_status, workers, processes):
    claims_running = (
        scheduler_status in _RUNNING_SCHEDULER_STATUSES
        or registry_status == "running"
        or any(
            isinstance(worker, dict)
            and worker.get("worker_status") in _RUNNING_WORKER_STATUSES
            for worker in workers
        )
    )
    if claims_running:
        return "running-alive" if processes["live"] else "running-stale"
    return scheduler_status or registry_status or "unknown"


def _process_counts(workers):
    entries = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        pid = _worker_pid(worker)
        if not pid:
            continue
        live = _pid_is_running(pid)
        entries.append(
            {
                "pid": pid,
                "live": live,
                "worker_agent_id": worker.get("worker_agent_id") or worker.get("worker_id"),
                "worker_status": worker.get("worker_status"),
            }
        )
    return {
        "registered": len(entries),
        "live": sum(1 for entry in entries if entry["live"]),
        "stale": sum(1 for entry in entries if not entry["live"]),
        "sample": entries[:5],
    }


def _runtime_release_summary(state, profile):
    profile = profile or {}
    state = state if isinstance(state, dict) else {}
    release_id = state.get("runtime_release_id") or profile.get("runtime_release_id")
    release_root = state.get("runtime_release_root") or profile.get("runtime_release_root")
    return {
        "id": release_id,
        "root": release_root,
        "managed": bool(release_id or release_root),
    }


def _cleanup_stale_worker(worker, now):
    updated = dict(worker)
    pid = _worker_pid(worker)
    if pid and _pid_is_running(pid):
        return updated
    updated["worker_status"] = "stopped"
    updated["stopped_by"] = "stale_pid" if pid else "no_pid"
    updated["stopped_at"] = now
    return updated


def _stop_worker(worker, now, grace_seconds=5, force=False):
    updated = dict(worker)
    _write_stop_file(updated.get("stop_file"))
    pid = _worker_pid(updated)
    if not pid:
        updated["worker_status"] = "stopped"
        updated["stopped_by"] = "stop_file"
        updated["stopped_at"] = now
        return updated
    if not _pid_is_running(pid):
        updated["worker_status"] = "stopped"
        updated["stopped_by"] = "stale_pid"
        updated["stopped_at"] = now
        return updated
    stop_process = _stop_process_tree(pid, grace_seconds=grace_seconds, force=force)
    updated["worker_status"] = "stopped" if stop_process["stopped"] else "stop_requested"
    updated["stopped_by"] = stop_process["stopped_by"]
    updated["stopped_at"] = now
    updated["owned_descendant_pids"] = stop_process["owned_descendant_pids"]
    if stop_process.get("permission_denied"):
        updated["permission_denied"] = True
    return updated


def _stop_process_tree(pid, grace_seconds=5, force=False):
    if not _pid_owned_by_current_user(pid):
        return {
            "stopped": False,
            "stopped_by": "permission_denied",
            "owned_descendant_pids": [],
            "permission_denied": True,
        }
    descendants = _owned_descendant_pids(pid)
    targets = [*descendants, pid]
    _signal_targets(targets, signal.SIGTERM)
    if _wait_for_processes_to_exit(targets, grace_seconds):
        return {
            "stopped": True,
            "stopped_by": "terminated",
            "owned_descendant_pids": descendants,
        }
    if force:
        _signal_targets(targets, signal.SIGKILL)
        if _wait_for_processes_to_exit(targets, grace_seconds):
            return {
                "stopped": True,
                "stopped_by": "killed",
                "owned_descendant_pids": descendants,
            }
        return {
            "stopped": False,
            "stopped_by": "kill_requested",
            "owned_descendant_pids": descendants,
        }
    return {
        "stopped": False,
        "stopped_by": "terminate_requested",
        "owned_descendant_pids": descendants,
    }


def _signal_targets(targets, sig):
    for target in targets:
        if target in {os.getpid(), os.getppid()}:
            continue
        try:
            os.kill(target, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue


def _wait_for_processes_to_exit(targets, timeout_seconds):
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while time.monotonic() <= deadline:
        if not any(_pid_is_running(pid) for pid in targets):
            return True
        time.sleep(0.05)
    return not any(_pid_is_running(pid) for pid in targets)


def _owned_descendant_pids(pid):
    snapshot = _process_snapshot()
    children_by_parent = {}
    for child_pid, process in snapshot.items():
        children_by_parent.setdefault(process["ppid"], []).append(child_pid)
    descendants = []
    queue = list(children_by_parent.get(pid, []))
    while queue:
        child_pid = queue.pop(0)
        child = snapshot.get(child_pid)
        if not child or child.get("uid") != os.getuid():
            continue
        descendants.append(child_pid)
        queue.extend(children_by_parent.get(child_pid, []))
    return descendants


def _process_snapshot():
    proc = Path("/proc")
    if not proc.exists():
        return {}
    snapshot = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        status = _read_proc_status(entry / "status")
        if status:
            snapshot[int(entry.name)] = status
    return snapshot


def _read_proc_status(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    status = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.strip()
        if key == "PPid":
            status["ppid"] = int(value)
        elif key == "Uid":
            status["uid"] = int(value.split()[0])
        elif key == "State":
            status["state"] = value.split()[0]
    if "ppid" not in status or "uid" not in status:
        return {}
    return status


def _pid_owned_by_current_user(pid):
    if not pid or not _pid_is_running(pid):
        return False
    status = _read_proc_status(Path("/proc") / str(pid) / "status")
    if status:
        return status.get("uid") == os.getuid()
    try:
        os.kill(pid, 0)
    except PermissionError:
        return False
    except ProcessLookupError:
        return False
    return True


def _pid_is_running(pid):
    if not pid:
        return False
    status = _read_proc_status(Path("/proc") / str(pid) / "status")
    if status.get("state") == "Z":
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_pid(worker):
    for key in ["worker_pid", "pid", "process_id"]:
        value = worker.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _write_stop_file(path):
    if not path:
        return
    stop_file = Path(path)
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop\n", encoding="utf-8")


def _stop_summary(
    stop_status,
    run_dir,
    state_path,
    registry_path,
    workers,
    skipped_live=0,
    stop_request_path=None,
):
    counts = _worker_counts(workers)
    if skipped_live:
        counts["skipped_live"] = skipped_live
    summary = {
        "stop_status": stop_status,
        "latest_run": run_dir.name,
        "run_dir": str(run_dir),
        "state_path": str(state_path) if state_path else None,
        "registry_path": str(registry_path) if registry_path else None,
        "workers": counts,
    }
    if stop_request_path:
        summary["stop_request_path"] = str(stop_request_path)
    return summary


def _worker_counts(workers):
    workers = [worker for worker in workers if isinstance(worker, dict)]
    statuses = [worker.get("worker_status") for worker in workers]
    return {
        "total": len(workers),
        "stopped": sum(1 for status in statuses if status == "stopped"),
        "stop_requested": sum(1 for status in statuses if status == "stop_requested"),
        "running": sum(1 for status in statuses if status in _RUNNING_WORKER_STATUSES),
    }


def _live_worker_count(workers):
    return sum(
        1
        for worker in workers
        if isinstance(worker, dict) and _pid_is_running(_worker_pid(worker))
    )


def _scheduler_state_path(run_dir):
    run_dir = Path(run_dir)
    two_phase = run_dir / "state" / "two_phase_scheduler_state.json"
    if two_phase.exists():
        return two_phase
    legacy = run_dir / "state" / "scheduler_state.json"
    if legacy.exists():
        return legacy
    return two_phase


def _worker_registry_path(run_dir):
    run_dir = Path(run_dir)
    process_registry = run_dir / "state" / "worker_process_registry.json"
    if process_registry.exists():
        return process_registry
    legacy = run_dir / "state" / "worker_registry.json"
    if legacy.exists():
        return legacy
    return process_registry


def _run_stop_request_path(run_dir):
    return Path(run_dir) / "state" / "run_stop_request.json"


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

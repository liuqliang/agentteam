import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path


_RUNNING_WORKER_STATUSES = {"running", "started", "idle", "busy"}


def stop_run(run_dir, grace_seconds=5, force=False, stale_only=False, operator="operator"):
    run_dir = Path(run_dir).resolve()
    now = _utc_now()
    state_path = _scheduler_state_path(run_dir)
    registry_path = _worker_registry_path(run_dir)
    state = _read_json_if_exists(state_path)
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

    if isinstance(registry, dict):
        registry["registry_status"] = stop_status
        registry["worker_count"] = len(updated_workers)
        registry["workers"] = updated_workers
        registry["stop_requested_at"] = now
        registry["stop_operator"] = operator
        registry["stop_mode"] = "stale_cleanup" if stale_only else "stop"
        _write_json(registry_path, registry)

    if isinstance(state, dict) and state:
        previous_status = state.get("scheduler_status")
        state["previous_scheduler_status"] = previous_status
        state["scheduler_status"] = stop_status
        state["stop_requested_at"] = now
        state["stop_operator"] = operator
        state["stop_mode"] = "stale_cleanup" if stale_only else "stop"
        _write_json(state_path, state)

    return _stop_summary(stop_status, run_dir, state_path, registry_path, updated_workers)


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
    runs = [
        stop_run(run_dir, stale_only=True, operator=operator)
        for run_dir in sorted(path for path in run_root.iterdir() if path.is_dir())
    ]
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
    claims_running = scheduler_status in {"running", "waiting", "max_ticks_reached"} or registry_status == "running"
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


def _stop_summary(stop_status, run_dir, state_path, registry_path, workers, skipped_live=0):
    counts = _worker_counts(workers)
    if skipped_live:
        counts["skipped_live"] = skipped_live
    return {
        "stop_status": stop_status,
        "latest_run": run_dir.name,
        "run_dir": str(run_dir),
        "state_path": str(state_path) if state_path else None,
        "registry_path": str(registry_path) if registry_path else None,
        "workers": counts,
    }


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

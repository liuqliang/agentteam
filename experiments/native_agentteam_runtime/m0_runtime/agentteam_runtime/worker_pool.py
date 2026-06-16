import json
import time
from pathlib import Path

from .mailbox_worker import FileMailboxWorkerProcessSupervisor


WORKER_HEARTBEAT_STALE_SECONDS = 120
WORKER_DIAGNOSTIC_STATES = [
    "idle",
    "processing",
    "processed",
    "processing_stale",
    "no_heartbeat",
    "exited",
]
WORKER_DIAGNOSTIC_ATTENTION_STATES = {
    "processing_stale",
    "no_heartbeat",
    "exited",
}


class FileMailboxWorkerPoolSupervisor:
    def __init__(
        self,
        agent_pool_path,
        output_dir,
        runtime_profile_defaults=None,
        env=None,
        poll_interval_seconds=0.05,
        max_restart_count=None,
        heartbeat_stale_seconds=WORKER_HEARTBEAT_STALE_SECONDS,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.runtime_profile_defaults = runtime_profile_defaults
        self.env = env
        self.poll_interval_seconds = poll_interval_seconds
        self.max_restart_count = max_restart_count
        self.heartbeat_stale_seconds = heartbeat_stale_seconds
        self.process_registry_path = self.output_dir / "state" / "worker_process_registry.json"
        self.workers = []
        self.restart_counts = {}
        self.quarantined_agents = {}

    def start(self):
        agents = _worker_agents(self.agent_pool_path)
        for agent in agents:
            self.restart_counts.setdefault(agent["agent_id"], 0)
        self.workers = [
            self._worker_for_agent(agent)
            for agent in agents
        ]
        for worker in self.workers:
            worker.start()
        starts = [
            self._worker_health(worker)
            for worker in self.workers
        ]
        summary = self._summary("running", starts)
        self._write_registry(summary)
        return summary

    def resume_from_registry(self):
        registry = self._load_registry()
        registry_workers = {
            worker.get("worker_agent_id"): worker
            for worker in registry.get("workers", [])
        }
        self.workers = []
        for agent in _worker_agents(self.agent_pool_path):
            agent_id = agent["agent_id"]
            worker = self._worker_for_agent(agent)
            registered = registry_workers.get(agent_id, {})
            self.restart_counts[agent_id] = registered.get("restart_count", 0)
            if registered.get("worker_status") == "quarantined":
                self.quarantined_agents[agent_id] = registered.get(
                    "quarantine_reason",
                    "restart_budget_exceeded",
                )
            if registered.get("worker_pid"):
                worker.attach_existing_process(
                    registered["worker_pid"],
                    stop_file=registered.get("stop_file"),
                )
            self.workers.append(worker)
        workers = [
            self._worker_health(worker)
            for worker in self.workers
        ]
        summary = {
            **self._summary(self._pool_health_status(workers), workers),
            "resume_status": "resumed" if registry else "registry_missing",
        }
        self._write_registry(summary)
        return summary

    def stop(self):
        stops = [
            self._with_restart_count(
                self._worker_with_heartbeat_diagnostics(
                    worker.stop(),
                    self._latest_worker_heartbeat(worker.agent_id),
                )
            )
            for worker in self.workers
        ]
        summary = self._summary("stopped", stops)
        self._write_registry(summary)
        return summary

    def health_check(self):
        workers = [
            self._worker_health(worker)
            for worker in self.workers
        ]
        summary = self._summary(self._pool_health_status(workers), workers)
        self._write_registry(summary)
        return summary

    def restart_exited_workers(self):
        restarted_count = 0
        workers = []
        for worker in self.workers:
            restart = self._restart_worker_if_allowed(worker)
            previous_worker = self._with_restart_count(restart["previous_worker"])
            if restart["restart_status"] == "restarted":
                agent_id = restart["new_worker"]["worker_agent_id"]
                self.restart_counts[agent_id] = self.restart_counts.get(agent_id, 0) + 1
                restarted_count += 1
            workers.append(
                {
                    "restart_status": restart["restart_status"],
                    "previous_worker": previous_worker,
                    "new_worker": self._with_restart_count(restart["new_worker"]),
                }
            )
        current_workers = [
            self._worker_health(worker)
            for worker in self.workers
        ]
        pool_status = self._pool_health_status(current_workers)
        self._write_registry(self._summary(pool_status, current_workers))
        return {
            "pool_status": pool_status,
            "worker_count": len(workers),
            "restarted_count": restarted_count,
            "process_registry_path": str(self.process_registry_path),
            "workers": workers,
        }

    def supervise_once(self):
        before = self.health_check()
        restart = self.restart_exited_workers()
        after = self.health_check()
        return {
            "supervision_status": after["pool_status"],
            "restarted_count": restart["restarted_count"],
            "before": before,
            "restart": restart,
            "after": after,
        }

    def _worker_for_agent(self, agent):
        agent_pool = _load_agent_pool(self.agent_pool_path)
        profile = _runtime_profile_for_agent(
            agent_pool,
            agent,
            self.runtime_profile_defaults,
        )
        defaults = self.runtime_profile_defaults or {}
        runtime = profile.get("adapter", "fake")
        if runtime not in {"fake", "codex"}:
            raise ValueError(f"unsupported mailbox worker pool runtime: {runtime}")
        return FileMailboxWorkerProcessSupervisor(
            self.agent_pool_path,
            self.output_dir,
            agent["agent_id"],
            env=self.env,
            poll_interval_seconds=self.poll_interval_seconds,
            runtime=runtime,
            codex_command=_codex_command_from_profile(profile, defaults),
            codex_model=profile.get("model", defaults.get("model")),
            codex_sandbox=profile.get(
                "sandbox",
                defaults.get("sandbox", "workspace-write"),
            ),
            codex_timeout_seconds=_codex_timeout_seconds_from_profile(
                profile,
                defaults,
            ),
            codex_fallback_worktree_path=profile.get(
                "fallback_worktree_path",
                defaults.get("fallback_worktree_path"),
            ),
        )

    def _worker_health(self, worker):
        health = worker.health()
        heartbeat = self._latest_worker_heartbeat(worker.agent_id)
        health = self._worker_with_heartbeat_diagnostics(health, heartbeat)
        if health.get("worker_agent_id") in self.quarantined_agents:
            health = self._quarantine_health(worker, health)
        return self._with_restart_count(health)

    def _worker_with_heartbeat_diagnostics(self, health, heartbeat):
        if heartbeat:
            heartbeat_age_seconds = _heartbeat_age_seconds(heartbeat)
            health = {
                **health,
                "last_activity": heartbeat.get("activity"),
                "last_poll_status": heartbeat.get("poll_status"),
                "last_heartbeat_at": heartbeat.get("updated_at"),
                "last_heartbeat_pid": heartbeat.get("worker_pid"),
                "heartbeat_path": heartbeat.get("heartbeat_path"),
                "heartbeat_task_id": heartbeat.get("task_id"),
                "heartbeat_attempt_id": heartbeat.get("attempt_id"),
                "heartbeat_message_id": heartbeat.get("source_message_id"),
                "heartbeat_result_status": heartbeat.get("result_status"),
                "heartbeat_changed_file_count": heartbeat.get("changed_file_count"),
                "heartbeat_progress_summary": heartbeat.get("progress_summary"),
                "heartbeat_age_seconds": heartbeat_age_seconds,
            }
        diagnostic_state = _worker_diagnostic_state(
            health.get("worker_status"),
            heartbeat,
            self.heartbeat_stale_seconds,
        )
        return {
            **health,
            "worker_diagnostic_state": diagnostic_state,
            "heartbeat_is_stale": diagnostic_state == "processing_stale",
            "heartbeat_stale_after_seconds": self.heartbeat_stale_seconds,
        }

    def _latest_worker_heartbeat(self, agent_id):
        candidates = []
        for path in self._heartbeat_paths(agent_id):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                modified = path.stat().st_mtime
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            payload = dict(payload)
            payload["heartbeat_path"] = str(path)
            payload["heartbeat_mtime"] = modified
            candidates.append((modified, str(path), payload))
        if not candidates:
            return {}
        return sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]

    def _heartbeat_paths(self, agent_id):
        filename = f"{agent_id}.heartbeat.json"
        paths = [self.output_dir / "state" / "workers" / filename]
        steps_dir = self.output_dir / "steps"
        if steps_dir.exists():
            paths.extend(
                sorted(steps_dir.glob(f"*/state/workers/{filename}"))
            )
        return paths

    def _restart_worker_if_allowed(self, worker):
        previous_worker = worker.health()
        agent_id = previous_worker.get("worker_agent_id")
        if agent_id in self.quarantined_agents:
            return {
                "restart_status": "quarantined",
                "previous_worker": self._quarantine_health(worker, previous_worker),
                "new_worker": self._quarantine_health(worker, previous_worker),
            }
        if previous_worker["worker_status"] == "running":
            return {
                "restart_status": "not_needed",
                "previous_worker": previous_worker,
                "new_worker": previous_worker,
            }
        if self._restart_budget_exceeded(agent_id):
            self.quarantined_agents[agent_id] = "restart_budget_exceeded"
            quarantined = self._quarantine_health(worker, previous_worker)
            return {
                "restart_status": "quarantined",
                "previous_worker": previous_worker,
                "new_worker": quarantined,
            }
        return worker.restart_if_exited()

    def _restart_budget_exceeded(self, agent_id):
        if self.max_restart_count is None:
            return False
        return self.restart_counts.get(agent_id, 0) >= self.max_restart_count

    def _quarantine_health(self, worker, health):
        return {
            **health,
            "worker_status": "quarantined",
            "worker_agent_id": worker.agent_id,
            "worker_runtime": worker.runtime,
            "quarantine_reason": self.quarantined_agents.get(
                worker.agent_id,
                "restart_budget_exceeded",
            ),
        }

    def _with_restart_count(self, worker):
        agent_id = worker.get("worker_agent_id")
        if not agent_id:
            return worker
        return {
            **worker,
            "restart_count": self.restart_counts.get(agent_id, 0),
        }

    def _pool_health_status(self, workers):
        if not workers:
            return "not_started"
        if all(worker["worker_status"] == "running" for worker in workers):
            return "running"
        if all(worker["worker_status"] == "not_started" for worker in workers):
            return "not_started"
        return "degraded"

    def _summary(self, status, workers):
        diagnostic_counts = _diagnostic_worker_counts(workers)
        return {
            "pool_status": status,
            "pool_diagnostic_status": _pool_diagnostic_status(diagnostic_counts),
            "diagnostic_worker_counts": diagnostic_counts,
            "worker_count": len(workers),
            "max_restart_count": self.max_restart_count,
            "process_registry_path": str(self.process_registry_path),
            "workers": workers,
        }

    def _write_registry(self, summary):
        self.process_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.process_registry_path.write_text(
            json.dumps(
                {
                    "registry_status": summary["pool_status"],
                    "pool_diagnostic_status": summary["pool_diagnostic_status"],
                    "diagnostic_worker_counts": summary["diagnostic_worker_counts"],
                    "worker_count": summary["worker_count"],
                    "workers": summary["workers"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _load_registry(self):
        if not self.process_registry_path.exists():
            return {}
        return json.loads(self.process_registry_path.read_text(encoding="utf-8"))


def _worker_diagnostic_state(worker_status, heartbeat, stale_seconds):
    if worker_status == "exited":
        return "exited"
    if not heartbeat:
        return "no_heartbeat"
    activity = heartbeat.get("activity") or heartbeat.get("poll_status")
    heartbeat_age_seconds = _heartbeat_age_seconds(heartbeat)
    if (
        activity == "processing"
        and heartbeat_age_seconds is not None
        and heartbeat_age_seconds >= stale_seconds
    ):
        return "processing_stale"
    if activity in {"idle", "processing", "processed"}:
        return activity
    return "no_heartbeat"


def _heartbeat_age_seconds(heartbeat):
    try:
        heartbeat_mtime = float(heartbeat.get("heartbeat_mtime"))
    except (TypeError, ValueError):
        return None
    return max(0, int(time.time() - heartbeat_mtime))


def _diagnostic_worker_counts(workers):
    counts = {state: 0 for state in WORKER_DIAGNOSTIC_STATES}
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        state = worker.get("worker_diagnostic_state") or "no_heartbeat"
        if state not in counts:
            counts[state] = 0
        counts[state] += 1
    return counts


def _pool_diagnostic_status(diagnostic_counts):
    if not any(diagnostic_counts.values()):
        return "not_started"
    if any(
        diagnostic_counts.get(state, 0)
        for state in WORKER_DIAGNOSTIC_ATTENTION_STATES
    ):
        return "attention"
    return "healthy"


def _worker_agents(agent_pool_path):
    agent_pool = _load_agent_pool(agent_pool_path)
    scheduler_agent_id = agent_pool.get("scheduler_agent_id")
    return [
        agent
        for agent in agent_pool.get("agents", [])
        if agent.get("agent_id") != scheduler_agent_id
    ]


def _load_agent_pool(agent_pool_path):
    return json.loads(Path(agent_pool_path).read_text(encoding="utf-8"))


def _runtime_profile_for_agent(agent_pool, agent, defaults):
    profile = agent.get("runtime_profile")
    if profile:
        _require_runtime_profile_object(profile, "runtime_profile")
        return profile
    role_profile = _role_runtime_profile(agent_pool, agent)
    if role_profile:
        return role_profile
    if defaults:
        _require_runtime_profile_object(defaults, "runtime_profile_defaults")
        return defaults
    return {"adapter": "fake"}


def _role_runtime_profile(agent_pool, agent):
    role_profiles = agent_pool.get("role_runtime_profiles", {})
    if not isinstance(role_profiles, dict):
        raise ValueError("role_runtime_profiles must be an object")
    profile = role_profiles.get(agent.get("role"))
    if profile:
        _require_runtime_profile_object(profile, "role runtime_profile")
    return profile


def _require_runtime_profile_object(profile, label):
    if not isinstance(profile, dict):
        raise ValueError(f"{label} must be an object")


def _codex_command_from_profile(profile, defaults):
    command = defaults.get("command") or profile.get("command")
    if command is not None and not _is_string_list(command):
        raise ValueError("codex runtime_profile command must be a string array")
    return command


def _codex_timeout_seconds_from_profile(profile, defaults):
    timeout_seconds = profile.get(
        "timeout_seconds",
        defaults.get("timeout_seconds", 300),
    )
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("codex runtime_profile timeout_seconds must be an integer >= 1")
    return timeout_seconds


def _is_string_list(value):
    return (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
    )

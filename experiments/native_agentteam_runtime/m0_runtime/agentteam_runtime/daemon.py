import json
from pathlib import Path

from .m0_runtime import FileScheduler, SystemClock


class FileSchedulerDaemon:
    def __init__(
        self,
        agent_pool_path,
        backlog_path,
        output_dir,
        clock=None,
        project_root=None,
        runtime_adapter=None,
        runtime_adapter_factory=None,
        runtime_profile_defaults=None,
        max_attempts=1,
        cleanup_accepted_worktrees=False,
        integrate_accepted_patch=False,
        integration_verification_command=None,
        commit_verified_integration=False,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.worker_registry_path = self.output_dir / "state" / "worker_registry.json"
        self.scheduler = FileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=self.clock,
            project_root=project_root,
            runtime_adapter=runtime_adapter,
            runtime_adapter_factory=runtime_adapter_factory,
            runtime_profile_defaults=runtime_profile_defaults,
            max_attempts=max_attempts,
            cleanup_accepted_worktrees=cleanup_accepted_worktrees,
            integrate_accepted_patch=integrate_accepted_patch,
            integration_verification_command=integration_verification_command,
            commit_verified_integration=commit_verified_integration,
        )

    def tick(self):
        heartbeat_time = self.clock.now()
        registry = self._load_or_create_registry()
        registry["tick_count"] += 1
        registry["registry_status"] = "active"
        for worker in registry["workers"]:
            worker["worker_status"] = "idle"
            worker["active_task_id"] = None
            worker["last_heartbeat"] = heartbeat_time
        self._write_registry(registry)

        step = self.scheduler.step_once()
        scheduler_summary = self.scheduler._summary(self.scheduler.state["scheduler_status"])
        return {
            "daemon_status": "idle" if step["step_status"] == "idle" else "running",
            "tick_status": step["step_status"],
            "step": step,
            "processed_task_ids": scheduler_summary["processed_task_ids"],
            "step_count": scheduler_summary["step_count"],
            "events_path": scheduler_summary["events_path"],
            "state_path": scheduler_summary["state_path"],
            "state_db_path": scheduler_summary["state_db_path"],
            "worker_registry_path": str(self.worker_registry_path),
            "tick_count": registry["tick_count"],
        }

    def run_until_idle(self, max_ticks=100):
        if max_ticks < 1:
            raise ValueError("max_ticks must be at least 1")
        summary = None
        for _ in range(max_ticks):
            summary = self.tick()
            if summary["tick_status"] == "idle":
                return summary
        return {
            **summary,
            "daemon_status": "max_ticks_reached",
        }

    def _load_or_create_registry(self):
        if self.worker_registry_path.exists():
            return json.loads(self.worker_registry_path.read_text(encoding="utf-8"))
        agent_pool = json.loads(self.agent_pool_path.read_text(encoding="utf-8"))
        workers = []
        for agent in agent_pool.get("agents", []):
            if agent.get("agent_id") == agent_pool.get("scheduler_agent_id"):
                continue
            workers.append(
                {
                    "worker_id": f"WORKER-{agent['agent_id']}",
                    "agent_id": agent["agent_id"],
                    "role": agent["role"],
                    "worker_status": "idle",
                    "runtime_adapter": agent.get("runtime_adapter"),
                    "runtime_profile": agent.get("runtime_profile"),
                    "active_task_id": None,
                    "last_heartbeat": None,
                }
            )
        return {
            "registry_status": "initialized",
            "tick_count": 0,
            "workers": workers,
        }

    def _write_registry(self, registry):
        self.worker_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_registry_path.write_text(
            json.dumps(registry, sort_keys=True),
            encoding="utf-8",
        )


def run_file_daemon(*args, max_ticks=100, **kwargs):
    daemon = FileSchedulerDaemon(*args, **kwargs)
    return daemon.run_until_idle(max_ticks=max_ticks)

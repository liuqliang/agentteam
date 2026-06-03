import json
from pathlib import Path

from .mailbox_worker import FileMailboxWorkerProcessSupervisor


class FileMailboxWorkerPoolSupervisor:
    def __init__(
        self,
        agent_pool_path,
        output_dir,
        runtime_profile_defaults=None,
        env=None,
        poll_interval_seconds=0.05,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.runtime_profile_defaults = runtime_profile_defaults
        self.env = env
        self.poll_interval_seconds = poll_interval_seconds
        self.process_registry_path = self.output_dir / "state" / "worker_process_registry.json"
        self.workers = []
        self.restart_counts = {}

    def start(self):
        agents = _worker_agents(self.agent_pool_path)
        for agent in agents:
            self.restart_counts.setdefault(agent["agent_id"], 0)
        self.workers = [
            self._worker_for_agent(agent)
            for agent in agents
        ]
        starts = [
            self._with_restart_count(worker.start())
            for worker in self.workers
        ]
        summary = self._summary("running", starts)
        self._write_registry(summary)
        return summary

    def stop(self):
        stops = [
            self._with_restart_count(worker.stop())
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
            restart = worker.restart_if_exited()
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
        profile = self.runtime_profile_defaults or agent.get("runtime_profile") or {"adapter": "fake"}
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
            codex_command=profile.get("command"),
            codex_model=profile.get("model"),
            codex_sandbox=profile.get("sandbox", "workspace-write"),
            codex_timeout_seconds=profile.get("timeout_seconds", 300),
            codex_fallback_worktree_path=profile.get("fallback_worktree_path"),
        )

    def _worker_health(self, worker):
        return self._with_restart_count(worker.health())

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
        return {
            "pool_status": status,
            "worker_count": len(workers),
            "process_registry_path": str(self.process_registry_path),
            "workers": workers,
        }

    def _write_registry(self, summary):
        self.process_registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.process_registry_path.write_text(
            json.dumps(
                {
                    "registry_status": summary["pool_status"],
                    "worker_count": summary["worker_count"],
                    "workers": summary["workers"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _worker_agents(agent_pool_path):
    agent_pool = json.loads(Path(agent_pool_path).read_text(encoding="utf-8"))
    scheduler_agent_id = agent_pool.get("scheduler_agent_id")
    return [
        agent
        for agent in agent_pool.get("agents", [])
        if agent.get("agent_id") != scheduler_agent_id
    ]

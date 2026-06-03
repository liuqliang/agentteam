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

    def start(self):
        self.workers = [
            self._worker_for_agent(agent)
            for agent in _worker_agents(self.agent_pool_path)
        ]
        starts = [worker.start() for worker in self.workers]
        summary = self._summary("running", starts)
        self._write_registry(summary)
        return summary

    def stop(self):
        stops = [worker.stop() for worker in self.workers]
        summary = self._summary("stopped", stops)
        self._write_registry(summary)
        return summary

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
        )

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

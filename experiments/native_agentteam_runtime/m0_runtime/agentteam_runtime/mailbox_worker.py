import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .m0_runtime import FakeRuntimeAdapter, SystemClock


class FileMailboxWorker:
    def __init__(
        self,
        agent_pool_path,
        output_dir,
        agent_id,
        runtime_adapter=None,
        clock=None,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.agent_id = agent_id
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self.clock = clock or SystemClock()
        self.agent = self._load_agent()
        self.inbox_path = self.output_dir / self.agent["inbox_path"]
        self.outbox_path = self.output_dir / self.agent["outbox_path"]

    def poll_once(self, message_id=None, worktree_path=None):
        message = self._next_dispatch(message_id=message_id)
        if not message:
            return {
                "poll_status": "idle",
                "reason": "no_dispatch_message",
            }
        runtime_result = self.runtime_adapter.run(message, worktree_path=worktree_path)
        result_message = self._result_message(message, runtime_result)
        _append_jsonl(self.outbox_path, [result_message])
        return {
            "poll_status": "processed",
            "source_message_id": message["message_id"],
            "result_status": runtime_result["result_status"],
            "changed_files": runtime_result["changed_files"],
            "outbox_path": str(self.outbox_path),
        }

    def _next_dispatch(self, message_id=None):
        answered = {
            record.get("payload", {}).get("source_message_id")
            for record in _read_jsonl_if_exists(self.outbox_path)
            if record.get("message_type") == "runtime_result"
        }
        for record in _read_jsonl_if_exists(self.inbox_path):
            if record.get("message_type") != "dispatch_task":
                continue
            if record.get("message_id") in answered:
                continue
            if message_id and record.get("message_id") != message_id:
                continue
            return record
        return None

    def _result_message(self, message, runtime_result):
        return {
            "message_id": f"RESULT-{message['message_id']}",
            "from_agent": self.agent_id,
            "to_agent": message["from_agent"],
            "message_type": "runtime_result",
            "correlation_id": message["correlation_id"],
            "created_at": self.clock.now(),
            "payload": {
                "source_message_id": message["message_id"],
                "task_id": message["payload"]["task_id"],
                "attempt_id": message["payload"]["attempt_id"],
                "lease_id": message["payload"]["lease_id"],
                "result_status": runtime_result["result_status"],
                "changed_files": runtime_result["changed_files"],
                "output": runtime_result.get("output", {}),
            },
        }

    def _load_agent(self):
        agent_pool = json.loads(self.agent_pool_path.read_text(encoding="utf-8"))
        for agent in agent_pool.get("agents", []):
            if agent.get("agent_id") == self.agent_id:
                return agent
        raise ValueError(f"agent not found in agent pool: {self.agent_id}")


class FileMailboxRuntimeAdapter:
    def __init__(self, agent_pool_path, output_dir=None, runtime_adapter=None, clock=None):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir) if output_dir else None
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self.clock = clock or SystemClock()

    def bind_output_dir(self, output_dir):
        return FileMailboxRuntimeAdapter(
            self.agent_pool_path,
            output_dir=output_dir,
            runtime_adapter=self.runtime_adapter,
            clock=self.clock,
        )

    def run(self, message, worktree_path=None):
        if not self.output_dir:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "mailbox", "error": "missing_output_dir"},
            }
        worker = FileMailboxWorker(
            self.agent_pool_path,
            self.output_dir,
            message["to_agent"],
            runtime_adapter=self.runtime_adapter,
            clock=self.clock,
        )
        poll_summary = worker.poll_once(
            message_id=message["message_id"],
            worktree_path=worktree_path,
        )
        if poll_summary["poll_status"] != "processed":
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "mailbox", "error": "mailbox_result_missing"},
            }
        return _runtime_result_from_outbox(worker.outbox_path, message["message_id"])


class FileMailboxSubprocessRuntimeAdapter:
    def __init__(
        self,
        agent_pool_path,
        output_dir=None,
        command=None,
        timeout_seconds=60,
        runtime="fake",
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir) if output_dir else None
        self.command = list(command or [sys.executable, "-m", "agentteam_runtime.mailbox_worker"])
        self.timeout_seconds = timeout_seconds
        self.runtime = runtime

    def bind_output_dir(self, output_dir):
        return FileMailboxSubprocessRuntimeAdapter(
            self.agent_pool_path,
            output_dir=output_dir,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            runtime=self.runtime,
        )

    def run(self, message, worktree_path=None):
        if not self.output_dir:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "mailbox_subprocess", "error": "missing_output_dir"},
            }
        command = self._build_command(message, worktree_path=worktree_path)
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "result_status": "timed_out",
                "changed_files": [],
                "output": {
                    "adapter": "mailbox_subprocess",
                    "error": "timeout",
                    "timeout_seconds": self.timeout_seconds,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                },
            }

        if completed.returncode != 0:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {
                    "adapter": "mailbox_subprocess",
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            }
        try:
            worker_summary = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {
                    "adapter": "mailbox_subprocess",
                    "error": "invalid_worker_stdout",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            }

        result = _runtime_result_from_outbox(
            self._outbox_path(message["to_agent"]),
            message["message_id"],
        )
        result["output"] = {
            **result.get("output", {}),
            "mailbox_subprocess": {
                "worker_pid": worker_summary.get("worker_pid"),
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
            },
        }
        return result

    def _build_command(self, message, worktree_path=None):
        command = [
            *self.command,
            "--agent-pool",
            str(self.agent_pool_path),
            "--output-dir",
            str(self.output_dir),
            "--agent-id",
            message["to_agent"],
            "--message-id",
            message["message_id"],
            "--runtime",
            self.runtime,
        ]
        if worktree_path:
            command.extend(["--worktree-path", str(worktree_path)])
        return command

    def _outbox_path(self, agent_id):
        agent_pool = json.loads(self.agent_pool_path.read_text(encoding="utf-8"))
        for agent in agent_pool.get("agents", []):
            if agent.get("agent_id") == agent_id:
                return self.output_dir / agent["outbox_path"]
        raise ValueError(f"agent not found in agent pool: {agent_id}")


def _runtime_result_from_outbox(outbox_path, source_message_id):
    for record in _read_jsonl_if_exists(outbox_path):
        if record.get("message_type") != "runtime_result":
            continue
        payload = record.get("payload", {})
        if payload.get("source_message_id") != source_message_id:
            continue
        return {
            "result_status": payload.get("result_status", "failed"),
            "changed_files": payload.get("changed_files", []),
            "output": payload.get("output", {}),
        }
    return {
        "result_status": "failed",
        "changed_files": [],
        "output": {"adapter": "mailbox", "error": "mailbox_result_missing"},
    }


def _append_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _read_jsonl_if_exists(path):
    path = Path(path)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll one AgentTeam file mailbox worker message.")
    parser.add_argument("--agent-pool", required=True, help="Path to agent pool JSON.")
    parser.add_argument("--output-dir", required=True, help="Directory containing mailbox files.")
    parser.add_argument("--agent-id", required=True, help="Agent id whose mailbox should be polled.")
    parser.add_argument("--message-id", help="Optional dispatch message id to process.")
    parser.add_argument("--worktree-path", help="Optional worktree path for writable runtime work.")
    parser.add_argument(
        "--runtime",
        choices=["fake"],
        default="fake",
        help="Delegate runtime to execute. M14c supports only fake.",
    )
    args = parser.parse_args(argv)

    worker = FileMailboxWorker(
        args.agent_pool,
        args.output_dir,
        args.agent_id,
        runtime_adapter=_runtime_adapter_from_name(args.runtime),
    )
    summary = worker.poll_once(
        message_id=args.message_id,
        worktree_path=args.worktree_path,
    )
    summary["worker_pid"] = os.getpid()
    print(json.dumps(summary, sort_keys=True))
    return 0


def _runtime_adapter_from_name(name):
    if name == "fake":
        return FakeRuntimeAdapter()
    raise ValueError(f"unsupported mailbox worker runtime: {name}")


if __name__ == "__main__":
    raise SystemExit(main())

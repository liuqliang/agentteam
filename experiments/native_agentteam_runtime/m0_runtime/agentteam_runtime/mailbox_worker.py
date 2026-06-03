import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .m0_runtime import CodexRuntimeAdapter, FakeRuntimeAdapter, SystemClock


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
        if worktree_path is None:
            worktree_path = message.get("payload", {}).get("worktree_path")
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

    @classmethod
    def poll_tree_once(
        cls,
        agent_pool_path,
        root_output_dir,
        agent_id,
        runtime_adapter=None,
        clock=None,
    ):
        agent = _load_agent(agent_pool_path, agent_id)
        for output_dir in _candidate_mailbox_output_dirs(root_output_dir, agent):
            worker = cls(
                agent_pool_path,
                output_dir,
                agent_id,
                runtime_adapter=runtime_adapter,
                clock=clock,
            )
            summary = worker.poll_once()
            if summary["poll_status"] == "processed":
                return {
                    **summary,
                    "mailbox_output_dir": str(output_dir),
                }
        return {
            "poll_status": "idle",
            "reason": "no_dispatch_message",
        }


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


class FileMailboxExternalRuntimeAdapter:
    def __init__(
        self,
        agent_pool_path,
        output_dir=None,
        timeout_seconds=60,
        poll_interval_seconds=0.05,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir) if output_dir else None
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def bind_output_dir(self, output_dir):
        return FileMailboxExternalRuntimeAdapter(
            self.agent_pool_path,
            output_dir=output_dir,
            timeout_seconds=self.timeout_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
        )

    def run(self, message, worktree_path=None):
        del worktree_path
        if not self.output_dir:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "mailbox_external", "error": "missing_output_dir"},
            }
        outbox_path = self._outbox_path(message["to_agent"])
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() <= deadline:
            result = _runtime_result_from_outbox(outbox_path, message["message_id"])
            if result["result_status"] != "failed" or result.get("output", {}).get("error") != "mailbox_result_missing":
                result["output"] = {
                    **result.get("output", {}),
                    "mailbox_external": {
                        "outbox_path": str(outbox_path),
                    },
                }
                return result
            time.sleep(self.poll_interval_seconds)
        return {
            "result_status": "timed_out",
            "changed_files": [],
            "output": {
                "adapter": "mailbox_external",
                "error": "timeout",
                "timeout_seconds": self.timeout_seconds,
                "outbox_path": str(outbox_path),
            },
        }

    def _outbox_path(self, agent_id):
        agent = _load_agent(self.agent_pool_path, agent_id)
        return self.output_dir / agent["outbox_path"]


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


class FileMailboxWorkerProcessSupervisor:
    def __init__(
        self,
        agent_pool_path,
        output_dir,
        agent_id,
        command=None,
        env=None,
        poll_interval_seconds=0.05,
        runtime="fake",
        codex_command=None,
        codex_model=None,
        codex_sandbox="workspace-write",
        codex_timeout_seconds=300,
    ):
        if runtime not in {"fake", "codex"}:
            raise ValueError(f"unsupported mailbox worker runtime: {runtime}")
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.agent_id = agent_id
        self.command = list(command or [sys.executable, "-m", "agentteam_runtime.mailbox_worker"])
        self.env = env
        self.poll_interval_seconds = poll_interval_seconds
        self.runtime = runtime
        self.codex_command = list(codex_command) if codex_command else None
        self.codex_model = codex_model
        self.codex_sandbox = codex_sandbox
        self.codex_timeout_seconds = codex_timeout_seconds
        self.stop_file = self.output_dir / "state" / "workers" / f"{agent_id}.stop"
        self.process = None

    def start(self):
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        if self.stop_file.exists():
            self.stop_file.unlink()
        command = [
            *self.command,
            "--agent-pool",
            str(self.agent_pool_path),
            "--output-dir",
            str(self.output_dir),
            "--agent-id",
            self.agent_id,
            "--runtime",
            self.runtime,
            "--serve",
            "--poll-interval-seconds",
            str(self.poll_interval_seconds),
            "--stop-file",
            str(self.stop_file),
        ]
        if self.runtime == "codex":
            command.extend(
                [
                    "--codex-sandbox",
                    self.codex_sandbox,
                    "--codex-timeout-seconds",
                    str(self.codex_timeout_seconds),
                ]
            )
            if self.codex_model:
                command.extend(["--codex-model", self.codex_model])
            if self.codex_command:
                command.extend(["--codex-command-json", json.dumps(self.codex_command)])
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        return {
            "worker_status": "running",
            "worker_pid": self.process.pid,
            "worker_agent_id": self.agent_id,
            "worker_runtime": self.runtime,
            "stop_file": str(self.stop_file),
        }

    def stop(self, timeout_seconds=5):
        if not self.process:
            return {
                "worker_status": "not_started",
                "worker_pid": None,
            }
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text("stop\n", encoding="utf-8")
        try:
            stdout, stderr = self.process.communicate(timeout=timeout_seconds)
            stopped_by = "stop_file"
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                stdout, stderr = self.process.communicate(timeout=timeout_seconds)
                stopped_by = "terminated"
            except subprocess.TimeoutExpired:
                self.process.kill()
                stdout, stderr = self.process.communicate()
                stopped_by = "killed"
        return {
            "worker_status": "stopped",
            "worker_pid": self.process.pid,
            "worker_agent_id": self.agent_id,
            "worker_runtime": self.runtime,
            "exit_code": self.process.returncode,
            "stopped_by": stopped_by,
            "stdout": stdout,
            "stderr": stderr,
        }


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


def _load_agent(agent_pool_path, agent_id):
    agent_pool = json.loads(Path(agent_pool_path).read_text(encoding="utf-8"))
    for agent in agent_pool.get("agents", []):
        if agent.get("agent_id") == agent_id:
            return agent
    raise ValueError(f"agent not found in agent pool: {agent_id}")


def _candidate_mailbox_output_dirs(root_output_dir, agent):
    root_output_dir = Path(root_output_dir)
    inbox_relative_path = Path(agent["inbox_path"])
    candidates = []
    if (root_output_dir / inbox_relative_path).exists():
        candidates.append(root_output_dir)
    steps_dir = root_output_dir / "steps"
    if steps_dir.exists():
        for step_dir in sorted(path for path in steps_dir.iterdir() if path.is_dir()):
            if (step_dir / inbox_relative_path).exists():
                candidates.append(step_dir)
    return candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll one AgentTeam file mailbox worker message.")
    parser.add_argument("--agent-pool", required=True, help="Path to agent pool JSON.")
    parser.add_argument("--output-dir", required=True, help="Directory containing mailbox files.")
    parser.add_argument("--agent-id", required=True, help="Agent id whose mailbox should be polled.")
    parser.add_argument("--message-id", help="Optional dispatch message id to process.")
    parser.add_argument("--worktree-path", help="Optional worktree path for writable runtime work.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Keep polling root and step mailboxes until --stop-file exists.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=0.05,
        help="Polling interval for --serve mode.",
    )
    parser.add_argument("--stop-file", help="Path that stops --serve mode when present.")
    parser.add_argument(
        "--runtime",
        choices=["fake", "codex"],
        default="fake",
        help="Delegate runtime to execute.",
    )
    parser.add_argument(
        "--codex-command-json",
        help="JSON string array command prefix for CodexRuntimeAdapter.",
    )
    parser.add_argument("--codex-model", help="Optional model passed to CodexRuntimeAdapter.")
    parser.add_argument(
        "--codex-sandbox",
        default="workspace-write",
        help="Sandbox mode passed to CodexRuntimeAdapter.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=300,
        help="CodexRuntimeAdapter timeout in seconds.",
    )
    args = parser.parse_args(argv)
    runtime_adapter = _runtime_adapter_from_args(parser, args)

    if args.serve:
        stop_file = Path(args.stop_file or Path(args.output_dir) / "state" / "workers" / f"{args.agent_id}.stop")
        processed_count = 0
        last_summary = None
        while not stop_file.exists():
            summary = FileMailboxWorker.poll_tree_once(
                args.agent_pool,
                args.output_dir,
                args.agent_id,
                runtime_adapter=runtime_adapter,
            )
            last_summary = summary
            if summary["poll_status"] == "processed":
                processed_count += 1
            time.sleep(args.poll_interval_seconds)
        print(
            json.dumps(
                {
                    "serve_status": "stopped",
                    "processed_count": processed_count,
                    "last_summary": last_summary,
                    "worker_pid": os.getpid(),
                },
                sort_keys=True,
            )
        )
        return 0

    worker = FileMailboxWorker(
        args.agent_pool,
        args.output_dir,
        args.agent_id,
        runtime_adapter=runtime_adapter,
    )
    summary = worker.poll_once(
        message_id=args.message_id,
        worktree_path=args.worktree_path,
    )
    summary["worker_pid"] = os.getpid()
    print(json.dumps(summary, sort_keys=True))
    return 0


def _runtime_adapter_from_args(parser, args):
    if args.runtime == "fake":
        if (
            args.codex_command_json
            or args.codex_model
            or args.codex_sandbox != "workspace-write"
            or args.codex_timeout_seconds != 300
        ):
            parser.error("Codex runtime options require --runtime codex")
        return FakeRuntimeAdapter()
    if args.runtime == "codex":
        if args.codex_timeout_seconds < 1:
            parser.error("--codex-timeout-seconds must be at least 1")
        return CodexRuntimeAdapter(
            command=_parse_command_json(parser, args.codex_command_json),
            model=args.codex_model,
            sandbox=args.codex_sandbox,
            timeout_seconds=args.codex_timeout_seconds,
        )
    raise ValueError(f"unsupported mailbox worker runtime: {args.runtime}")


def _parse_command_json(parser, raw_command):
    if not raw_command:
        return None
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError as exc:
        parser.error(f"--codex-command-json must be valid JSON: {exc}")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        parser.error("--codex-command-json must be a non-empty JSON string array")
    return command


if __name__ == "__main__":
    raise SystemExit(main())

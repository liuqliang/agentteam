import argparse
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .m0_runtime import CodexRuntimeAdapter, FakeRuntimeAdapter, SystemClock


IDLE_HEARTBEAT_MIN_INTERVAL_SECONDS = 15
HEARTBEAT_PROGRESS_MAX_CHARS = 220


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
        self.heartbeat_path = (
            self.output_dir / "state" / "workers" / f"{self.agent_id}.heartbeat.json"
        )

    def poll_once(self, message_id=None, worktree_path=None):
        message = self._next_dispatch(message_id=message_id)
        if not message:
            self._write_heartbeat(
                activity="idle",
                poll_status="idle",
                reason="no_dispatch_message",
            )
            return {
                "poll_status": "idle",
                "reason": "no_dispatch_message",
            }
        if worktree_path is None:
            worktree_path = message.get("payload", {}).get("worktree_path")
        self._write_heartbeat(
            activity="processing",
            poll_status="processing",
            message=message,
            worktree_path=worktree_path,
        )

        def progress_callback():
            self._write_heartbeat(
                activity="processing",
                poll_status="processing",
                message=message,
                worktree_path=worktree_path,
            )

        runtime_result = _run_runtime_adapter(
            self.runtime_adapter,
            message,
            worktree_path=worktree_path,
            progress_callback=progress_callback,
        )
        result_message = self._result_message(message, runtime_result)
        _append_jsonl(self.outbox_path, [result_message])
        self._write_heartbeat(
            activity="processed",
            poll_status="processed",
            message=message,
            runtime_result=runtime_result,
            worktree_path=worktree_path,
        )
        return {
            "poll_status": "processed",
            "source_message_id": message["message_id"],
            "result_status": runtime_result["result_status"],
            "changed_files": runtime_result["changed_files"],
            "outbox_path": str(self.outbox_path),
        }

    def _write_heartbeat(
        self,
        *,
        activity,
        poll_status,
        reason=None,
        message=None,
        runtime_result=None,
        worktree_path=None,
    ):
        if activity == "idle" and not self._should_write_idle_heartbeat():
            return
        payload = {
            "heartbeat_schema_version": "worker_heartbeat.v1",
            "worker_agent_id": self.agent_id,
            "worker_pid": os.getpid(),
            "updated_at": self.clock.now(),
            "activity": activity,
            "poll_status": poll_status,
            "reason": reason,
            "worktree_path": str(worktree_path) if worktree_path else None,
        }
        progress_summary = _heartbeat_progress_summary(
            activity,
            reason=reason,
            message=message,
            runtime_result=runtime_result,
        )
        if progress_summary:
            payload["progress_summary"] = progress_summary
        if message:
            message_payload = message.get("payload", {})
            payload.update(
                {
                    "source_message_id": message.get("message_id"),
                    "task_id": message_payload.get("task_id"),
                    "attempt_id": message_payload.get("attempt_id"),
                    "lease_id": message_payload.get("lease_id"),
                }
            )
        if isinstance(runtime_result, dict):
            changed_files = runtime_result.get("changed_files", [])
            payload.update(
                {
                    "result_status": runtime_result.get("result_status"),
                    "changed_file_count": (
                        len(changed_files) if isinstance(changed_files, list) else 0
                    ),
                }
            )
        _write_json_best_effort(self.heartbeat_path, payload)

    def _should_write_idle_heartbeat(self):
        try:
            modified_at = self.heartbeat_path.stat().st_mtime
        except OSError:
            return True
        return (time.time() - modified_at) >= IDLE_HEARTBEAT_MIN_INTERVAL_SECONDS

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
        payload = {
            "source_message_id": message["message_id"],
            "task_id": message["payload"]["task_id"],
            "attempt_id": message["payload"]["attempt_id"],
            "lease_id": message["payload"]["lease_id"],
            "result_status": runtime_result["result_status"],
            "changed_files": runtime_result["changed_files"],
            "output": runtime_result.get("output", {}),
        }
        if isinstance(runtime_result.get("token_usage"), dict):
            payload["token_usage"] = runtime_result["token_usage"]
        if isinstance(runtime_result.get("usage"), dict):
            payload["usage"] = runtime_result["usage"]
        return {
            "message_id": f"RESULT-{message['message_id']}",
            "from_agent": self.agent_id,
            "to_agent": message["from_agent"],
            "message_type": "runtime_result",
            "correlation_id": message["correlation_id"],
            "created_at": self.clock.now(),
            "payload": payload,
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
        worker = cls(
            agent_pool_path,
            root_output_dir,
            agent_id,
            runtime_adapter=runtime_adapter,
            clock=clock,
        )
        worker._write_heartbeat(
            activity="idle",
            poll_status="idle",
            reason="no_dispatch_message",
        )
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
        return _with_worktree_salvage(
            {
                "result_status": "timed_out",
                "changed_files": [],
                "output": {
                    "adapter": "mailbox_external",
                    "error": "timeout",
                    "timeout_seconds": self.timeout_seconds,
                    "outbox_path": str(outbox_path),
                },
            },
            worktree_path,
            reason="timeout",
        )

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
            return _with_worktree_salvage(
                {
                    "result_status": "timed_out",
                    "changed_files": [],
                    "output": {
                        "adapter": "mailbox_subprocess",
                        "error": "timeout",
                        "timeout_seconds": self.timeout_seconds,
                        "stdout": _process_text(exc.stdout),
                        "stderr": _process_text(exc.stderr),
                    },
                },
                worktree_path,
                reason="timeout",
            )

        if completed.returncode != 0:
            return _with_worktree_salvage(
                {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "adapter": "mailbox_subprocess",
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    },
                },
                worktree_path,
                reason="process_failed",
            )
        try:
            worker_summary = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return _with_worktree_salvage(
                {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "adapter": "mailbox_subprocess",
                        "error": "invalid_worker_stdout",
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    },
                },
                worktree_path,
                reason="invalid_worker_stdout",
            )

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
        if result["result_status"] == "failed" and result.get("output", {}).get("error") == "mailbox_result_missing":
            return _with_worktree_salvage(
                result,
                worktree_path,
                reason="mailbox_result_missing",
            )
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
        codex_fallback_worktree_path=None,
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
        self.codex_fallback_worktree_path = codex_fallback_worktree_path
        self.stop_file = self.output_dir / "state" / "workers" / f"{agent_id}.stop"
        self.process = None
        self.attached_pid = None
        self.attached_exit_code = None

    def attach_existing_process(self, pid, stop_file=None):
        self.process = None
        self.attached_pid = int(pid)
        self.attached_exit_code = None
        if stop_file:
            self.stop_file = Path(stop_file)
        return self.health()

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
            if self.codex_fallback_worktree_path:
                command.extend(
                    [
                        "--codex-fallback-worktree-path",
                        str(self.codex_fallback_worktree_path),
                    ]
                )
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

    def health(self):
        if not self.process:
            if self.attached_pid:
                worker_status = (
                    "running"
                    if self.attached_exit_code is None
                    and _pid_is_running(self.attached_pid)
                    else "exited"
                )
                return {
                    "worker_status": worker_status,
                    "worker_pid": self.attached_pid,
                    "worker_agent_id": self.agent_id,
                    "worker_runtime": self.runtime,
                    "exit_code": self.attached_exit_code,
                    "stop_file": str(self.stop_file),
                    "attached": True,
                }
            return {
                "worker_status": "not_started",
                "worker_pid": None,
                "worker_agent_id": self.agent_id,
                "worker_runtime": self.runtime,
                "exit_code": None,
                "stop_file": str(self.stop_file),
            }
        exit_code = self.process.poll()
        return {
            "worker_status": "running" if exit_code is None else "exited",
            "worker_pid": self.process.pid,
            "worker_agent_id": self.agent_id,
            "worker_runtime": self.runtime,
            "exit_code": exit_code,
            "stop_file": str(self.stop_file),
        }

    def restart_if_exited(self):
        previous_worker = self.health()
        if previous_worker["worker_status"] == "running":
            return {
                "restart_status": "not_needed",
                "previous_worker": previous_worker,
                "new_worker": previous_worker,
            }
        if self.process:
            self.process.communicate()
        return {
            "restart_status": "restarted",
            "previous_worker": previous_worker,
            "new_worker": self.start(),
        }

    def stop(self, timeout_seconds=5):
        if not self.process:
            if self.attached_pid:
                return self._stop_attached(timeout_seconds=timeout_seconds)
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
            "stop_file": str(self.stop_file),
            "stdout": stdout,
            "stderr": stderr,
        }

    def _stop_attached(self, timeout_seconds=5):
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text("stop\n", encoding="utf-8")
        exit_code = _wait_for_pid_exit(self.attached_pid, timeout_seconds)
        stopped_by = "stop_file"
        if exit_code is None and _pid_is_running(self.attached_pid):
            os.kill(self.attached_pid, signal.SIGTERM)
            exit_code = _wait_for_pid_exit(self.attached_pid, timeout_seconds)
            stopped_by = "terminated"
        if exit_code is None and _pid_is_running(self.attached_pid):
            os.kill(self.attached_pid, signal.SIGKILL)
            exit_code = _wait_for_pid_exit(self.attached_pid, timeout_seconds)
            stopped_by = "killed"
        self.attached_exit_code = exit_code
        return {
            "worker_status": "stopped" if exit_code is not None else "stop_requested",
            "worker_pid": self.attached_pid,
            "worker_agent_id": self.agent_id,
            "worker_runtime": self.runtime,
            "exit_code": exit_code,
            "stopped_by": stopped_by,
            "stop_file": str(self.stop_file),
            "attached": True,
            "stdout": "",
            "stderr": "",
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
            "usage": payload.get("usage"),
            "token_usage": payload.get("token_usage"),
        }
    return {
        "result_status": "failed",
        "changed_files": [],
        "output": {"adapter": "mailbox", "error": "mailbox_result_missing"},
    }


def _with_worktree_salvage(result, worktree_path, reason):
    changed_files = _worktree_changed_files(worktree_path)
    if not changed_files:
        return result

    declared_files = result.get("changed_files", [])
    if not isinstance(declared_files, list):
        declared_files = []
    all_changed_files = sorted(
        set(changed_files).union(
            path for path in declared_files if isinstance(path, str)
        )
    )
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    output = {
        **output,
        "summary": (
            "Patch available from worker worktree after timeout or missing outbox; "
            "not accepted automatically."
        ),
        "salvage": {
            "salvage_status": "patch_available",
            "reason": reason,
            "changed_files": all_changed_files,
            "review_required": True,
            "integration_status": "not_integrated",
            "accepted_automatically": False,
        },
    }
    return {
        **result,
        "changed_files": all_changed_files,
        "output": output,
    }


def _worktree_changed_files(worktree_path):
    if not worktree_path:
        return []
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    changed_files = []
    for line in completed.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(".agentteam/"):
            continue
        changed_files.append(path)
    return sorted(set(changed_files))


def _process_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _heartbeat_progress_summary(
    activity,
    *,
    reason=None,
    message=None,
    runtime_result=None,
):
    payload = message.get("payload", {}) if isinstance(message, dict) else {}
    task_id = _compact_progress_text(payload.get("task_id"))
    attempt_id = _compact_progress_text(payload.get("attempt_id"))
    objective = _compact_progress_text(payload.get("objective"), max_chars=140)
    parts = [str(activity)]
    if task_id:
        parts.append(task_id)
    if activity == "processing" and attempt_id:
        parts.append(f"attempt={attempt_id}")
    if activity == "processed" and isinstance(runtime_result, dict):
        result_status = _compact_progress_text(runtime_result.get("result_status"))
        if result_status:
            parts.append(f"result={result_status}")
        changed_files = runtime_result.get("changed_files")
        if isinstance(changed_files, list):
            parts.append(f"changed_files={len(changed_files)}")
        return _compact_progress_text(" ".join(parts))
    if objective:
        return _compact_progress_text(f"{' '.join(parts)}: {objective}")
    if reason:
        return _compact_progress_text(f"{' '.join(parts)}: {reason}")
    return _compact_progress_text(" ".join(parts))


def _compact_progress_text(value, *, max_chars=HEARTBEAT_PROGRESS_MAX_CHARS):
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _append_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _write_json_best_effort(path, payload):
    tmp_path = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        )
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return


def _run_runtime_adapter(runtime_adapter, message, *, worktree_path, progress_callback):
    if _runtime_adapter_accepts_progress_callback(runtime_adapter):
        return runtime_adapter.run(
            message,
            worktree_path=worktree_path,
            progress_callback=progress_callback,
        )
    return runtime_adapter.run(message, worktree_path=worktree_path)


def _runtime_adapter_accepts_progress_callback(runtime_adapter):
    try:
        signature = inspect.signature(runtime_adapter.run)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == "progress_callback"
        for parameter in signature.parameters.values()
    )


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


def _pid_is_running(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pid_exit(pid, timeout_seconds):
    pid = int(pid)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited_pid = 0
            status = 0
        if waited_pid == pid:
            try:
                return os.waitstatus_to_exitcode(status)
            except ValueError:
                return 0
        if not _pid_is_running(pid):
            return 0
        time.sleep(0.05)
    return None


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
    parser.add_argument(
        "--codex-fallback-worktree-path",
        help="Fallback worktree path for Codex tasks without writable worktrees.",
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
            fallback_worktree_path=args.codex_fallback_worktree_path,
            output_dir=args.output_dir,
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

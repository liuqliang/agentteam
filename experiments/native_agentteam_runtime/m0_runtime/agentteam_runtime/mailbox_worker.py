import argparse
import fcntl
import hashlib
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path

from .m0_runtime import CodexRuntimeAdapter, FakeRuntimeAdapter, SystemClock
from .model_routing import validate_model_route
from .model_context_budget import replace_codex_context_policy_arguments


IDLE_HEARTBEAT_MIN_INTERVAL_SECONDS = 15
HEARTBEAT_PROGRESS_MAX_CHARS = 220
MODEL_INVOCATION_CONTEXT_FIELDS = (
    "project",
    "run_id",
    "pursue_id",
    "round_index",
    "taskpack_id",
    "implementation_run_id",
    "gate_epoch",
    "task_id",
    "attempt_id",
    "runtime_execution_session_id",
    "requested_provider_session_id",
    "provider_resume_mode",
    "provider_predecessor_invocation_id",
    "provider_predecessor_turn_id",
    "provider_predecessor_usage_snapshot",
    "lifecycle_owner_token",
    "lease_id",
    "agent_id",
    "agent_role",
    "required_role",
    "usage_stage",
    "model_routing",
    "tool_budget_routing",
    "provider_usage_scope",
    "provider_session_lock_held",
    "provider_project_binding_valid",
    "provider_lineage_status",
    "previous_provider_session_id",
    "previous_provider_turn_id",
    "previous_invocation_id",
    "experiment_sandbox_reference",
    "experiment_sandbox_required",
    "experiment_authority_root",
    "experiment_controller_reference",
    "experiment_controller_required",
)


class ProviderSessionProjectBindingError(RuntimeError):
    """Raised before launch when a provider session is bound to another project."""


class MailboxDispatchIntegrityError(RuntimeError):
    """Raised when a dispatch differs from scheduler-published authority."""


class ProviderSessionCoordinator:
    """Serialize cumulative provider-session writers in a user-shared namespace."""

    def __init__(self, state_root=None):
        self.state_root = Path(state_root) if state_root else _provider_session_state_root()

    @contextmanager
    def prepare_message(self, message, runtime_adapter, *, authority_root):
        prepared = deepcopy(message)
        payload = prepared.setdefault("payload", {})
        resume_mode, requested_session = _provider_resume_request(
            payload,
            runtime_adapter,
        )
        payload["provider_resume_mode"] = resume_mode
        payload["requested_provider_session_id"] = requested_session
        if resume_mode == "new":
            yield prepared
            return

        domain_dir = self.state_root / _provider_session_namespace(payload, None)
        namespace_dir = (
            self.state_root
            / _provider_session_namespace(payload, requested_session)
            if resume_mode == "explicit"
            else domain_dir
        )
        domain_lock_mode = (
            fcntl.LOCK_SH if resume_mode == "explicit" else fcntl.LOCK_EX
        )
        with _provider_lock(domain_dir / "writer.lock", domain_lock_mode):
            session_lock = (
                _provider_lock(namespace_dir / "writer.lock", fcntl.LOCK_EX)
                if resume_mode == "explicit"
                else nullcontext()
            )
            with session_lock:
                binding = _bind_provider_session_project(
                    namespace_dir,
                    payload,
                    authority_root=authority_root,
                )
                context = {
                    "provider_resume_mode": resume_mode,
                    "requested_provider_session_id": requested_session,
                    "provider_session_lock_held": True,
                    "provider_project_binding_valid": True,
                    "provider_usage_scope": "session_cumulative",
                }
                if resume_mode == "explicit":
                    context.update(
                        _provider_predecessor_context(
                            binding["lifecycle_authority_root"],
                            requested_session,
                        )
                    )
                else:
                    context.update(
                        {
                            "provider_lineage_status": "ambiguous_resume_last",
                            "provider_predecessor_invocation_id": None,
                            "provider_predecessor_turn_id": None,
                            "provider_predecessor_usage_snapshot": None,
                            "previous_provider_session_id": None,
                            "previous_provider_turn_id": None,
                            "previous_invocation_id": None,
                        }
                    )
                _update_model_invocation_context(payload, context)
                yield prepared


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
        if (
            self.output_dir
            / "state"
            / "mailbox_dispatch_authority"
        ).is_dir():
            _validate_dispatch_authority(self.output_dir, message)
        lock_digest = hashlib.sha256(
            message["message_id"].encode("utf-8")
        ).hexdigest()
        lock_path = (
            self.output_dir
            / "state"
            / "mailbox_dispatch_locks"
            / f"{lock_digest}.lock"
        )
        with _provider_lock(lock_path, fcntl.LOCK_EX):
            replayed_result = _runtime_result_record(
                self.outbox_path,
                message["message_id"],
            )
            if replayed_result is not None:
                return {
                    "poll_status": "processed",
                    "source_message_id": message["message_id"],
                    "result_status": replayed_result["result_status"],
                    "changed_files": replayed_result["changed_files"],
                    "outbox_path": str(self.outbox_path),
                    "replayed_from_outbox": True,
                }
            return self._process_dispatch(
                message,
                worktree_path=worktree_path,
            )

    def _process_dispatch(self, message, *, worktree_path=None):
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

        runtime_adapter = _runtime_adapter_for_dispatch(
            self.runtime_adapter,
            message,
        )

        authority_root = _invocation_authority_root(
            message,
            runtime_adapter,
            self.output_dir,
        )
        replay = _replay_model_invocation_result(
            authority_root,
            message,
            worktree_path=worktree_path,
        )
        if replay and replay.get("recovery_status") == "open":
            self._write_heartbeat(
                activity="processing",
                poll_status="waiting_recovery",
                reason="open_model_invocation_requires_scheduler_reconciliation",
                message=message,
                worktree_path=worktree_path,
            )
            return {
                "poll_status": "waiting_recovery",
                "source_message_id": message["message_id"],
                "invocation_id": replay["invocation_id"],
                "outbox_path": str(self.outbox_path),
            }

        if replay:
            runtime_message = message
            runtime_result = replay["runtime_result"]
        else:
            try:
                coordinator = ProviderSessionCoordinator(
                    message.get("payload", {}).get("provider_session_state_root")
                )
                with coordinator.prepare_message(
                    message,
                    runtime_adapter,
                    authority_root=authority_root,
                ) as runtime_message:
                    runtime_result = _run_runtime_adapter(
                        runtime_adapter,
                        runtime_message,
                        worktree_path=worktree_path,
                        progress_callback=progress_callback,
                    )
            except ProviderSessionProjectBindingError as exc:
                runtime_message = message
                runtime_result = {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "adapter": "mailbox",
                        "error": "provider_session_project_binding_conflict",
                        "reason": str(exc),
                    },
                }
        runtime_result = _reconcile_runtime_changed_files(
            runtime_result,
            worktree_path,
            expected_output_artifacts=runtime_message.get(
                "payload",
                {},
            ).get("expected_output_artifacts", []),
        )
        result_message = self._result_message(runtime_message, runtime_result)
        _append_jsonl(self.outbox_path, [result_message])
        self._write_heartbeat(
            activity="processed",
            poll_status="processed",
            message=runtime_message,
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
        invocation_context = _model_invocation_context_payload(message)
        payload = {
            "source_message_id": message["message_id"],
            "task_id": message["payload"]["task_id"],
            "attempt_id": message["payload"]["attempt_id"],
            "lease_id": message["payload"]["lease_id"],
            "result_status": runtime_result["result_status"],
            "changed_files": runtime_result["changed_files"],
            "output": runtime_result.get("output", {}),
        }
        if invocation_context:
            payload["model_invocation_context"] = invocation_context
        model_invocation = runtime_result.get("output", {}).get("model_invocation")
        if isinstance(model_invocation, dict):
            payload["model_invocation"] = deepcopy(model_invocation)
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


def _runtime_adapter_for_dispatch(runtime_adapter, message):
    route = validate_model_route(
        (message.get("payload") or {}).get("model_routing")
    )
    if route is None or not isinstance(runtime_adapter, CodexRuntimeAdapter):
        return runtime_adapter
    tool_route = (message.get("payload") or {}).get("tool_budget_routing")
    command = runtime_adapter.command
    if isinstance(tool_route, dict) and isinstance(tool_route.get("policy"), dict):
        command = replace_codex_context_policy_arguments(
            command,
            tool_route["policy"],
        )
    return CodexRuntimeAdapter(
        command=command,
        model=route["model"],
        reasoning_profile=route["reasoning_profile"],
        sandbox=runtime_adapter.sandbox,
        timeout_seconds=runtime_adapter.timeout_seconds,
        extra_args=runtime_adapter.extra_args,
        fallback_worktree_path=runtime_adapter.fallback_worktree_path,
        output_dir=runtime_adapter.output_dir,
        progress_interval_seconds=runtime_adapter.progress_interval_seconds,
        resume_session_id=runtime_adapter.resume_session_id,
        resume_last=runtime_adapter.resume_last,
        systemd_runner_factory=runtime_adapter.systemd_runner_factory,
    )


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
        codex_reasoning_profile=None,
        codex_sandbox="workspace-write",
        codex_timeout_seconds=300,
        codex_fallback_worktree_path=None,
        codex_resume_session_id=None,
        codex_resume_last=False,
    ):
        if codex_resume_session_id and codex_resume_last:
            raise ValueError("codex_resume_session_id and codex_resume_last are mutually exclusive")
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
        self.codex_reasoning_profile = codex_reasoning_profile
        self.codex_sandbox = codex_sandbox
        self.codex_timeout_seconds = codex_timeout_seconds
        self.codex_fallback_worktree_path = codex_fallback_worktree_path
        self.codex_resume_session_id = codex_resume_session_id
        self.codex_resume_last = bool(codex_resume_last)
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
            if self.codex_reasoning_profile:
                command.extend(
                    [
                        "--codex-reasoning-profile",
                        self.codex_reasoning_profile,
                    ]
                )
            if self.codex_command:
                command.extend(["--codex-command-json", json.dumps(self.codex_command)])
            if self.codex_fallback_worktree_path:
                command.extend(
                    [
                        "--codex-fallback-worktree-path",
                        str(self.codex_fallback_worktree_path),
                    ]
                )
            if self.codex_resume_session_id:
                command.extend(["--codex-resume-session-id", self.codex_resume_session_id])
            if self.codex_resume_last:
                command.append("--codex-resume-last")
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
    result = _runtime_result_record(outbox_path, source_message_id)
    if result is not None:
        return result
    return {
        "result_status": "failed",
        "changed_files": [],
        "output": {"adapter": "mailbox", "error": "mailbox_result_missing"},
    }


def _runtime_result_record(outbox_path, source_message_id):
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
            "model_invocation_context": payload.get("model_invocation_context"),
            "model_invocation": payload.get("model_invocation"),
        }
    return None


def _provider_session_state_root():
    configured = os.environ.get("AGENTTEAM_PROVIDER_SESSION_STATE_ROOT")
    if configured:
        return Path(configured)
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "agentteam" / "provider_sessions"
    return Path.home() / ".local" / "state" / "agentteam" / "provider_sessions"


@contextmanager
def _provider_lock(lock_path, mode):
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+b") as lock_file:
        fcntl.flock(lock_file.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _provider_resume_request(payload, runtime_adapter):
    requested = payload.get("requested_provider_session_id")
    adapter_requested = getattr(runtime_adapter, "resume_session_id", None)
    if requested and adapter_requested and requested != adapter_requested:
        raise ProviderSessionProjectBindingError(
            "dispatch requested_provider_session_id does not match worker runtime profile"
        )
    requested = requested or adapter_requested
    resume_last = bool(
        payload.get("provider_resume_mode") == "resume_last"
        or getattr(runtime_adapter, "resume_last", False)
    )
    if requested and resume_last:
        raise ProviderSessionProjectBindingError(
            "explicit provider session and resume_last are mutually exclusive"
        )
    if requested:
        return "explicit", str(requested)
    if resume_last:
        return "resume_last", None
    return "new", None


def _provider_session_namespace(payload, requested_session):
    identity = {
        "backend": payload.get("backend") or "codex",
        "backend_account": payload.get("provider_backend_account") or "default",
        "session_store": payload.get("provider_session_store") or "default",
        "provider_session": requested_session or "__resume_last_domain__",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest


def _bind_provider_session_project(namespace_dir, payload, *, authority_root):
    binding_path = namespace_dir / "project_binding.json"
    project_identity = str(
        payload.get("provider_project_identity")
        or payload.get("project_root")
        or payload.get("project")
        or "agentteam"
    )
    lifecycle_root = Path(
        payload.get("provider_project_lifecycle_root") or authority_root
    ).resolve()
    candidate = {
        "binding_schema_version": "provider_session_project_binding.v1",
        "project_identity": project_identity,
        "lifecycle_authority_root": str(lifecycle_root),
    }
    if binding_path.exists():
        existing = json.loads(binding_path.read_text(encoding="utf-8"))
        if existing.get("project_identity") != project_identity:
            raise ProviderSessionProjectBindingError(
                "provider session is already bound to project "
                f"{existing.get('project_identity')!r}; cross-project resume rejected"
            )
        return existing
    data = json.dumps(candidate, sort_keys=True).encode("utf-8")
    descriptor = os.open(
        binding_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            binding_path.unlink()
        except OSError:
            pass
        raise
    return candidate


def _provider_predecessor_context(lifecycle_root, provider_session_id):
    terminals = []
    root = Path(lifecycle_root)
    if root.exists():
        for path in sorted(root.glob("**/model_invocations/*/terminal.json")):
            try:
                terminal = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if terminal.get("provider_session_id") != provider_session_id:
                continue
            turn_id = terminal.get("provider_turn_id")
            invocation_id = terminal.get("invocation_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            if not isinstance(invocation_id, str) or not invocation_id:
                continue
            terminals.append((path, terminal))

    referenced_turn_ids = {
        terminal.get("provider_predecessor_turn_id")
        for _, terminal in terminals
        if terminal.get("provider_predecessor_turn_id")
    }
    tips = [
        item
        for item in terminals
        if item[1].get("provider_turn_id") not in referenced_turn_ids
    ]
    if len(tips) != 1:
        return {
            "provider_lineage_status": "unresolved",
            "provider_predecessor_invocation_id": None,
            "provider_predecessor_turn_id": None,
            "provider_predecessor_usage_snapshot": None,
            "previous_provider_session_id": None,
            "previous_provider_turn_id": None,
            "previous_invocation_id": None,
        }

    terminal_path, terminal = tips[0]
    snapshot = _provider_usage_snapshot(
        terminal_path.with_name("stdout.jsonl"),
        provider_session_id,
    )
    if snapshot is None:
        return {
            "provider_lineage_status": "unresolved",
            "provider_predecessor_invocation_id": None,
            "provider_predecessor_turn_id": None,
            "provider_predecessor_usage_snapshot": None,
            "previous_provider_session_id": None,
            "previous_provider_turn_id": None,
            "previous_invocation_id": None,
        }
    return {
        "provider_lineage_status": "authoritative",
        "provider_predecessor_invocation_id": terminal["invocation_id"],
        "provider_predecessor_turn_id": terminal["provider_turn_id"],
        "provider_predecessor_usage_snapshot": snapshot,
        "previous_provider_session_id": provider_session_id,
        "previous_provider_turn_id": terminal["provider_turn_id"],
        "previous_invocation_id": terminal["invocation_id"],
    }


def _provider_usage_snapshot(stdout_path, provider_session_id):
    try:
        lines = stdout_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    snapshot = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("usage"), dict):
            continue
        event_session = event.get("provider_session_id")
        if event_session not in {None, provider_session_id}:
            continue
        usage = event["usage"]
        candidate = {
            field: usage.get(field, 0)
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        }
        if all(isinstance(value, int) and value >= 0 for value in candidate.values()):
            snapshot = candidate
    return snapshot


def _update_model_invocation_context(payload, context):
    payload.update(deepcopy(context))
    nested = payload.get("model_invocation_context")
    if isinstance(nested, dict):
        payload["model_invocation_context"] = {
            **nested,
            **deepcopy(context),
        }


def _model_invocation_context_payload(message):
    payload = message.get("payload", {})
    nested = payload.get("model_invocation_context")
    merged = dict(payload)
    if isinstance(nested, dict):
        merged.update(nested)
    return {
        field: deepcopy(merged.get(field))
        for field in MODEL_INVOCATION_CONTEXT_FIELDS
        if field in merged
    }


def _invocation_authority_root(message, runtime_adapter, fallback):
    payload = message.get("payload", {})
    configured = payload.get("model_invocation_authority_root")
    if configured:
        return Path(configured)
    adapter_root = getattr(runtime_adapter, "output_dir", None)
    if adapter_root:
        return Path(adapter_root)
    return Path(fallback)


def _replay_model_invocation_result(authority_root, message, *, worktree_path):
    payload = message.get("payload", {})
    attempt_id = payload.get("attempt_id")
    owner_token = payload.get("lifecycle_owner_token") or payload.get("lease_id")
    agent_id = payload.get("agent_id") or message.get("to_agent")
    matches = []
    invocation_root = Path(authority_root) / "model_invocations"
    if not invocation_root.exists():
        return None
    for started_path in sorted(invocation_root.glob("*/started.json")):
        try:
            start = json.loads(started_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if start.get("attempt_id") != attempt_id:
            continue
        if start.get("lifecycle_owner_token") != owner_token:
            continue
        if agent_id and start.get("agent_id") not in {None, agent_id}:
            continue
        matches.append((started_path, start))
    if not matches:
        return None
    if len(matches) != 1:
        return {
            "recovery_status": "open",
            "invocation_id": None,
            "reason": "multiple_model_invocations_for_dispatch",
        }
    started_path, start = matches[0]
    terminal_path = started_path.with_name("terminal.json")
    if not terminal_path.exists():
        return {
            "recovery_status": "open",
            "invocation_id": start["invocation_id"],
            "reason": "open_model_invocation",
        }
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    terminal_status = terminal.get("terminal_status")
    result_status = (
        terminal_status
        if terminal_status in {"completed", "failed", "blocked", "cancelled", "timed_out"}
        else "failed"
    )
    token_usage = None
    if terminal.get("usage_status") in {"reported", "partial"}:
        token_usage = {
            field: terminal.get(field, 0)
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        }
        token_usage["usage_source"] = "model_invocation_terminal"
    invocation_dir = started_path.parent
    invocation = {
        "invocation_id": start["invocation_id"],
        "usage_event_id": terminal.get("usage_event_id"),
        "started_path": str(started_path),
        "terminal_path": str(terminal_path),
        "stdout_path": str(invocation_dir / "stdout.jsonl"),
        "stderr_path": str(invocation_dir / "stderr.log"),
        "replayed_from_terminal": True,
    }
    runtime_result = {
        "result_status": result_status,
        "changed_files": _worktree_changed_files(worktree_path),
        "output": {
            "adapter": "mailbox_replay",
            "model_invocation": invocation,
            "model_invocation_usage": terminal,
        },
    }
    if token_usage is not None:
        runtime_result["token_usage"] = token_usage
    return {
        "recovery_status": "terminal_replayed",
        "invocation_id": start["invocation_id"],
        "runtime_result": runtime_result,
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
    changed_files = _authoritative_worktree_changed_files(worktree_path)
    return changed_files if changed_files is not None else []


def _authoritative_worktree_changed_files(worktree_path):
    if not worktree_path:
        return None
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
        return None

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


def _reconcile_runtime_changed_files(
    result,
    worktree_path,
    *,
    expected_output_artifacts=(),
):
    actual_changed_files = _authoritative_worktree_changed_files(
        worktree_path
    )
    if actual_changed_files is None:
        return result
    reported_changed_files = result.get("changed_files")
    reported_paths = (
        [
            path
            for path in reported_changed_files
            if isinstance(path, str)
        ]
        if isinstance(reported_changed_files, list)
        else []
    )
    expected_artifacts = {
        path
        for path in expected_output_artifacts
        if isinstance(path, str)
    }
    preserved_runtime_artifacts = sorted(
        set(reported_paths).intersection(expected_artifacts)
    )
    authoritative_changed_files = sorted(
        set(actual_changed_files).union(preserved_runtime_artifacts)
    )
    if reported_changed_files == authoritative_changed_files:
        return result
    output = (
        dict(result.get("output"))
        if isinstance(result.get("output"), dict)
        else {}
    )
    output["changed_files_reconciliation"] = {
        "status": "reconciled_to_worktree",
        "reported_changed_files": (
            list(reported_changed_files)
            if isinstance(reported_changed_files, list)
            else []
        ),
        "actual_changed_files": actual_changed_files,
        "preserved_runtime_artifacts": preserved_runtime_artifacts,
    }
    return {
        **result,
        "changed_files": authoritative_changed_files,
        "output": output,
    }


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


def _validate_dispatch_authority(output_dir, message):
    message_id = message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise MailboxDispatchIntegrityError(
            "mailbox dispatch message_id is invalid"
        )
    file_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    authority_path = (
        Path(output_dir)
        / "state"
        / "mailbox_dispatch_authority"
        / f"{file_id}.json"
    )
    try:
        authority = json.loads(authority_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailboxDispatchIntegrityError(
            "mailbox dispatch authority is unavailable"
        ) from exc
    expected = {
        "schema_version": "mailbox_dispatch_authority.v1",
        "message_id": message_id,
        "message_sha256": hashlib.sha256(
            json.dumps(
                message,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    if authority != expected:
        raise MailboxDispatchIntegrityError(
            "mailbox dispatch differs from scheduler authority"
        )


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
        "--codex-reasoning-profile",
        help="Optional reasoning effort passed to CodexRuntimeAdapter.",
    )
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
    parser.add_argument(
        "--codex-resume-session-id",
        help="Explicit Codex session id to resume for this worker. Experimental.",
    )
    parser.add_argument(
        "--codex-resume-last",
        action="store_true",
        help="Resume the most recent Codex session for this worker process. Experimental.",
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
            or args.codex_reasoning_profile
            or args.codex_sandbox != "workspace-write"
            or args.codex_timeout_seconds != 300
            or args.codex_resume_session_id
            or args.codex_resume_last
        ):
            parser.error("Codex runtime options require --runtime codex")
        return FakeRuntimeAdapter()
    if args.runtime == "codex":
        if args.codex_timeout_seconds < 1:
            parser.error("--codex-timeout-seconds must be at least 1")
        if args.codex_resume_session_id and args.codex_resume_last:
            parser.error("--codex-resume-session-id and --codex-resume-last are mutually exclusive")
        return CodexRuntimeAdapter(
            command=_parse_command_json(parser, args.codex_command_json),
            model=args.codex_model,
            reasoning_profile=args.codex_reasoning_profile,
            sandbox=args.codex_sandbox,
            timeout_seconds=args.codex_timeout_seconds,
            fallback_worktree_path=args.codex_fallback_worktree_path,
            output_dir=args.output_dir,
            resume_session_id=args.codex_resume_session_id,
            resume_last=args.codex_resume_last,
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

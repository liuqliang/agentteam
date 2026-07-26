import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import m0_runtime as _runtime
from .m0_runtime import CodexRuntimeAdapter as _BaseCodexRuntimeAdapter
from .m0_runtime import run_simulation
from .model_invocation import (
    ModelInvocationCall,
    ModelInvocationError,
    ModelInvocationUnavailable,
    invocation_context_from_message,
    is_supported_codex_command,
)


ENV_GATE = "AGENTTEAM_RUN_LIVE_CODEX"
EXPECTED_FILE = "generated/live_codex_smoke.json"
SUPPORTED_NON_WORKER_INVOCATION_INVENTORY = {
    "taskpack_author": "taskpack_author.py:_run_codex_author_command",
    "follow_up_author": (
        "agentteam.py:_run_pursue_loop/_handle_next"
    ),
    "runtime_diagnostic": "diagnostic_chat.py:run_runtime_diagnostic_chat",
    "development_smoke_basic": "live_codex_smoke.py:run_live_smoke",
    "development_smoke_scheduler": (
        "live_codex_scheduler_smoke.py:run_live_scheduler_smoke"
    ),
    "development_smoke_repo_context": (
        "live_codex_repo_context_smoke.py:run_live_repo_context_smoke"
    ),
    "development_smoke_pipeline": (
        "live_codex_pipeline_smoke.py:run_live_pipeline_smoke"
    ),
    "development_smoke_multifile_pipeline": (
        "live_codex_multifile_pipeline_smoke.py:"
        "run_live_multifile_pipeline_smoke"
    ),
    "development_smoke_cli": "live_codex_cli_smoke.py:run_live_cli_smoke",
}


class CodexRuntimeAdapter(_BaseCodexRuntimeAdapter):
    """Codex adapter whose one provider call is owned by the smoke controller."""

    def __init__(
        self,
        *args,
        controller_session_id=None,
        lifecycle_output_dir=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.controller_session_id = controller_session_id
        self.lifecycle_output_dir = (
            Path(lifecycle_output_dir) if lifecycle_output_dir else None
        )

    def bind_output_dir(self, output_dir):
        session_id = f"DEVELOPMENT-SMOKE-SESSION-{uuid.uuid4().hex}"
        lifecycle_output_dir = (
            Path(output_dir)
            / "state"
            / "controller_invocations"
            / "development_smoke"
            / session_id
        )
        lifecycle_output_dir.mkdir(parents=True, exist_ok=False)
        return DevelopmentSmokeCodexRuntimeAdapter(
            command=self.command,
            model=self.model,
            sandbox=self.sandbox,
            timeout_seconds=self.timeout_seconds,
            extra_args=self.extra_args,
            fallback_worktree_path=self.fallback_worktree_path,
            output_dir=lifecycle_output_dir,
            progress_interval_seconds=self.progress_interval_seconds,
            resume_session_id=self.resume_session_id,
            resume_last=self.resume_last,
            systemd_runner_factory=self.systemd_runner_factory,
            controller_session_id=session_id,
            lifecycle_output_dir=lifecycle_output_dir,
        )

    def run(self, message, worktree_path=None, progress_callback=None):
        message = copy.deepcopy(message)
        payload = message["payload"]
        session_id = (
            self.controller_session_id
            or f"DEVELOPMENT-SMOKE-SESSION-{uuid.uuid4().hex}"
        )
        owner_token = f"DEVELOPMENT-SMOKE-OWNER-{uuid.uuid4().hex}"
        payload.update(
            {
                "runtime_execution_session_id": session_id,
                "lifecycle_owner_token": owner_token,
                "agent_id": "development-smoke-controller",
                "agent_role": "development_smoke",
                "required_role": "development_smoke",
                "usage_stage": "development_smoke",
            }
        )
        authority_root = self.lifecycle_output_dir or self.output_dir
        if authority_root is not None:
            _write_controller_claim(
                Path(authority_root),
                {
                    "claim_schema_version": (
                        "model_invocation_controller_claim.v1"
                    ),
                    "project": payload.get("project"),
                    "run_id": payload.get("run_id"),
                    "taskpack_id": payload.get("taskpack_id"),
                    "usage_stage": "development_smoke",
                    "runtime_execution_session_id": session_id,
                    "lifecycle_owner_token": owner_token,
                    "authority_root": str(Path(authority_root)),
                },
            )
        return self._run_controller_invocation(
            message,
            worktree_path=worktree_path,
            progress_callback=progress_callback,
        )

    def _run_controller_invocation(
        self,
        message,
        *,
        worktree_path=None,
        progress_callback=None,
    ):
        runtime_worktree_path = worktree_path or self.fallback_worktree_path
        using_fallback = (
            worktree_path is None and self.fallback_worktree_path is not None
        )
        if not runtime_worktree_path:
            return _development_smoke_failure("missing_worktree_path")
        fallback_status_before = (
            _runtime._git_status_signature(runtime_worktree_path)
            if using_fallback
            else None
        )
        temporary_result_dir = None
        result_path = self._result_path(
            runtime_worktree_path,
            message["payload"]["attempt_id"],
            using_fallback=using_fallback,
        )
        if result_path is None:
            temporary_result_dir = tempfile.TemporaryDirectory()
            result_path = (
                Path(temporary_result_dir.name)
                / f"codex_result_{message['payload']['attempt_id']}.json"
            )
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            command = self._build_command(runtime_worktree_path, result_path)
            prompt = self._build_prompt(message)
            supported = is_supported_codex_command(command)
            if supported and self.output_dir is None:
                return _development_smoke_failure(
                    "missing_model_invocation_output_root"
                )
            context = invocation_context_from_message(
                message,
                model=self.model,
                backend="codex",
            )
            context.update(
                {
                    "provider_resume_mode": "new",
                    "requested_provider_session_id": None,
                    "provider_predecessor_invocation_id": None,
                    "provider_predecessor_turn_id": None,
                    "provider_predecessor_usage_snapshot": None,
                    "coverage_class": (
                        "supported_model_invocation"
                        if supported
                        else "not_applicable_adapter"
                    ),
                }
            )
            invocation = ModelInvocationCall(
                self.output_dir or result_path.parent,
                context,
                supported=supported,
                systemd_runner_factory=self.systemd_runner_factory,
            )

            def finish(runtime_result, terminal_status, execution):
                terminal = invocation.finalize(
                    terminal_status,
                    execution,
                    terminal_writer="development_smoke_controller",
                )
                output = runtime_result.get("output")
                if not isinstance(output, dict):
                    output = {
                        "adapter": "codex",
                        "error": "invalid_runtime_output",
                    }
                    runtime_result["output"] = output
                output["model_invocation"] = {
                    **invocation.lifecycle.summary(),
                    "usage_event_id": terminal["usage_event_id"],
                    "terminal_status": terminal["terminal_status"],
                    "usage_status": terminal["usage_status"],
                }
                return _runtime._attach_codex_token_usage(
                    runtime_result,
                    execution.stdout,
                )

            try:
                execution = invocation.execute(
                    command,
                    cwd=runtime_worktree_path,
                    input_text=prompt,
                    timeout_seconds=self.timeout_seconds,
                    progress_callback=progress_callback,
                    progress_interval_seconds=self.progress_interval_seconds,
                )
            except ModelInvocationUnavailable as exc:
                return _development_smoke_failure(
                    "model_invocation_execution_group_unavailable",
                    reason=str(exc)[:500],
                )
            except ModelInvocationError as exc:
                return _development_smoke_failure(
                    "model_invocation_lifecycle_error",
                    reason=str(exc)[:500],
                )
            if execution.launch_failed:
                return finish(
                    _development_smoke_failure(
                        "provider_launch_failed",
                        reason=execution.launch_error,
                    ),
                    "launch_failed",
                    execution,
                )
            if execution.timed_out:
                return finish(
                    _development_smoke_failure(
                        "timeout",
                        result_status="timed_out",
                        timeout_seconds=self.timeout_seconds,
                    ),
                    "timed_out",
                    execution,
                )
            completed = execution.completed_process()
            fallback_modification = self._fallback_modification_result(
                runtime_worktree_path,
                fallback_status_before,
            )
            if fallback_modification:
                return finish(fallback_modification, "failed", execution)
            if completed.returncode != 0:
                permission_request = (
                    _runtime._codex_permission_request_from_failure(
                        command,
                        completed.returncode,
                        completed.stdout,
                        completed.stderr,
                        self.sandbox,
                    )
                )
                if permission_request:
                    return finish(
                        {
                            "result_status": "blocked",
                            "changed_files": [],
                            "output": {
                                "adapter": "codex",
                                "error": "permission_required",
                                "permission_request": permission_request,
                                "exit_code": completed.returncode,
                            },
                        },
                        "blocked",
                        execution,
                    )
                return finish(
                    _development_smoke_failure(
                        "provider_failed",
                        exit_code=completed.returncode,
                    ),
                    "failed",
                    execution,
                )
            if not result_path.exists():
                return finish(
                    _development_smoke_failure(
                        "missing_output_last_message"
                    ),
                    "missing_result",
                    execution,
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return finish(
                    _development_smoke_failure(
                        "invalid_output_last_message_json"
                    ),
                    "invalid_result",
                    execution,
                )
            if not isinstance(result, dict):
                return finish(
                    _development_smoke_failure(
                        "invalid_output_last_message_shape"
                    ),
                    "invalid_result",
                    execution,
                )
            normalized = _runtime._normalize_runtime_result(
                result,
                adapter="codex",
                stderr=completed.stderr,
                preserve_token_usage=False,
            )
            terminal_status = normalized.get("result_status")
            normalized_output = normalized.get("output")
            invalid = (
                not isinstance(result.get("output", {}), dict)
                or (
                    isinstance(normalized_output, dict)
                    and normalized_output.get("error")
                    == "invalid_changed_files"
                )
            )
            if invalid:
                normalized["result_status"] = "failed"
                terminal_status = "invalid_result"
            if terminal_status not in {
                "completed",
                "failed",
                "blocked",
                "cancelled",
            }:
                normalized["result_status"] = "failed"
                terminal_status = "invalid_result"
            return finish(normalized, terminal_status, execution)
        finally:
            if temporary_result_dir:
                temporary_result_dir.cleanup()


DevelopmentSmokeCodexRuntimeAdapter = CodexRuntimeAdapter


def _development_smoke_failure(error, *, result_status="failed", **details):
    return {
        "result_status": result_status,
        "changed_files": [],
        "output": {"adapter": "codex", "error": error, **details},
    }


def _write_controller_claim(authority_root, claim):
    authority_root.mkdir(parents=True, exist_ok=True)
    path = authority_root / "controller_claim.json"
    payload = json.dumps(claim, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError:
        if path.read_text(encoding="utf-8") != payload:
            raise RuntimeError(f"conflicting development-smoke claim: {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a gated live Codex runtime smoke test.")
    parser.add_argument(
        "--output-dir",
        help="Directory for the temporary repo, generated fixtures, and runtime output.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional command prefix for CodexRuntimeAdapter. Must appear last.",
    )
    args = parser.parse_args(argv)

    if os.environ.get(ENV_GATE) != "1":
        _print_json({"status": "skipped", "reason": f"set {ENV_GATE}=1"})
        return 0

    output_dir = Path(args.output_dir or tempfile.mkdtemp(prefix="agentteam-live-codex-"))
    output_dir = output_dir.resolve()
    try:
        summary = run_live_smoke(output_dir, args.codex_command, args.timeout_seconds)
    except Exception as exc:  # pragma: no cover - exercised by CLI failure behavior.
        _print_json({"status": "failed", "error": str(exc), "output_dir": str(output_dir)})
        return 1

    _print_json(summary)
    return 0 if summary["status"] == "completed" else 1


def run_live_smoke(output_dir, codex_command=None, timeout_seconds=300):
    repo_path = output_dir / "repo"
    fixture_dir = output_dir / "fixtures"
    run_dir = output_dir / "run"
    if repo_path.exists():
        raise RuntimeError(f"refusing to reuse existing smoke repo: {repo_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _init_git_repo(repo_path)
    agent_pool_path, backlog_path = _write_smoke_fixtures(fixture_dir)
    adapter = DevelopmentSmokeCodexRuntimeAdapter(
        command=codex_command or None,
        timeout_seconds=timeout_seconds,
    )

    result = run_simulation(
        agent_pool_path,
        backlog_path,
        run_dir,
        project_root=repo_path,
        runtime_adapter=adapter,
    )
    runtime_event = _find_runtime_event(Path(result["events_path"]))
    changed_files = runtime_event["payload"]["changed_files"]
    expected_path = Path(result["worktree_path"]) / EXPECTED_FILE
    status = (
        "completed"
        if result["validation_status"] == "accepted"
        and EXPECTED_FILE in changed_files
        and expected_path.exists()
        else "failed"
    )

    return {
        "status": status,
        "validation_status": result["validation_status"],
        "expected_file": EXPECTED_FILE,
        "expected_file_exists": expected_path.exists(),
        "changed_files": changed_files,
        "output_dir": str(output_dir),
        "repo_path": str(repo_path),
        "worktree_path": result["worktree_path"],
        "events_path": result["events_path"],
    }


def _init_git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "agentteam@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "AgentTeam Smoke"], cwd=path, check=True)
    (path / "README.md").write_text("# live codex smoke fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial smoke fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _write_smoke_fixtures(fixture_dir):
    fixture_dir.mkdir(parents=True, exist_ok=True)
    agent_pool = {
        "scheduler_agent_id": "agent-scheduler",
        "agents": [
            {
                "agent_id": "agent-live-codex",
                "role": "repo_map_agent",
                "status": "idle",
                "inbox_path": "mailboxes/agent-live-codex/inbox.jsonl",
            }
        ],
    }
    backlog = {
        "backlog_id": "BL-LIVE-CODEX-SMOKE",
        "items": [
            {
                "task_id": "TASK-LIVE-CODEX-SMOKE",
                "milestone_id": "M1c",
                "objective": (
                    f"Create {EXPECTED_FILE} containing a JSON object with "
                    '`"live_codex_smoke": true`. Report exactly '
                    f"{EXPECTED_FILE} in changed_files."
                ),
                "backlog_status": "ready",
                "risk_target": "L0",
                "depends_on": [],
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "required_role": "repo_map_agent",
                "blockers": [],
            }
        ],
    }
    agent_pool_path = fixture_dir / "agent_pool.json"
    backlog_path = fixture_dir / "backlog.json"
    agent_pool_path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")
    backlog_path.write_text(json.dumps(backlog, sort_keys=True), encoding="utf-8")
    return agent_pool_path, backlog_path


def _find_runtime_event(events_path):
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["event_type"] == "runtime_output_received":
            return event
    raise RuntimeError(f"missing runtime_output_received event: {events_path}")


def _print_json(payload):
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())

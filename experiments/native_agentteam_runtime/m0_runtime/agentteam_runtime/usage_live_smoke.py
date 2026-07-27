"""Bounded, non-authoritative provider smoke used by the Phase 1 controller.

This helper owns one acceptance-live-smoke invocation lifecycle.  It can only
write a provisional result below the controller-supplied output directory; it
cannot publish the fixed acceptance artifact.  The controller deliberately
does not trust the normalized provider fields emitted here.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .model_invocation import (
    ModelInvocationCall,
    ModelInvocationError,
    ModelInvocationUnavailable,
    is_supported_codex_command,
)


FIXED_ACCEPTANCE_ARTIFACT = "model-invocation-live-smoke.v1.json"
MINIMAL_PROMPT = (
    "Return only the word READY. Do not inspect, create, edit, or delete files."
)
DEFAULT_TIMEOUT_SECONDS = 180
MAX_ACCEPTANCE_PROVIDER_SPOOL_BYTES = 1024 * 1024
SUPPORTED_ACCEPTANCE_INVOCATION_INVENTORY = {
    "acceptance_live_smoke": "usage_live_smoke.py:run_usage_live_smoke",
}


class UsageLiveSmokeError(RuntimeError):
    """The bounded helper could not produce a provisional result."""


def run_usage_live_smoke(
    *,
    project_root,
    run_id,
    taskpack_id,
    implementation_run_id,
    gate_epoch,
    attempt_id,
    output_dir,
    output,
    project=None,
    expected_commit=None,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    codex_command=None,
    model=None,
    systemd_runner_factory=None,
):
    """Run one provider call and return its non-authoritative provisional data."""
    project_root = Path(project_root).resolve()
    output_dir = Path(output_dir).resolve()
    output = Path(output).resolve()
    _require_external_output(project_root, output_dir, output)
    if output.exists() or output.is_symlink():
        raise UsageLiveSmokeError(f"refusing to replace provisional output: {output}")
    if output.name == FIXED_ACCEPTANCE_ARTIFACT:
        raise UsageLiveSmokeError(
            "the helper cannot publish the fixed acceptance artifact"
        )
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
        or timeout_seconds > 900
    ):
        raise UsageLiveSmokeError(
            "timeout_seconds must be greater than zero and at most 900"
        )
    if not project_root.is_dir():
        raise UsageLiveSmokeError(f"candidate project root is missing: {project_root}")
    candidate_commit = _git_stdout(project_root, ["rev-parse", "HEAD"])
    if expected_commit is not None and candidate_commit != expected_commit:
        raise UsageLiveSmokeError("candidate commit differs from expected commit")

    output_dir.mkdir(parents=True, exist_ok=True)
    lifecycle_root = output_dir / "lifecycle"
    provider_result = output_dir / "provider-last-message.json"
    command = _provider_command(
        codex_command,
        project_root=project_root,
        result_path=provider_result,
        model=model,
    )
    supported = is_supported_codex_command(command)
    context = {
        "project": project or project_root.name,
        "run_id": run_id,
        "pursue_id": None,
        "round_index": None,
        "taskpack_id": taskpack_id,
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "task_id": "P1-LIVE",
        "attempt_id": attempt_id,
        "runtime_execution_session_id": (
            f"ACCEPTANCE-SESSION-{uuid.uuid4().hex}"
        ),
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": f"ACCEPTANCE-OWNER-{uuid.uuid4().hex}",
        "agent_id": "phase1-usage-acceptance-controller",
        "role": "acceptance_live_smoke",
        "usage_stage": "acceptance_live_smoke",
        "backend": "codex",
        "model": model,
        "coverage_class": (
            "supported_model_invocation"
            if supported
            else "not_applicable_adapter"
        ),
        "_explicit_context_fields": {
            field: True
            for field in (
                "project",
                "run_id",
                "taskpack_id",
                "runtime_execution_session_id",
                "lifecycle_owner_token",
                "agent_id",
                "role",
                "usage_stage",
            )
        },
    }
    invocation = ModelInvocationCall(
        lifecycle_root,
        context,
        supported=supported,
        systemd_runner_factory=systemd_runner_factory,
    )
    try:
        execution = invocation.execute(
            command,
            cwd=project_root,
            input_text=MINIMAL_PROMPT,
            timeout_seconds=timeout_seconds,
        )
    except (ModelInvocationUnavailable, ModelInvocationError) as exc:
        raise UsageLiveSmokeError(str(exc)) from exc

    if execution.launch_failed:
        terminal_status = "launch_failed"
    elif execution.timed_out:
        terminal_status = "timed_out"
    elif execution.returncode == 0:
        terminal_status = "completed"
    else:
        terminal_status = "failed"
    terminal = invocation.finalize(
        terminal_status,
        execution,
        terminal_writer="acceptance_controller",
    )
    started = _read_json(invocation.lifecycle.started_path)
    raw_spool = invocation.lifecycle.stdout_path.resolve()
    _require_path_within(raw_spool, output_dir, "bounded provider spool")
    if raw_spool.stat().st_size > MAX_ACCEPTANCE_PROVIDER_SPOOL_BYTES:
        raise UsageLiveSmokeError(
            "bounded provider spool exceeds the 1 MiB acceptance limit"
        )
    terminal_event = _last_terminal_event(raw_spool)
    provisional = {
        "schema_version": "model_invocation_live_smoke.provisional.v1",
        "project": context["project"],
        "run_id": run_id,
        "taskpack_id": taskpack_id,
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "acceptance_attempt_id": attempt_id,
        "candidate_commit_sha": candidate_commit,
        "candidate_runtime_root": str(Path(__file__).resolve().parents[1]),
        "terminal_status": terminal_status,
        "invocation_start_record": started,
        "invocation_usage_record": terminal,
        "provider_terminal_snapshot_sha256": (
            _canonical_sha256(terminal_event) if terminal_event is not None else None
        ),
        "provider_totals": {
            key: terminal.get(key)
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        },
        "bounded_raw_spool_path": raw_spool.relative_to(output_dir).as_posix(),
    }
    _exclusive_publish_json(output, provisional)
    return provisional


def _provider_command(command, *, project_root, result_path, model):
    command = list(command or ["codex", "exec"])
    executable = Path(str(command[0]))
    if executable.parent == Path(".") and executable.name.lower() in {
        "codex",
        "codex.exe",
    }:
        resolved = shutil.which(str(command[0]))
        if not resolved:
            raise UsageLiveSmokeError("Codex executable is unavailable")
        command[0] = str(Path(resolved).resolve())
    command.extend(["-C", str(project_root), "-s", "read-only"])
    if model:
        command.extend(["-m", model])
    if "--json" not in command:
        command.append("--json")
    command.extend(["--output-last-message", str(result_path), "-"])
    return command


def _require_external_output(project_root, output_dir, output):
    try:
        output_dir.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise UsageLiveSmokeError("helper output directory must be outside the candidate")
    _require_path_within(output, output_dir, "provisional output")
    if output == output_dir:
        raise UsageLiveSmokeError("provisional output must be a file below output-dir")


def _require_path_within(path, root, label):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError as exc:
        raise UsageLiveSmokeError(f"{label} escapes output-dir") from exc


def _last_terminal_event(path):
    terminal = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type")
            in {
                "response.completed",
                "response_completed",
                "turn.completed",
                "turn_completed",
            }
        ):
            terminal = event
    return terminal


def _canonical_sha256(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageLiveSmokeError(f"lifecycle record is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise UsageLiveSmokeError(f"lifecycle record is not an object: {path}")
    return value


def _exclusive_publish_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise UsageLiveSmokeError(
                f"refusing to replace provisional output: {path}"
            ) from exc
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path):
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _git_stdout(project_root, arguments):
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise UsageLiveSmokeError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _print_json(value, stream=None):
    (stream or sys.stdout).write(json.dumps(value, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one bounded non-authoritative Phase 1 usage smoke."
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--project")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--taskpack-id", required=True)
    parser.add_argument("--implementation-run-id", required=True)
    parser.add_argument("--gate-epoch", required=True, type=int)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--model")
    args = parser.parse_args(argv)
    try:
        result = run_usage_live_smoke(
            project_root=args.project_root,
            project=args.project,
            run_id=args.run_id,
            taskpack_id=args.taskpack_id,
            implementation_run_id=args.implementation_run_id,
            gate_epoch=args.gate_epoch,
            attempt_id=args.attempt_id,
            output_dir=args.output_dir,
            output=args.output,
            expected_commit=args.expected_commit,
            timeout_seconds=args.timeout_seconds,
            model=args.model,
        )
    except Exception as exc:
        _print_json(
            {"status": "failed", "error": str(exc)[:1000]},
            stream=sys.stderr,
        )
        return 1
    _print_json(
        {
            "status": "completed",
            "run_id": result["run_id"],
            "output": str(Path(args.output).resolve()),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

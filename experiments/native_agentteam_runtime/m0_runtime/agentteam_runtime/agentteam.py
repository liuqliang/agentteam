import argparse
import json
import subprocess
import sys
from pathlib import Path

from .taskpack import build_taskpack_runtime_args, freeze_taskpack, validate_taskpack
from .taskpack_author import draft_taskpack_from_goal


class AgentTeamCliError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentTeamCliError(message)


def main(argv=None):
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        result = args.handler(args)
        if isinstance(result, int):
            return result
        if result is not None:
            _print_json(result, stream=sys.stdout)
        return 0
    except AgentTeamCliError as exc:
        _print_json(_error_payload(exc), stream=sys.stderr)
        return 1
    except Exception as exc:
        _print_json(_error_payload(exc), stream=sys.stderr)
        return 1


def _build_parser():
    parser = JsonArgumentParser(description="AgentTeam operator CLI.")
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=JsonArgumentParser,
    )

    taskpack = subcommands.add_parser("taskpack", help="Draft, validate, and freeze taskpacks.")
    taskpack_subcommands = taskpack.add_subparsers(
        dest="taskpack_command",
        required=True,
        parser_class=JsonArgumentParser,
    )
    _add_submit_parser(subcommands)
    _add_taskpack_draft_parser(taskpack_subcommands)
    _add_taskpack_validate_parser(taskpack_subcommands)
    _add_taskpack_freeze_parser(taskpack_subcommands)
    _add_run_parser(subcommands)
    return parser


def _add_submit_parser(subcommands):
    parser = subcommands.add_parser("submit", help="Draft, validate, freeze, and run a taskpack.")
    parser.add_argument("--project-root", required=True, help="Git repository root for the target project.")
    parser.add_argument("--goal", required=True, help="Human-readable taskpack goal.")
    parser.add_argument("--work-root", required=True, help="Directory for drafts, frozen taskpacks, and runs.")
    parser.add_argument("--taskpack-id", help="Optional safe taskpack id slug.")
    parser.add_argument(
        "--author-runtime",
        choices=["fake", "codex"],
        default="fake",
        help="Runtime used to author the taskpack.",
    )
    parser.add_argument(
        "--runtime",
        choices=["auto", "fake", "codex"],
        default="auto",
        help="Runtime backend used to execute the frozen taskpack.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Use the one-shot scheduler path instead of the daemon worker-pool path.",
    )
    parser.add_argument("--max-inflight", type=int, default=2, help="Maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, default=1, help="Maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Commit integration worktree changes after verification passes.",
    )
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.set_defaults(handler=_handle_submit)


def _add_taskpack_draft_parser(subcommands):
    parser = subcommands.add_parser("draft", help="Draft a taskpack from a human goal.")
    parser.add_argument("--project-root", required=True, help="Git repository root for the target project.")
    parser.add_argument("--goal", required=True, help="Human-readable taskpack goal.")
    parser.add_argument("--draft-root", required=True, help="Directory where the draft taskpack will be written.")
    parser.add_argument("--taskpack-id", help="Optional safe taskpack id slug.")
    parser.add_argument(
        "--author-runtime",
        choices=["fake", "codex"],
        default="fake",
        help="Runtime used to author the taskpack.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for Codex taskpack authoring.",
    )
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional Codex command prefix. Must appear last.",
    )
    parser.set_defaults(handler=_handle_taskpack_draft)


def _add_taskpack_validate_parser(subcommands):
    parser = subcommands.add_parser("validate", help="Validate a draft or frozen taskpack.")
    parser.add_argument("taskpack_dir", help="Taskpack directory to validate.")
    parser.set_defaults(handler=_handle_taskpack_validate)


def _add_taskpack_freeze_parser(subcommands):
    parser = subcommands.add_parser("freeze", help="Freeze an accepted taskpack for runtime launch.")
    parser.add_argument("taskpack_dir", help="Draft taskpack directory to freeze.")
    parser.add_argument("--frozen-root", required=True, help="Directory where frozen taskpacks are written.")
    parser.set_defaults(handler=_handle_taskpack_freeze)


def _add_run_parser(subcommands):
    parser = subcommands.add_parser("run", help="Run a frozen taskpack through agentteam_runtime.cli.")
    parser.add_argument("frozen_taskpack_dir", help="Frozen taskpack directory to run.")
    parser.add_argument("--run-root", required=True, help="Directory where run output will be written.")
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Use the one-shot scheduler path instead of the daemon worker-pool path.",
    )
    parser.add_argument("--max-inflight", type=int, default=2, help="Maximum daemon inflight attempts.")
    parser.add_argument("--max-attempts", type=int, default=1, help="Maximum attempts per task.")
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Commit integration worktree changes after verification passes.",
    )
    parser.set_defaults(handler=_handle_run)


def _handle_taskpack_draft(args):
    return draft_taskpack_from_goal(
        project_root=args.project_root,
        goal=args.goal,
        draft_root=args.draft_root,
        author_runtime=args.author_runtime,
        taskpack_id=args.taskpack_id,
        codex_command=args.codex_command,
        codex_timeout_seconds=args.codex_timeout_seconds,
    )


def _handle_taskpack_validate(args):
    return validate_taskpack(args.taskpack_dir)


def _handle_taskpack_freeze(args):
    return freeze_taskpack(args.taskpack_dir, args.frozen_root)


def _handle_submit(args):
    work_root = Path(args.work_root).resolve()
    draft_root = work_root / "drafts"
    frozen_root = work_root / "frozen"
    run_root = work_root / "runs"
    runtime_backend = _submit_runtime_backend(args.runtime, args.author_runtime)

    draft = draft_taskpack_from_goal(
        project_root=args.project_root,
        goal=args.goal,
        draft_root=draft_root,
        author_runtime=args.author_runtime,
        taskpack_id=args.taskpack_id,
        codex_command=args.codex_command,
        codex_timeout_seconds=args.codex_timeout_seconds,
    )
    taskpack_dir = Path(draft["taskpack_dir"])
    _set_taskpack_runtime_backend(taskpack_dir, runtime_backend)
    validation = validate_taskpack(taskpack_dir)
    frozen = freeze_taskpack(taskpack_dir, frozen_root)
    completed = _run_frozen_taskpack(
        frozen["frozen_taskpack_dir"],
        run_root=run_root,
        one_shot=args.one_shot,
        max_inflight=args.max_inflight,
        max_attempts=args.max_attempts,
        commit_verified_integration=args.commit_verified_integration,
    )
    if completed.returncode != 0:
        raise AgentTeamCliError(
            "agentteam submit run step failed",
            step="run",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return {
        "status": "completed",
        "taskpack_id": draft["taskpack_id"],
        "runtime": runtime_backend,
        "draft": draft,
        "validation": validation,
        "freeze": frozen,
        "run": _json_or_output(completed.stdout),
        "paths": {
            "work_root": str(work_root),
            "draft_root": str(draft_root),
            "frozen_root": str(frozen_root),
            "run_root": str(run_root),
        },
    }


def _handle_run(args):
    completed = _run_frozen_taskpack(
        args.frozen_taskpack_dir,
        run_root=args.run_root,
        one_shot=args.one_shot,
        max_inflight=args.max_inflight,
        max_attempts=args.max_attempts,
        commit_verified_integration=args.commit_verified_integration,
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
        sys.stdout.flush()
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()
    return completed.returncode


def _run_frozen_taskpack(
    frozen_taskpack_dir,
    run_root,
    one_shot=False,
    max_inflight=2,
    max_attempts=1,
    commit_verified_integration=False,
):
    runtime_args = build_taskpack_runtime_args(
        frozen_taskpack_dir,
        run_root=run_root,
        daemon=not one_shot,
        max_inflight=max_inflight,
        max_attempts=max_attempts,
        commit_verified_integration=commit_verified_integration,
    )
    command = [sys.executable, "-m", "agentteam_runtime.cli", *runtime_args]
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _submit_runtime_backend(runtime, author_runtime):
    if runtime != "auto":
        return runtime
    return "fake" if author_runtime == "fake" else "codex"


def _set_taskpack_runtime_backend(taskpack_dir, runtime_backend):
    taskpack_dir = Path(taskpack_dir)
    taskpack_path = taskpack_dir / "taskpack.yaml"
    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
    runtime = taskpack.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    runtime["default_backend"] = runtime_backend
    taskpack["runtime"] = runtime
    _write_json(taskpack_path, taskpack)

    files = taskpack.get("files", {})
    if not isinstance(files, dict):
        files = {}

    agent_pool_path = taskpack_dir / files.get("agent_pool", "agent_pool.json")
    agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
    for profile in _runtime_profiles(agent_pool):
        profile["adapter"] = runtime_backend
    _write_json(agent_pool_path, agent_pool)

    if runtime_backend == "fake":
        backlog_path = taskpack_dir / files.get("backlog", "backlog.json")
        backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
        if isinstance(backlog, dict):
            for item in backlog.get("items", []):
                if isinstance(item, dict):
                    item["write_scope"] = ["generated/"]
        _write_json(backlog_path, backlog)


def _runtime_profiles(agent_pool):
    if not isinstance(agent_pool, dict):
        return
    role_profiles = agent_pool.get("role_runtime_profiles")
    if isinstance(role_profiles, dict):
        for profile in role_profiles.values():
            if isinstance(profile, dict):
                yield profile
    agents = agent_pool.get("agents")
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict) and isinstance(agent.get("runtime_profile"), dict):
                yield agent["runtime_profile"]


def _json_or_output(stdout):
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"stdout": stdout}


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_json(payload, stream):
    print(json.dumps(payload, sort_keys=True), file=stream)


def _error_payload(exc):
    payload = {
        "status": "error",
        "error": str(exc),
        "error_type": exc.__class__.__name__,
    }
    if isinstance(exc, AgentTeamCliError):
        payload.update(exc.details)
    return payload


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import json

from .daemon import run_file_daemon
from .mailbox_worker import FileMailboxRuntimeAdapter
from .m0_runtime import (
    FakeRuntimeAdapter,
    read_scheduler_state_index,
    replay_events,
    run_scheduler_loop,
    run_simulation,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AgentTeam native runtime M0 simulation.")
    parser.add_argument("--agent-pool", help="Path to agent pool JSON.")
    parser.add_argument("--backlog", help="Path to backlog JSON.")
    parser.add_argument("--output-dir", required=True, help="Directory for mailbox and event output.")
    parser.add_argument("--project-root", help="Optional git repository root for real worktree creation.")
    parser.add_argument(
        "--show-state-index",
        action="store_true",
        help="Print a read-only summary from the scheduler SQLite state index.",
    )
    parser.add_argument(
        "--run-until-idle",
        action="store_true",
        help="Run the file scheduler loop until no ready tasks remain.",
    )
    parser.add_argument(
        "--daemon-run-until-idle",
        action="store_true",
        help="Run the file daemon facade until no ready tasks remain.",
    )
    parser.add_argument(
        "--daemon-mailbox-worker",
        action="store_true",
        help="Run daemon tasks through the file mailbox worker bridge with a fake delegate.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Maximum scheduler loop steps when --run-until-idle is set.",
    )
    parser.add_argument(
        "--integrate-accepted-patch",
        action="store_true",
        help="Apply accepted patch artifacts to an integration worktree without committing.",
    )
    parser.add_argument(
        "--integration-verification-command-json",
        help="JSON array command to run in the integration worktree after patch application.",
    )
    parser.add_argument(
        "--commit-verified-integration",
        action="store_true",
        help="Commit the integration worktree only after the verification command passes.",
    )
    parser.add_argument(
        "--runtime",
        choices=["fake", "shell", "codex"],
        help=(
            "Runtime adapter to use. Defaults to fake unless --shell-command "
            "or a Codex-specific runtime option is supplied."
        ),
    )
    parser.add_argument("--codex-model", help="Optional model passed to CodexRuntimeAdapter.")
    parser.add_argument(
        "--codex-sandbox",
        help="Optional sandbox mode passed to CodexRuntimeAdapter. Defaults to workspace-write.",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        help="Optional CodexRuntimeAdapter timeout in seconds. Defaults to 300.",
    )
    parser.add_argument(
        "--shell-command",
        nargs=argparse.REMAINDER,
        help="Optional command to execute through ShellRuntimeAdapter. Must appear last.",
    )
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional command prefix to execute through CodexRuntimeAdapter. Must appear last.",
    )
    args = parser.parse_args(argv)
    if args.shell_command and args.codex_command:
        parser.error("--shell-command and --codex-command are mutually exclusive")
    if args.run_until_idle and args.daemon_run_until_idle:
        parser.error("--run-until-idle and --daemon-run-until-idle are mutually exclusive")
    if args.daemon_mailbox_worker and not args.daemon_run_until_idle:
        parser.error("--daemon-mailbox-worker requires --daemon-run-until-idle")
    if args.show_state_index:
        result = read_scheduler_state_index(args.output_dir)
        print(json.dumps(result, sort_keys=True))
        return
    _require_execution_arg(parser, args.agent_pool, "--agent-pool")
    _require_execution_arg(parser, args.backlog, "--backlog")
    runtime_profile_defaults = _build_runtime_profile_defaults(parser, args)
    integration_verification_command = _parse_command_json(
        parser,
        args.integration_verification_command_json,
    )

    if args.run_until_idle:
        result = run_scheduler_loop(
            args.agent_pool,
            args.backlog,
            args.output_dir,
            project_root=args.project_root,
            runtime_profile_defaults=runtime_profile_defaults,
            integrate_accepted_patch=args.integrate_accepted_patch,
            integration_verification_command=integration_verification_command,
            commit_verified_integration=args.commit_verified_integration,
            max_steps=args.max_steps,
        )
        snapshot = replay_events(result["events_path"])
        print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))
        return

    if args.daemon_run_until_idle:
        runtime_adapter = None
        if args.daemon_mailbox_worker:
            if runtime_profile_defaults:
                parser.error("--daemon-mailbox-worker currently supports only the fake delegate runtime")
            runtime_adapter = FileMailboxRuntimeAdapter(
                args.agent_pool,
                runtime_adapter=FakeRuntimeAdapter(),
            )
        result = run_file_daemon(
            args.agent_pool,
            args.backlog,
            args.output_dir,
            project_root=args.project_root,
            runtime_adapter=runtime_adapter,
            runtime_profile_defaults=None if runtime_adapter else runtime_profile_defaults,
            integrate_accepted_patch=args.integrate_accepted_patch,
            integration_verification_command=integration_verification_command,
            commit_verified_integration=args.commit_verified_integration,
            max_ticks=args.max_steps,
        )
        snapshot = replay_events(result["events_path"])
        print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))
        return

    result = run_simulation(
        args.agent_pool,
        args.backlog,
        args.output_dir,
        project_root=args.project_root,
        runtime_profile_defaults=runtime_profile_defaults,
        integrate_accepted_patch=args.integrate_accepted_patch,
        integration_verification_command=integration_verification_command,
        commit_verified_integration=args.commit_verified_integration,
    )
    snapshot = replay_events(result["events_path"])
    print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))


def _require_execution_arg(parser, value, flag):
    if not value:
        parser.error(f"{flag} is required unless --show-state-index is set")


def _build_runtime_profile_defaults(parser, args):
    runtime = args.runtime
    has_codex_options = _has_codex_runtime_options(args)
    if runtime is None:
        if args.shell_command:
            runtime = "shell"
        elif args.codex_command or has_codex_options:
            runtime = "codex"
        else:
            runtime = "fake"

    if runtime == "fake":
        if args.shell_command or args.codex_command or has_codex_options:
            parser.error("--runtime fake cannot be combined with runtime command overrides or Codex options")
        return None
    if runtime == "shell":
        if args.codex_command:
            parser.error("--codex-command cannot be combined with --runtime shell")
        if has_codex_options:
            parser.error("Codex runtime options require --runtime codex")
        if not args.shell_command:
            parser.error("--shell-command is required when --runtime shell is set")
        return {
            "adapter": "shell",
            "command": args.shell_command,
        }
    if runtime == "codex":
        if args.shell_command:
            parser.error("--shell-command cannot be combined with --runtime codex")
        if not args.project_root:
            parser.error("--project-root is required when --runtime codex is set")
        if args.codex_timeout_seconds is not None and args.codex_timeout_seconds < 1:
            parser.error("--codex-timeout-seconds must be at least 1")
        profile = {
            "adapter": "codex",
            "sandbox": args.codex_sandbox or "workspace-write",
            "timeout_seconds": args.codex_timeout_seconds or 300,
        }
        if args.codex_command:
            profile["command"] = args.codex_command
        if args.codex_model:
            profile["model"] = args.codex_model
        return profile
    raise AssertionError(f"unhandled runtime: {runtime}")


def _has_codex_runtime_options(args):
    return bool(args.codex_model or args.codex_sandbox or args.codex_timeout_seconds is not None)


def _parse_command_json(parser, raw_command):
    if not raw_command:
        return None
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError as exc:
        parser.error(f"--integration-verification-command-json must be valid JSON: {exc}")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        parser.error("--integration-verification-command-json must be a non-empty JSON string array")
    return command


if __name__ == "__main__":
    main()

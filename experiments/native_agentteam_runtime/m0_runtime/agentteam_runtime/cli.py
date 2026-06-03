import argparse
import json

from .daemon import run_file_daemon
from .mailbox_worker import (
    FileMailboxExternalRuntimeAdapter,
    FileMailboxRuntimeAdapter,
    FileMailboxSubprocessRuntimeAdapter,
    FileMailboxWorkerProcessSupervisor,
)
from .worker_pool import FileMailboxWorkerPoolSupervisor
from .m0_runtime import (
    FakeRuntimeAdapter,
    read_scheduler_state_index,
    replay_events,
    run_scheduler_loop,
    run_simulation,
)
from .two_phase_scheduler import run_two_phase_scheduler_loop


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
        "--daemon-mailbox-subprocess-worker",
        action="store_true",
        help="Run daemon tasks through a one-shot file mailbox worker subprocess.",
    )
    parser.add_argument(
        "--daemon-long-running-mailbox-worker",
        action="store_true",
        help="Run daemon tasks through one long-running fake mailbox worker process.",
    )
    parser.add_argument(
        "--daemon-long-running-worker-agent-id",
        default="agent-repo-map",
        help="Agent id served by --daemon-long-running-mailbox-worker.",
    )
    parser.add_argument(
        "--daemon-long-running-worker-pool",
        action="store_true",
        help="Run daemon tasks through one long-running mailbox worker process per agent.",
    )
    parser.add_argument(
        "--daemon-two-phase-worker-pool",
        action="store_true",
        help="Run daemon tasks through the two-phase scheduler and static worker pool.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Maximum scheduler loop steps when --run-until-idle is set.",
    )
    parser.add_argument(
        "--max-inflight",
        type=int,
        default=2,
        help="Maximum inflight attempts for --daemon-two-phase-worker-pool.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="Maximum attempts per task for --daemon-two-phase-worker-pool.",
    )
    parser.add_argument(
        "--lease-timeout-seconds",
        type=int,
        default=900,
        help="Lease timeout for inflight two-phase attempts.",
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
    if args.daemon_mailbox_subprocess_worker and not args.daemon_run_until_idle:
        parser.error("--daemon-mailbox-subprocess-worker requires --daemon-run-until-idle")
    if args.daemon_long_running_mailbox_worker and not args.daemon_run_until_idle:
        parser.error("--daemon-long-running-mailbox-worker requires --daemon-run-until-idle")
    if args.daemon_long_running_worker_pool and not args.daemon_run_until_idle:
        parser.error("--daemon-long-running-worker-pool requires --daemon-run-until-idle")
    if args.daemon_two_phase_worker_pool and not args.daemon_run_until_idle:
        parser.error("--daemon-two-phase-worker-pool requires --daemon-run-until-idle")
    if args.max_inflight < 1:
        parser.error("--max-inflight must be at least 1")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")
    if args.lease_timeout_seconds < 0:
        parser.error("--lease-timeout-seconds must be at least 0")
    if args.daemon_mailbox_worker and args.daemon_mailbox_subprocess_worker:
        parser.error("--daemon-mailbox-worker and --daemon-mailbox-subprocess-worker are mutually exclusive")
    if args.daemon_long_running_mailbox_worker and (
        args.daemon_mailbox_worker or args.daemon_mailbox_subprocess_worker
    ):
        parser.error(
            "--daemon-long-running-mailbox-worker cannot be combined with other daemon mailbox worker flags"
        )
    if args.daemon_long_running_worker_pool and (
        args.daemon_mailbox_worker
        or args.daemon_mailbox_subprocess_worker
        or args.daemon_long_running_mailbox_worker
    ):
        parser.error(
            "--daemon-long-running-worker-pool cannot be combined with other daemon mailbox worker flags"
        )
    if args.daemon_two_phase_worker_pool and (
        args.daemon_mailbox_worker
        or args.daemon_mailbox_subprocess_worker
        or args.daemon_long_running_mailbox_worker
        or args.daemon_long_running_worker_pool
    ):
        parser.error(
            "--daemon-two-phase-worker-pool cannot be combined with other daemon mailbox worker flags"
        )
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
        if args.daemon_two_phase_worker_pool:
            worker_pool = FileMailboxWorkerPoolSupervisor(
                args.agent_pool,
                args.output_dir,
                runtime_profile_defaults=runtime_profile_defaults,
            )
            worker_pool_start = worker_pool.start()
            try:
                result = run_two_phase_scheduler_loop(
                    args.agent_pool,
                    args.backlog,
                    args.output_dir,
                    project_root=args.project_root,
                    max_inflight=args.max_inflight,
                    max_attempts=args.max_attempts,
                    lease_timeout_seconds=args.lease_timeout_seconds,
                    max_ticks=args.max_steps,
                )
            finally:
                worker_pool_stop = worker_pool.stop()
            result = {
                **result,
                "daemon_status": result["scheduler_status"],
                "worker_pool": {
                    **worker_pool_start,
                    **worker_pool_stop,
                },
            }
            snapshot = replay_events(result["events_path"])
            print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))
            return

        runtime_adapter = None
        worker_process = None
        worker_pool = None
        worker_start = None
        worker_pool_start = None
        if args.daemon_mailbox_worker:
            if runtime_profile_defaults:
                parser.error("--daemon-mailbox-worker currently supports only the fake delegate runtime")
            runtime_adapter = FileMailboxRuntimeAdapter(
                args.agent_pool,
                runtime_adapter=FakeRuntimeAdapter(),
            )
        if args.daemon_mailbox_subprocess_worker:
            if runtime_profile_defaults:
                parser.error(
                    "--daemon-mailbox-subprocess-worker currently supports only the fake delegate runtime"
                )
            runtime_adapter = FileMailboxSubprocessRuntimeAdapter(args.agent_pool)
        if args.daemon_long_running_mailbox_worker:
            worker_runtime_profile = runtime_profile_defaults or {"adapter": "fake"}
            worker_runtime = worker_runtime_profile.get("adapter", "fake")
            if worker_runtime not in {"fake", "codex"}:
                parser.error(
                    "--daemon-long-running-mailbox-worker currently supports only fake or codex delegate runtimes"
                )
            worker_timeout_seconds = worker_runtime_profile.get("timeout_seconds", 300)
            worker_process = FileMailboxWorkerProcessSupervisor(
                args.agent_pool,
                args.output_dir,
                args.daemon_long_running_worker_agent_id,
                runtime=worker_runtime,
                codex_command=worker_runtime_profile.get("command"),
                codex_model=worker_runtime_profile.get("model"),
                codex_sandbox=worker_runtime_profile.get("sandbox", "workspace-write"),
                codex_timeout_seconds=worker_timeout_seconds,
            )
            worker_start = worker_process.start()
            external_timeout_seconds = 60
            if worker_runtime == "codex":
                external_timeout_seconds = max(60, worker_timeout_seconds + 5)
            runtime_adapter = FileMailboxExternalRuntimeAdapter(
                args.agent_pool,
                timeout_seconds=external_timeout_seconds,
            )
        if args.daemon_long_running_worker_pool:
            worker_pool = FileMailboxWorkerPoolSupervisor(
                args.agent_pool,
                args.output_dir,
                runtime_profile_defaults=runtime_profile_defaults,
            )
            worker_pool_start = worker_pool.start()
            runtime_adapter = FileMailboxExternalRuntimeAdapter(args.agent_pool)
        try:
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
        finally:
            worker_stop = worker_process.stop() if worker_process else None
            worker_pool_stop = worker_pool.stop() if worker_pool else None
        if worker_process:
            result = {
                **result,
                "worker_process": {
                    **worker_start,
                    **worker_stop,
                },
            }
        if worker_pool:
            result = {
                **result,
                "worker_pool": {
                    **worker_pool_start,
                    **worker_pool_stop,
                },
            }
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

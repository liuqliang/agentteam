import argparse
import json

from .m0_runtime import CodexRuntimeAdapter, ShellRuntimeAdapter, replay_events, run_simulation


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AgentTeam native runtime M0 simulation.")
    parser.add_argument("--agent-pool", required=True, help="Path to agent pool JSON.")
    parser.add_argument("--backlog", required=True, help="Path to backlog JSON.")
    parser.add_argument("--output-dir", required=True, help="Directory for mailbox and event output.")
    parser.add_argument("--project-root", help="Optional git repository root for real worktree creation.")
    parser.add_argument(
        "--integrate-accepted-patch",
        action="store_true",
        help="Apply accepted patch artifacts to an integration worktree without committing.",
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
    if args.shell_command:
        runtime_adapter = ShellRuntimeAdapter(args.shell_command)
    elif args.codex_command:
        runtime_adapter = CodexRuntimeAdapter(command=args.codex_command)
    else:
        runtime_adapter = None

    result = run_simulation(
        args.agent_pool,
        args.backlog,
        args.output_dir,
        project_root=args.project_root,
        runtime_adapter=runtime_adapter,
        integrate_accepted_patch=args.integrate_accepted_patch,
    )
    snapshot = replay_events(result["events_path"])
    print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))


if __name__ == "__main__":
    main()

import argparse
import json

from .m0_runtime import ShellRuntimeAdapter, replay_events, run_simulation


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AgentTeam native runtime M0 simulation.")
    parser.add_argument("--agent-pool", required=True, help="Path to agent pool JSON.")
    parser.add_argument("--backlog", required=True, help="Path to backlog JSON.")
    parser.add_argument("--output-dir", required=True, help="Directory for mailbox and event output.")
    parser.add_argument("--project-root", help="Optional git repository root for real worktree creation.")
    parser.add_argument(
        "--shell-command",
        nargs=argparse.REMAINDER,
        help="Optional command to execute through ShellRuntimeAdapter. Must appear last.",
    )
    args = parser.parse_args(argv)
    runtime_adapter = ShellRuntimeAdapter(args.shell_command) if args.shell_command else None

    result = run_simulation(
        args.agent_pool,
        args.backlog,
        args.output_dir,
        project_root=args.project_root,
        runtime_adapter=runtime_adapter,
    )
    snapshot = replay_events(result["events_path"])
    print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))


if __name__ == "__main__":
    main()

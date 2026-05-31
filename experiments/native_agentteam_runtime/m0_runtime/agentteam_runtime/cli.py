import argparse
import json

from .m0_runtime import replay_events, run_simulation


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the AgentTeam native runtime M0 simulation.")
    parser.add_argument("--agent-pool", required=True, help="Path to agent pool JSON.")
    parser.add_argument("--backlog", required=True, help="Path to backlog JSON.")
    parser.add_argument("--output-dir", required=True, help="Directory for mailbox and event output.")
    args = parser.parse_args(argv)

    result = run_simulation(args.agent_pool, args.backlog, args.output_dir)
    snapshot = replay_events(result["events_path"])
    print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))


if __name__ == "__main__":
    main()

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .m0_runtime import CodexRuntimeAdapter, run_simulation


ENV_GATE = "AGENTTEAM_RUN_LIVE_CODEX"
EXPECTED_FILE = "generated/live_codex_smoke.json"


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
    adapter = CodexRuntimeAdapter(
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

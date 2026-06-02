import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .live_codex_scheduler_smoke import ENV_GATE, _find_runtime_event, _init_git_repo, _print_json


EXPECTED_FILE = "generated/live_codex_cli_smoke.json"
TASK_ID = "TASK-LIVE-CODEX-CLI-SMOKE"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a gated live Codex CLI smoke test.")
    parser.add_argument(
        "--output-dir",
        help="Directory for the temporary repo, generated fixtures, and CLI output.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=330)
    parser.add_argument(
        "--codex-command",
        nargs=argparse.REMAINDER,
        help="Optional command prefix for CodexRuntimeAdapter. Must appear last.",
    )
    args = parser.parse_args(argv)

    if os.environ.get(ENV_GATE) != "1":
        _print_json({"status": "skipped", "reason": f"set {ENV_GATE}=1"})
        return 0

    output_dir = Path(args.output_dir or tempfile.mkdtemp(prefix="agentteam-live-codex-cli-"))
    output_dir = output_dir.resolve()
    try:
        summary = run_live_cli_smoke(
            output_dir,
            args.codex_command,
            args.timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - exercised by CLI failure behavior.
        _print_json({"status": "failed", "error": str(exc), "output_dir": str(output_dir)})
        return 1

    _print_json(summary)
    return 0 if summary["status"] == "completed" else 1


def run_live_cli_smoke(output_dir, codex_command=None, timeout_seconds=330):
    repo_path = output_dir / "repo"
    fixture_dir = output_dir / "fixtures"
    run_dir = output_dir / "run"
    if repo_path.exists():
        raise RuntimeError(f"refusing to reuse existing smoke repo: {repo_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _init_git_repo(repo_path)
    agent_pool_path, backlog_path = _write_cli_smoke_fixtures(fixture_dir)
    cli_summary = _run_scheduler_cli(
        agent_pool_path,
        backlog_path,
        run_dir,
        repo_path,
        codex_command,
        timeout_seconds,
    )
    state_index = _read_state_index_through_cli(run_dir, timeout_seconds)
    step_result = cli_summary["steps"][0]["result"] if cli_summary.get("steps") else {}
    runtime_event = (
        _find_runtime_event(Path(step_result["events_path"]))
        if step_result.get("events_path")
        else {"payload": {"changed_files": []}}
    )
    changed_files = runtime_event["payload"]["changed_files"]
    worktree_path = step_result.get("worktree_path")
    expected_path = Path(worktree_path) / EXPECTED_FILE if worktree_path else None
    runtime_sessions = state_index.get("runtime_sessions", [])
    tasks = state_index.get("tasks", [])
    status = (
        "completed"
        if cli_summary.get("scheduler_status") == "idle"
        and cli_summary.get("processed_task_ids") == [TASK_ID]
        and step_result.get("validation_status") == "accepted"
        and EXPECTED_FILE in changed_files
        and expected_path is not None
        and expected_path.exists()
        and tasks
        and tasks[0]["task_status"] == "done"
        and runtime_sessions
        and runtime_sessions[0]["runtime_adapter"] == "CodexRuntimeAdapter"
        and runtime_sessions[0]["session_status"] == "stopped"
        else "failed"
    )

    return {
        "status": status,
        "scheduler_status": cli_summary.get("scheduler_status"),
        "processed_task_ids": cli_summary.get("processed_task_ids", []),
        "expected_file": EXPECTED_FILE,
        "expected_file_exists": expected_path.exists() if expected_path else False,
        "changed_files": changed_files,
        "output_dir": str(output_dir),
        "repo_path": str(repo_path),
        "worktree_path": worktree_path,
        "events_path": cli_summary.get("events_path"),
        "state_db_path": cli_summary.get("state_db_path"),
        "state_index": state_index,
    }


def _run_scheduler_cli(
    agent_pool_path,
    backlog_path,
    run_dir,
    repo_path,
    codex_command,
    timeout_seconds,
):
    command = [
        sys.executable,
        "-m",
        "agentteam_runtime.cli",
        "--agent-pool",
        str(agent_pool_path),
        "--backlog",
        str(backlog_path),
        "--output-dir",
        str(run_dir),
        "--project-root",
        str(repo_path),
        "--run-until-idle",
        "--runtime",
        "codex",
    ]
    if codex_command:
        command.extend(["--codex-command", *codex_command])
    completed = _run_cli_subprocess(command, timeout_seconds)
    return _read_cli_json(completed, "scheduler CLI")


def _read_state_index_through_cli(run_dir, timeout_seconds):
    command = [
        sys.executable,
        "-m",
        "agentteam_runtime.cli",
        "--output-dir",
        str(run_dir),
        "--show-state-index",
    ]
    completed = _run_cli_subprocess(command, timeout_seconds)
    return _read_cli_json(completed, "state-index CLI")


def _run_cli_subprocess(command, timeout_seconds):
    try:
        return subprocess.run(
            command,
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"CLI timed out after {timeout_seconds}s: stdout={exc.stdout or ''} "
            f"stderr={exc.stderr or ''}"
        ) from exc


def _read_cli_json(completed, label):
    if completed.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit {completed.returncode}: "
            f"stdout={completed.stdout} stderr={completed.stderr}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON: {completed.stdout}") from exc


def _write_cli_smoke_fixtures(fixture_dir):
    fixture_dir.mkdir(parents=True, exist_ok=True)
    agent_pool = {
        "scheduler_agent_id": "agent-scheduler",
        "agents": [
            {
                "agent_id": "agent-live-codex-cli",
                "role": "repo_map_agent",
                "status": "idle",
                "inbox_path": "mailboxes/agent-live-codex-cli/inbox.jsonl",
            }
        ],
    }
    backlog = {
        "backlog_id": "BL-LIVE-CODEX-CLI-SMOKE",
        "items": [
            {
                "task_id": TASK_ID,
                "milestone_id": "M12b",
                "objective": (
                    f"Create {EXPECTED_FILE} containing a JSON object with "
                    '`"live_codex_cli_smoke": true`. Report exactly '
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


def _child_env():
    env = os.environ.copy()
    runtime_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = runtime_root if not current else f"{runtime_root}{os.pathsep}{current}"
    return env


if __name__ == "__main__":
    sys.exit(main())

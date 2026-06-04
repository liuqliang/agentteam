import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .integration_batch import verify_integration_batch
from .m0_runtime import CodexRuntimeAdapter, read_scheduler_state_index, run_simulation


ENV_GATE = "AGENTTEAM_RUN_LIVE_CODEX"
BATCH_ID = "BATCH-LIVE-CODEX-PIPELINE-SMOKE"
EXPECTED_CHANGED_FILE = "src/text_utils.py"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a gated live Codex pipeline smoke test."
    )
    parser.add_argument(
        "--output-dir",
        help="Directory for the temporary repo, fixtures, and runtime output.",
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

    output_dir = Path(
        args.output_dir or tempfile.mkdtemp(prefix="agentteam-live-codex-pipeline-")
    )
    output_dir = output_dir.resolve()
    try:
        summary = run_live_pipeline_smoke(
            output_dir,
            args.codex_command,
            args.timeout_seconds,
        )
    except Exception as exc:  # pragma: no cover - exercised by CLI failure behavior.
        _print_json(
            {"status": "failed", "error": str(exc), "output_dir": str(output_dir)}
        )
        return 1

    _print_json(summary)
    return 0 if summary["status"] == "completed" else 1


def run_live_pipeline_smoke(output_dir, codex_command=None, timeout_seconds=300):
    repo_path = output_dir / "repo"
    fixture_dir = output_dir / "fixtures"
    run_dir = output_dir / "run"
    if repo_path.exists():
        raise RuntimeError(f"refusing to reuse existing smoke repo: {repo_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _init_git_repo(repo_path)
    agent_pool_path, backlog_path = _write_pipeline_smoke_fixtures(fixture_dir)
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
    state_index = read_scheduler_state_index(run_dir)
    attempt = state_index["attempts"][0]
    repo_context_path = attempt.get("repo_context_path")
    role_context_path = _role_context_path_from_mailbox(run_dir)

    verification_command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
    ]
    batch = verify_integration_batch(
        repo_path,
        run_dir,
        BATCH_ID,
        verification_command,
        merge_verified_batch=True,
    )
    source_verification = _run_source_verification(repo_path, verification_command)
    runtime_event = _find_runtime_event(Path(result["events_path"]))
    changed_files = runtime_event["payload"]["changed_files"]
    actual_changed_files = result["diff_audit"]["actual_changed_files"]
    status = (
        "completed"
        if result["validation_status"] == "accepted"
        and result["integration_queue_status"] == "pending"
        and changed_files == [EXPECTED_CHANGED_FILE]
        and actual_changed_files == [EXPECTED_CHANGED_FILE]
        and batch["batch_status"] == "verified"
        and batch["verification_status"] == "passed"
        and batch["merge_status"] == "merged"
        and source_verification["source_repo_tests_passed"]
        else "failed"
    )

    return {
        "status": status,
        "validation_status": result["validation_status"],
        "integration_queue_status": result["integration_queue_status"],
        "integration_queue_item_id": result["integration_queue_item_id"],
        "batch_id": BATCH_ID,
        "batch_status": batch["batch_status"],
        "verification_status": batch["verification_status"],
        "verification_exit_code": batch["verification_exit_code"],
        "merge_status": batch["merge_status"],
        "merge_commit_sha": batch["merge_commit_sha"],
        "changed_files": changed_files,
        "actual_changed_files": actual_changed_files,
        "repo_context_path": repo_context_path,
        "role_context_path": role_context_path,
        "source_repo_tests_passed": source_verification["source_repo_tests_passed"],
        "source_repo_test_exit_code": source_verification["source_repo_test_exit_code"],
        "output_dir": str(output_dir),
        "repo_path": str(repo_path),
        "worktree_path": result["worktree_path"],
        "events_path": result["events_path"],
        "state_index": state_index,
    }


def _init_git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "config", "user.email", "agentteam@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "AgentTeam Smoke"], cwd=path, check=True)
    (path / "src").mkdir()
    (path / "tests").mkdir()
    (path / "README.md").write_text(
        "# live codex pipeline smoke fixture\n",
        encoding="utf-8",
    )
    (path / "src" / "text_utils.py").write_text(
        "\n".join(
            [
                "def normalize_slug(text):",
                "    return text.lower().replace(' ', '-')",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (path / "tests" / "test_text_utils.py").write_text(
        "\n".join(
            [
                "import unittest",
                "",
                "from src.text_utils import normalize_slug",
                "",
                "",
                "class NormalizeSlugTests(unittest.TestCase):",
                "    def test_normalizes_title_punctuation_and_spaces(self):",
                "        self.assertEqual(",
                "            normalize_slug('Hello, Agent Team!'),",
                "            'hello-agent-team',",
                "        )",
                "",
                "    def test_trims_separator_edges(self):",
                "        self.assertEqual(",
                "            normalize_slug('  Already -- Slug  '),",
                "            'already-slug',",
                "        )",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "README.md", "src/text_utils.py", "tests/test_text_utils.py"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial pipeline smoke fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _write_pipeline_smoke_fixtures(fixture_dir):
    fixture_dir.mkdir(parents=True, exist_ok=True)
    agent_pool = {
        "scheduler_agent_id": "agent-scheduler",
        "role_context_packages": {
            "repo_map_agent": {
                "context_notes": [
                    "Use repo map references for navigation only.",
                    "Use repo_context_path for task-specific selected files.",
                ],
                "include_repo_map_references": True,
            }
        },
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
        "backlog_id": "BL-LIVE-CODEX-PIPELINE-SMOKE",
        "items": [
            {
                "task_id": "TASK-LIVE-CODEX-PIPELINE-SMOKE",
                "milestone_id": "M36a",
                "objective": (
                    "Fix src/text_utils.py normalize_slug(text). It must lower-case "
                    "text, replace runs of non-alphanumeric characters with a single "
                    "hyphen, and trim leading/trailing hyphens. Read role_context_path "
                    "and repo_context_path before editing. Do not edit tests. Report "
                    f"exactly {EXPECTED_CHANGED_FILE} in changed_files."
                ),
                "backlog_status": "ready",
                "risk_target": "L0",
                "depends_on": [],
                "read_scope": ["src/", "tests/"],
                "write_scope": [EXPECTED_CHANGED_FILE],
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


def _role_context_path_from_mailbox(run_dir):
    mailboxes_dir = Path(run_dir) / "mailboxes"
    for inbox_path in sorted(mailboxes_dir.glob("*/inbox.jsonl")):
        for line in inbox_path.read_text(encoding="utf-8").splitlines():
            message = json.loads(line)
            role_context_path = message.get("payload", {}).get("role_context_path")
            if role_context_path:
                return role_context_path
    return None


def _find_runtime_event(events_path):
    for line in events_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["event_type"] == "runtime_output_received":
            return event
    raise RuntimeError(f"missing runtime_output_received event: {events_path}")


def _run_source_verification(repo_path, command):
    completed = subprocess.run(
        command,
        cwd=repo_path,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "source_repo_tests_passed": completed.returncode == 0,
        "source_repo_test_exit_code": completed.returncode,
    }


def _print_json(payload):
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())

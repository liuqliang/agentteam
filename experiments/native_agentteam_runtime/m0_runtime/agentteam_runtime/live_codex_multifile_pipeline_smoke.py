import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .integration_batch import verify_integration_batch
from .live_codex_pipeline_smoke import (
    _find_runtime_event,
    _print_json,
    _role_context_path_from_mailbox,
    _run_source_verification,
)
from .m0_runtime import CodexRuntimeAdapter, read_scheduler_state_index, run_simulation


ENV_GATE = "AGENTTEAM_RUN_LIVE_CODEX"
BATCH_ID = "BATCH-LIVE-CODEX-MULTIFILE-PIPELINE-SMOKE"
EXPECTED_CHANGED_FILES = ["docs/guide.md", "src/toc.py"]
EXPECTED_TOC_LINES = [
    "- [Install](#install)",
    "  - [Linux Setup](#linux-setup)",
    "- [Usage Tips](#usage-tips)",
]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run a gated live Codex multi-file pipeline smoke test."
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
        args.output_dir
        or tempfile.mkdtemp(prefix="agentteam-live-codex-multifile-pipeline-")
    )
    output_dir = output_dir.resolve()
    try:
        summary = run_live_multifile_pipeline_smoke(
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


def run_live_multifile_pipeline_smoke(
    output_dir,
    codex_command=None,
    timeout_seconds=300,
):
    repo_path = output_dir / "repo"
    fixture_dir = output_dir / "fixtures"
    run_dir = output_dir / "run"
    if repo_path.exists():
        raise RuntimeError(f"refusing to reuse existing smoke repo: {repo_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    _init_git_repo(repo_path)
    agent_pool_path, backlog_path = _write_multifile_smoke_fixtures(fixture_dir)
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
    guide_toc_updated = _guide_toc_updated(repo_path / "docs" / "guide.md")
    status = (
        "completed"
        if result["validation_status"] == "accepted"
        and result["integration_queue_status"] == "pending"
        and sorted(changed_files) == EXPECTED_CHANGED_FILES
        and sorted(actual_changed_files) == EXPECTED_CHANGED_FILES
        and batch["batch_status"] == "verified"
        and batch["verification_status"] == "passed"
        and batch["merge_status"] == "merged"
        and source_verification["source_repo_tests_passed"]
        and guide_toc_updated
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
        "expected_changed_files": EXPECTED_CHANGED_FILES,
        "repo_context_path": repo_context_path,
        "role_context_path": role_context_path,
        "guide_toc_updated": guide_toc_updated,
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
    (path / "docs").mkdir()
    (path / "README.md").write_text(
        "# live codex multifile pipeline smoke fixture\n",
        encoding="utf-8",
    )
    (path / "src" / "toc.py").write_text(
        "\n".join(
            [
                "def build_toc(markdown_text):",
                "    return []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (path / "docs" / "guide.md").write_text(
        "\n".join(
            [
                "# Agent Guide",
                "",
                "<!-- TOC:start -->",
                "<!-- TOC:end -->",
                "",
                "## Install",
                "",
                "Install the tool locally.",
                "",
                "### Linux Setup",
                "",
                "Use the standard Python runtime.",
                "",
                "## Usage Tips",
                "",
                "Keep generated artifacts small.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (path / "tests" / "test_toc.py").write_text(
        "\n".join(
            [
                "import unittest",
                "from pathlib import Path",
                "",
                "from src.toc import build_toc",
                "",
                "",
                "EXPECTED_TOC = [",
                "    '- [Install](#install)',",
                "    '  - [Linux Setup](#linux-setup)',",
                "    '- [Usage Tips](#usage-tips)',",
                "]",
                "",
                "",
                "class TocTests(unittest.TestCase):",
                "    def test_build_toc_uses_heading_levels_and_slugs(self):",
                "        markdown = '\\n'.join([",
                "            '# Agent Guide',",
                "            '',",
                "            '## Install',",
                "            '### Linux Setup',",
                "            '## Usage Tips',",
                "            '',",
                "        ])",
                "",
                "        self.assertEqual(build_toc(markdown), EXPECTED_TOC)",
                "",
                "    def test_guide_toc_matches_document_headings(self):",
                "        guide = Path('docs/guide.md').read_text(encoding='utf-8')",
                "",
                "        self.assertEqual(_toc_block(guide), build_toc(guide))",
                "        self.assertEqual(_toc_block(guide), EXPECTED_TOC)",
                "",
                "",
                "def _toc_block(markdown_text):",
                "    start = '<!-- TOC:start -->'",
                "    end = '<!-- TOC:end -->'",
                "    body = markdown_text.split(start, 1)[1].split(end, 1)[0]",
                "    return [line for line in body.splitlines() if line.strip()]",
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
        ["git", "add", "README.md", "src/toc.py", "docs/guide.md", "tests/test_toc.py"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial multifile pipeline smoke fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _write_multifile_smoke_fixtures(fixture_dir):
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
        "role_prompt_contracts": {
            "repo_map_agent": {
                "role_summary": "Implement the scoped code and documentation change.",
                "instructions": [
                    "Read role_context_path and repo_context_path before editing.",
                    "Keep changes inside the declared write_scope.",
                    "Report every changed file relative to the repository root.",
                ],
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
        "backlog_id": "BL-LIVE-CODEX-MULTIFILE-PIPELINE-SMOKE",
        "items": [
            {
                "task_id": "TASK-LIVE-CODEX-MULTIFILE-PIPELINE-SMOKE",
                "milestone_id": "M36b",
                "objective": (
                    "Implement src/toc.py build_toc(markdown_text) and update "
                    "docs/guide.md between the TOC markers. The TOC should list "
                    "the h2 and h3 headings as Markdown links, nesting h3 under "
                    "h2 with two leading spaces. Run python -m unittest discover "
                    "-s tests. Only edit docs/guide.md and src/toc.py. Report "
                    "exactly docs/guide.md and src/toc.py in changed_files."
                ),
                "backlog_status": "ready",
                "risk_target": "L1",
                "depends_on": [],
                "read_scope": ["src/", "tests/", "docs/"],
                "write_scope": EXPECTED_CHANGED_FILES,
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


def _guide_toc_updated(guide_path):
    if not guide_path.exists():
        return False
    guide_text = guide_path.read_text(encoding="utf-8")
    return all(line in guide_text for line in EXPECTED_TOC_LINES)


if __name__ == "__main__":
    sys.exit(main())

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.live_codex_smoke import (
    SUPPORTED_NON_WORKER_INVOCATION_INVENTORY,
)


ROOT = Path(__file__).resolve().parents[2]


class LiveCodexSmokeTests(unittest.TestCase):
    def test_live_codex_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "smoke"
            fake_codex = tmp_path / "fake_codex.py"
            _write_fake_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["expected_file"], "generated/live_codex_smoke.json")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "live_codex_smoke.json").exists()
            )
            controller_roots = list(
                (
                    output_dir
                    / "run"
                    / "state"
                    / "controller_invocations"
                    / "development_smoke"
                ).glob("DEVELOPMENT-SMOKE-SESSION-*")
            )
            self.assertEqual(len(controller_roots), 1)
            claim = json.loads(
                (controller_roots[0] / "controller_claim.json").read_text(
                    encoding="utf-8"
                )
            )
            invocation_dirs = list(
                (controller_roots[0] / "model_invocations").glob("INV-*")
            )
            self.assertEqual(len(invocation_dirs), 1)
            started = json.loads(
                (invocation_dirs[0] / "started.json").read_text(
                    encoding="utf-8"
                )
            )
            terminal = json.loads(
                (invocation_dirs[0] / "terminal.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(claim["usage_stage"], "development_smoke")
            self.assertEqual(started["usage_stage"], "development_smoke")
            self.assertEqual(
                started["runtime_execution_session_id"],
                claim["runtime_execution_session_id"],
            )
            self.assertEqual(
                terminal["terminal_writer"],
                "development_smoke_controller",
            )

    def test_supported_non_worker_inventory_matches_tracked_smoke_sources(self):
        runtime_dir = ROOT / "m0_runtime" / "agentteam_runtime"
        tracked_smokes = {
            path.name
            for path in runtime_dir.glob("live_codex*_smoke.py")
            if path.name != "usage_live_smoke.py"
        }
        inventory_smokes = {
            reference.split(":", 1)[0]
            for key, reference in SUPPORTED_NON_WORKER_INVOCATION_INVENTORY.items()
            if key.startswith("development_smoke_")
        }

        self.assertEqual(
            tracked_smokes,
            {
                "live_codex_smoke.py",
                "live_codex_scheduler_smoke.py",
                "live_codex_repo_context_smoke.py",
                "live_codex_pipeline_smoke.py",
                "live_codex_multifile_pipeline_smoke.py",
                "live_codex_cli_smoke.py",
            },
        )
        self.assertEqual(inventory_smokes, tracked_smokes)
        self.assertEqual(
            {
                "taskpack_author",
                "follow_up_author",
                "runtime_diagnostic",
            },
            set(SUPPORTED_NON_WORKER_INVOCATION_INVENTORY)
            - {
                key
                for key in SUPPORTED_NON_WORKER_INVOCATION_INVENTORY
                if key.startswith("development_smoke_")
            },
        )
        for filename in sorted(tracked_smokes):
            source = (runtime_dir / filename).read_text(encoding="utf-8")
            self.assertIn("DevelopmentSmokeCodexRuntimeAdapter", source)

    def test_live_codex_scheduler_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "scheduler-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_scheduler_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_scheduler_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "scheduler-smoke"
            fake_codex = tmp_path / "fake_codex.py"
            _write_fake_codex(fake_codex, changed_file="generated/live_codex_scheduler_smoke.json")
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_scheduler_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(
                summary["processed_task_ids"],
                ["TASK-LIVE-CODEX-SCHEDULER-SMOKE"],
            )
            self.assertTrue(summary["expected_file_exists"])
            self.assertEqual(summary["state_index"]["tasks"][0]["task_status"], "done")
            self.assertEqual(
                summary["state_index"]["runtime_sessions"][0]["session_status"],
                "stopped",
            )

    def test_live_codex_cli_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "cli-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_cli_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_cli_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "cli-smoke"
            fake_codex = tmp_path / "fake_codex.py"
            _write_fake_codex(fake_codex, changed_file="generated/live_codex_cli_smoke.json")
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_cli_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(
                summary["processed_task_ids"],
                ["TASK-LIVE-CODEX-CLI-SMOKE"],
            )
            self.assertTrue(summary["expected_file_exists"])
            self.assertEqual(summary["state_index"]["tasks"][0]["task_status"], "done")
            self.assertEqual(
                summary["state_index"]["runtime_sessions"][0]["runtime_adapter"],
                "CodexRuntimeAdapter",
            )
            self.assertEqual(
                summary["state_index"]["runtime_sessions"][0]["session_status"],
                "stopped",
            )

    def test_live_codex_repo_context_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "repo-context-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_repo_context_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_repo_context_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "repo-context-smoke"
            fake_codex = tmp_path / "fake_repo_context_codex.py"
            _write_fake_repo_context_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_repo_context_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["expected_selected_file"],
                "pkg/context_target.py",
            )
            self.assertEqual(
                summary["reported_selected_file"],
                "pkg/context_target.py",
            )
            self.assertTrue(summary["repo_context_path"])
            self.assertTrue(summary["expected_file_exists"])

    def test_live_codex_pipeline_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "pipeline-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_pipeline_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "pipeline-smoke"
            fake_codex = tmp_path / "fake_pipeline_codex.py"
            _write_fake_pipeline_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["integration_queue_status"], "pending")
            self.assertEqual(summary["batch_status"], "verified")
            self.assertEqual(summary["verification_status"], "passed")
            self.assertEqual(summary["merge_status"], "merged")
            self.assertEqual(summary["changed_files"], ["src/text_utils.py"])
            self.assertEqual(summary["actual_changed_files"], ["src/text_utils.py"])
            self.assertTrue(summary["repo_context_path"])
            self.assertTrue(summary["role_context_path"])
            self.assertTrue(summary["source_repo_tests_passed"])
            self.assertTrue(
                (Path(summary["repo_path"]) / "src" / "text_utils.py").exists()
            )

    def test_live_codex_multifile_pipeline_smoke_skips_without_env_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "multifile-pipeline-smoke"
            env = os.environ.copy()
            env.pop("AGENTTEAM_RUN_LIVE_CODEX", None)
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_multifile_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
            self.assertFalse(output_dir.exists())

    def test_live_codex_multifile_pipeline_smoke_runs_with_fake_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "multifile-pipeline-smoke"
            fake_codex = tmp_path / "fake_multifile_pipeline_codex.py"
            _write_fake_multifile_pipeline_codex(fake_codex)
            env = os.environ.copy()
            env["AGENTTEAM_RUN_LIVE_CODEX"] = "1"
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.live_codex_multifile_pipeline_smoke",
                    "--output-dir",
                    str(output_dir),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            expected_files = ["docs/guide.md", "src/toc.py"]
            guide_text = (Path(summary["repo_path"]) / "docs" / "guide.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["integration_queue_status"], "pending")
            self.assertEqual(summary["batch_status"], "verified")
            self.assertEqual(summary["verification_status"], "passed")
            self.assertEqual(summary["merge_status"], "merged")
            self.assertEqual(sorted(summary["changed_files"]), expected_files)
            self.assertEqual(sorted(summary["actual_changed_files"]), expected_files)
            self.assertTrue(summary["repo_context_path"])
            self.assertTrue(summary["role_context_path"])
            self.assertTrue(summary["source_repo_tests_passed"])
            self.assertIn("- [Install](#install)", guide_text)
            self.assertIn("  - [Linux Setup](#linux-setup)", guide_text)


def _write_fake_codex(path, changed_file="generated/live_codex_smoke.json"):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'mode': 'fake', 'saw_prompt': 'live_codex_smoke' in prompt}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'mode': 'fake'}",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_pipeline_codex(path):
    changed_file = "src/text_utils.py"
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "mailbox = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "repo_context_path = pathlib.Path(mailbox['payload']['repo_context_path'])",
                "role_context_path = pathlib.Path(mailbox['payload']['role_context_path'])",
                "repo_context = json.loads(repo_context_path.read_text(encoding='utf-8'))",
                "selected_paths = [entry['path'] for entry in repo_context['selected_files']]",
                f"target = worktree / {changed_file!r}",
                "target.write_text('\\n'.join([",
                "    'import re',",
                "    '',",
                "    'def normalize_slug(text):',",
                "    \"    normalized = re.sub(r'[^a-z0-9]+', '-', text.lower())\",",
                "    \"    return normalized.strip('-')\",",
                "    '',",
                "]), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'mode': 'fake_pipeline',",
                "        'selected_paths': selected_paths,",
                "        'repo_context_path': str(repo_context_path),",
                "        'role_context_path': str(role_context_path),",
                "    },",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_multifile_pipeline_codex(path):
    changed_files = ["docs/guide.md", "src/toc.py"]
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "mailbox = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "repo_context_path = pathlib.Path(mailbox['payload']['repo_context_path'])",
                "role_context_path = pathlib.Path(mailbox['payload']['role_context_path'])",
                "repo_context = json.loads(repo_context_path.read_text(encoding='utf-8'))",
                "json.loads(role_context_path.read_text(encoding='utf-8'))",
                "selected_paths = [entry['path'] for entry in repo_context['selected_files']]",
                "toc_source = worktree / 'src' / 'toc.py'",
                "toc_source.write_text('\\n'.join([",
                "    'import re',",
                "    '',",
                "    'def _slugify(title):',",
                "    \"    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')\",",
                "    '',",
                "    'def build_toc(markdown_text):',",
                "    '    lines = []',",
                "    '    in_toc = False',",
                "    '    for raw_line in markdown_text.splitlines():',",
                "    '        line = raw_line.strip()',",
                "    \"        if line == '<!-- TOC:start -->':\",",
                "    '            in_toc = True',",
                "    '            continue',",
                "    \"        if line == '<!-- TOC:end -->':\",",
                "    '            in_toc = False',",
                "    '            continue',",
                "    '        if in_toc:',",
                "    '            continue',",
                "    \"        if raw_line.startswith('## '):\",",
                "    '            title = raw_line[3:].strip()',",
                "    '            lines.append(f\\'- [{title}](#{_slugify(title)})\\')',",
                "    \"        elif raw_line.startswith('### '):\",",
                "    '            title = raw_line[4:].strip()',",
                "    '            lines.append(f\\'  - [{title}](#{_slugify(title)})\\')',",
                "    '    return lines',",
                "    '',",
                "]), encoding='utf-8')",
                "guide = worktree / 'docs' / 'guide.md'",
                "guide.write_text('\\n'.join([",
                "    '# Agent Guide',",
                "    '',",
                "    '<!-- TOC:start -->',",
                "    '- [Install](#install)',",
                "    '  - [Linux Setup](#linux-setup)',",
                "    '- [Usage Tips](#usage-tips)',",
                "    '<!-- TOC:end -->',",
                "    '',",
                "    '## Install',",
                "    '',",
                "    'Install the tool locally.',",
                "    '',",
                "    '### Linux Setup',",
                "    '',",
                "    'Use the standard Python runtime.',",
                "    '',",
                "    '## Usage Tips',",
                "    '',",
                "    'Keep generated artifacts small.',",
                "    '',",
                "]), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': {changed_files!r},",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'mode': 'fake_multifile_pipeline',",
                "        'selected_paths': selected_paths,",
                "        'repo_context_path': str(repo_context_path),",
                "        'role_context_path': str(role_context_path),",
                "    },",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_repo_context_codex(path):
    changed_file = "generated/live_codex_repo_context_smoke.json"
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "mailbox = json.loads(prompt.rsplit('Mailbox message:', 1)[1].strip())",
                "repo_context_path = pathlib.Path(mailbox['payload']['repo_context_path'])",
                "repo_context = json.loads(repo_context_path.read_text(encoding='utf-8'))",
                "selected_file = repo_context['selected_files'][0]['path']",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({",
                "    'repo_context_smoke': True,",
                "    'selected_file': selected_file,",
                "    'repo_context_path': str(repo_context_path),",
                "}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {",
                "        'adapter': 'codex',",
                "        'mode': 'fake',",
                "        'selected_file': selected_file,",
                "        'repo_context_path': str(repo_context_path),",
                "    }",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()

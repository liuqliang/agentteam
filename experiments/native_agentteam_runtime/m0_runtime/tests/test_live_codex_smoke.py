import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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


def _write_fake_codex(path):
    changed_file = "generated/live_codex_smoke.json"
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


if __name__ == "__main__":
    unittest.main()

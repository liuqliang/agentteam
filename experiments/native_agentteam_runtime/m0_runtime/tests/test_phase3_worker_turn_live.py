from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agentteam_runtime.phase3_worker_turn_live import _patch_id, main


class Phase3WorkerTurnLiveTests(unittest.TestCase):
    def test_cli_is_gated_before_live_execution(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            status = main(
                [
                    "--source-repository",
                    "/missing/source",
                    "--source-commit",
                    "a" * 40,
                    "--task",
                    "/missing/task.json",
                    "--output-dir",
                    "/missing/output",
                    "--run-id",
                    "test-run",
                ]
            )
        self.assertEqual(status, 0)
        self.assertIn('"status": "skipped"', output.getvalue())

    def test_patch_id_uses_stable_git_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                check=True,
            )
            source = repo / "source.py"
            source.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "source.py"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
                check=True,
            )
            source.write_text("value = 2\n", encoding="utf-8")
            patch_path = Path(temporary) / "candidate.patch"
            patch_path.write_bytes(
                subprocess.run(
                    ["git", "-C", str(repo), "diff", "--binary", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout
            )
            identity = _patch_id(patch_path)
            self.assertEqual(len(identity), 40)
            self.assertEqual(identity, _patch_id(patch_path))


if __name__ == "__main__":
    unittest.main()

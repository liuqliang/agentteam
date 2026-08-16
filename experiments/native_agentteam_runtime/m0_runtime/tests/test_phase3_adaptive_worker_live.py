from __future__ import annotations

import io
import hashlib
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agentteam_runtime.phase3_adaptive_worker_live import (
    _validate_handoff_binding,
    main,
)


class Phase3AdaptiveWorkerLiveTests(unittest.TestCase):
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
                    "--repo-map-handoff",
                    "/missing/handoff.json",
                    "--output-dir",
                    "/missing/output",
                    "--run-id",
                    "test-run",
                ]
            )
        self.assertEqual(status, 0)
        self.assertIn('"status": "skipped"', output.getvalue())

    def test_handoff_must_match_task_commit_and_have_no_gaps(self):
        task = {"task_id": "TASK-1"}
        task["objective"] = "Implement the bounded change."
        handoff = {
            "task_id": "TASK-1",
            "repository": {"commit": "a" * 40},
            "objective_sha256": hashlib.sha256(
                task["objective"].encode("utf-8")
            ).hexdigest(),
            "semantic_gaps": [],
        }
        _validate_handoff_binding(task, handoff, "a" * 40)
        with self.assertRaisesRegex(RuntimeError, "task binding"):
            _validate_handoff_binding(
                task,
                {**handoff, "task_id": "TASK-2"},
                "a" * 40,
            )
        with self.assertRaisesRegex(RuntimeError, "source commit"):
            _validate_handoff_binding(task, handoff, "b" * 40)
        with self.assertRaisesRegex(RuntimeError, "objective binding"):
            _validate_handoff_binding(
                {**task, "objective": "Different objective."},
                handoff,
                "a" * 40,
            )
        with self.assertRaisesRegex(RuntimeError, "semantic gaps"):
            _validate_handoff_binding(
                task,
                {**handoff, "semantic_gaps": ["unknown call path"]},
                "a" * 40,
            )


if __name__ == "__main__":
    unittest.main()

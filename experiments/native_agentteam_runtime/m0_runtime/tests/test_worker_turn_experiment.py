from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.worker_turn_checkpoint import WorkerTurnCheckpointError
from agentteam_runtime.worker_turn_experiment import WorkerTurnExperimentRunner


class WorkerTurnExperimentTests(unittest.TestCase):
    def _git(self, repo, *arguments):
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout

    def _repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Test")
        self._git(repo, "config", "user.email", "test@example.com")
        (repo / "source.py").write_text("value = 1\n", encoding="utf-8")
        self._git(repo, "add", "source.py")
        self._git(repo, "commit", "-q", "-m", "base")
        return repo

    def _task(self):
        return {
            "task_id": "TASK-BOUNDED",
            "objective": "Change value to 2 and verify it.",
            "read_scope": ["source.py"],
            "write_scope": ["source.py"],
            "required_deliverables": ["implemented_change", "verification_summary"],
            "risk_target": "L1",
        }

    def _result(self, stage, total):
        remaining = {
            "locate": "Change source.py in the implementation turn.",
            "implement": "Verify source.py contains value 2.",
            "verify": "",
        }[stage]
        return {
            "result_status": "completed",
            "changed_files": ["source.py"] if stage != "locate" else [],
            "token_usage": {
                "usage_status": "reported",
                "input_tokens": total - 10,
                "cached_input_tokens": total // 2,
                "output_tokens": 10,
                "reasoning_tokens": 1,
                "total_tokens": total,
            },
            "output": {
                "turn_checkpoint": {
                    "completed_actions": [f"completed {stage}"],
                    "key_findings": [
                        {"path": "source.py", "symbol": "value", "summary": "target"}
                    ],
                    "decisions": [],
                    "verification": (
                        [
                            {
                                "command": [
                                    "python3",
                                    "-m",
                                    "compileall",
                                    "source.py",
                                ],
                                "status": "passed",
                            }
                        ]
                        if stage == "verify"
                        else []
                    ),
                    "remaining_objective": remaining,
                    "source_paths": ["source.py"],
                }
            },
        }

    def test_runs_three_stages_in_one_worktree_and_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            calls = []

            def invoke(stage, message, worktree):
                calls.append(stage)
                if stage == "implement":
                    (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                self.assertEqual(message["payload"]["turn_stage"], stage)
                if stage != "locate":
                    self.assertTrue(message["payload"]["turn_checkpoint_path"])
                return self._result(stage, 100)

            runner = WorkerTurnExperimentRunner(
                worktree_path=repo,
                output_dir=root / "out",
                run_id="bounded-run",
                task=self._task(),
                invoke=invoke,
                maximum_total_tokens=500,
            )
            state = runner.run()
            self.assertEqual(state["status"], "completed")
            self.assertEqual(calls, ["locate", "implement", "verify"])
            self.assertEqual(state["usage"]["totals"]["total_tokens"], 300)
            self.assertEqual(self._git(repo, "diff", "--name-only").strip(), "source.py")

            resumed = runner.run()
            self.assertEqual(resumed, state)
            self.assertEqual(calls, ["locate", "implement", "verify"])

    def test_settled_budget_stops_before_next_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            calls = []

            def invoke(stage, _message, _worktree):
                calls.append(stage)
                return self._result(stage, 120)

            state = WorkerTurnExperimentRunner(
                worktree_path=repo,
                output_dir=root / "out",
                run_id="budget-run",
                task=self._task(),
                invoke=invoke,
                maximum_total_tokens=100,
            ).run()
            self.assertEqual(calls, ["locate"])
            self.assertEqual(state["status"], "stopped")
            self.assertEqual(state["stop_reason"], "settled_token_ceiling_reached")

    def test_terminal_turn_requires_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)

            def invoke(stage, _message, worktree):
                if stage == "implement":
                    (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                result = self._result(stage, 50)
                if stage == "verify":
                    result["output"].pop("turn_checkpoint")
                return result

            state = WorkerTurnExperimentRunner(
                worktree_path=repo,
                output_dir=root / "out",
                run_id="terminal-checkpoint-run",
                task=self._task(),
                invoke=invoke,
                maximum_total_tokens=500,
            ).run()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["stop_reason"], "missing_turn_checkpoint")

    def test_restart_rejects_external_mutation_after_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)

            def locate_only(stage, _message, _worktree):
                result = self._result(stage, 120)
                result["result_status"] = "failed"
                return result

            runner = WorkerTurnExperimentRunner(
                worktree_path=repo,
                output_dir=root / "out",
                run_id="stale-run",
                task=self._task(),
                invoke=locate_only,
                maximum_total_tokens=500,
            )
            state = runner.run()
            self.assertEqual(state["status"], "stopped")

            # Simulate a resumable in-progress state and then mutate the worktree.
            state["status"] = "running"
            state["next_stage"] = "implement"
            runner._write_state(state)
            (repo / "source.py").write_text("external = True\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "stale"):
                runner.run()


if __name__ == "__main__":
    unittest.main()

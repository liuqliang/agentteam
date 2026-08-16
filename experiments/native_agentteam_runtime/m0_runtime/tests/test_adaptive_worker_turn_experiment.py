from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.adaptive_worker_turn_experiment import (
    AdaptiveWorkerTurnExperimentRunner,
    classify_controller_verification,
    profile_execution_observations,
)
from agentteam_runtime.worker_turn_checkpoint import WorkerTurnCheckpointError


class AdaptiveWorkerTurnExperimentTests(unittest.TestCase):
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
            "task_id": "TASK-ADAPTIVE",
            "objective": "Change value to 2 and verify it.",
            "read_scope": ["source.py"],
            "write_scope": ["source.py"],
            "input_artifacts": ["repo-map-handoff.json"],
        }

    def _result(self, stage, total=100):
        return {
            "result_status": "completed",
            "changed_files": ["source.py"],
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
                    "key_findings": [{"path": "source.py", "summary": "target"}],
                    "decisions": [],
                    "verification": [],
                    "remaining_objective": (
                        "Run controller verification." if stage == "implement" else ""
                    ),
                    "source_paths": ["source.py"],
                },
                "verification_additions": [
                    {
                        "label": "focused",
                        "command": ["python3", "-m", "compileall", "source.py"],
                        "reason": "Check syntax.",
                    }
                ],
            },
        }

    def _verification(self, status, *, stderr="", exit_code=0):
        return {
            "integration_verification_additions_status": status,
            "integration_verification_additions": [
                {
                    "label": "focused",
                    "command": ["python3", "-m", "compileall", "source.py"],
                    "verification_addition_status": status,
                    "verification_addition_exit_code": exit_code,
                    "verification_addition_stdout": "",
                    "verification_addition_stderr": stderr,
                    "verification_addition_rejection_reason": None,
                }
            ],
        }

    def _runner(self, root, repo, invoke, verify, **overrides):
        return AdaptiveWorkerTurnExperimentRunner(
            worktree_path=repo,
            output_dir=root / "out",
            run_id="adaptive-run",
            task=self._task(),
            invoke=invoke,
            run_verification=verify,
            maximum_total_tokens=overrides.get("maximum_total_tokens", 500),
            allow_repair=overrides.get("allow_repair", True),
        )

    def test_starts_with_implementation_and_pass_does_not_launch_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            calls = []

            def invoke(stage, message, worktree):
                calls.append(stage)
                self.assertEqual(stage, "implement")
                self.assertEqual(message["payload"]["turn_stage"], "implement")
                (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                return self._result(stage)

            runner = self._runner(
                root,
                repo,
                invoke,
                lambda _additions, _worktree: self._verification("passed"),
            )
            state = runner.run()
            self.assertEqual(state["status"], "completed")
            self.assertEqual(calls, ["implement"])
            self.assertEqual(state["repair_invocations"], 0)
            self.assertEqual(state["usage"]["totals"]["total_tokens"], 100)
            self.assertEqual(len(state["controller_verifications"]), 1)

            self.assertEqual(runner.run(), state)
            self.assertEqual(calls, ["implement"])

    def test_environment_failure_does_not_launch_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            calls = []

            def invoke(stage, _message, worktree):
                calls.append(stage)
                (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                return self._result(stage)

            state = self._runner(
                root,
                repo,
                invoke,
                lambda _additions, _worktree: self._verification(
                    "failed", stderr="python3: No module named pytest", exit_code=1
                ),
            ).run()
            self.assertEqual(state["status"], "stopped")
            self.assertEqual(state["stop_reason"], "environment_failure")
            self.assertEqual(calls, ["implement"])

    def test_code_failure_launches_at_most_one_repair_then_reverifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            calls = []
            verification_calls = []

            def invoke(stage, message, worktree):
                calls.append(stage)
                if stage == "implement":
                    (worktree / "source.py").write_text("value = 3\n", encoding="utf-8")
                else:
                    self.assertEqual(
                        message["payload"]["verification_failure_packet"]["route"],
                        "code_semantic_failure",
                    )
                    (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                return self._result(stage)

            def verify(_additions, _worktree):
                verification_calls.append(True)
                if len(verification_calls) == 1:
                    return self._verification(
                        "failed", stderr="AssertionError: expected 2", exit_code=1
                    )
                return self._verification("passed")

            state = self._runner(root, repo, invoke, verify).run()
            self.assertEqual(state["status"], "completed")
            self.assertEqual(calls, ["implement", "repair"])
            self.assertEqual(state["repair_invocations"], 1)
            self.assertEqual(len(state["controller_verifications"]), 2)
            self.assertEqual(state["usage"]["totals"]["total_tokens"], 200)

    def test_restart_rejects_external_worktree_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)

            def invoke(stage, _message, worktree):
                (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                result = self._result(stage)
                result["result_status"] = "failed"
                return result

            runner = self._runner(
                root,
                repo,
                invoke,
                lambda _additions, _worktree: self._verification("passed"),
            )
            state = runner.run()
            self.assertEqual(state["status"], "stopped")
            state["status"] = "running"
            state["phase"] = "controller_verification"
            runner._write_state(state)
            (repo / "source.py").write_text("external = True\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "stale"):
                runner.run()

    def test_worker_commit_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)

            def invoke(stage, _message, worktree):
                (worktree / "source.py").write_text("value = 2\n", encoding="utf-8")
                self._git(worktree, "add", "source.py")
                self._git(worktree, "commit", "-q", "-m", "worker commit")
                return self._result(stage)

            state = self._runner(
                root,
                repo,
                invoke,
                lambda _additions, _worktree: self._verification("passed"),
            ).run()
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["stop_reason"], "worker_changed_source_head")
            self.assertIsNone(state["model_turns"][0]["checkpoint_path"])

    def test_execution_observation_records_repeats_without_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "stdout.jsonl"
            events = []
            for command in (
                "/bin/bash -lc 'git status --short'",
                "/bin/bash -lc 'git status --short'",
                "/bin/bash -lc 'sed -n 1,20p source.py'",
                "/bin/bash -lc 'sed -n 1,20p source.py'",
                "/bin/bash -lc 'printf x > source.py'",
            ):
                events.append(
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": command,
                                "aggregated_output": "large output must not be retained",
                            },
                        }
                    )
                )
            transcript.write_text("\n".join(events), encoding="utf-8")
            report = profile_execution_observations([transcript])
            self.assertEqual(report["mode"], "record_only")
            self.assertEqual(report["command_count"], 5)
            self.assertEqual(report["repeated_command_count"], 2)
            self.assertEqual(report["simple_read_only_extra_execution_count"], 2)
            self.assertNotIn("large output", json.dumps(report))

    def test_verification_classifier_is_conservative(self):
        self.assertEqual(
            classify_controller_verification(self._verification("passed")), "passed"
        )
        self.assertEqual(
            classify_controller_verification(
                self._verification("failed", stderr="AssertionError", exit_code=1)
            ),
            "code_semantic_failure",
        )
        rejected = self._verification("rejected", exit_code=None)
        rejected["integration_verification_additions"][0][
            "verification_addition_status"
        ] = "rejected"
        self.assertEqual(
            classify_controller_verification(rejected),
            "verification_policy_failure",
        )


if __name__ == "__main__":
    unittest.main()

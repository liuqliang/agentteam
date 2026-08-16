from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.worker_turn_checkpoint import (
    WorkerTurnCheckpointError,
    aggregate_worker_turn_usage,
    build_worker_turn_checkpoint,
    load_worker_turn_checkpoint,
    profile_worker_turn_transcripts,
    publish_worker_turn_checkpoint,
    validate_worker_turn_checkpoint,
)


class WorkerTurnCheckpointTests(unittest.TestCase):
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

    def _semantic(self, remaining="Implement the selected change."):
        return {
            "completed_actions": ["Inspected source.py"],
            "key_findings": [
                {"path": "source.py", "symbol": "value", "summary": "Entry point"}
            ],
            "decisions": [{"decision": "edit value", "rationale": "task requires it"}],
            "verification": [],
            "remaining_objective": remaining,
            "source_paths": ["source.py"],
        }

    def test_checkpoint_is_outside_diff_and_detects_stale_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(Path(temporary))
            checkpoint = build_worker_turn_checkpoint(
                task_id="TASK-1",
                attempt_id="ATTEMPT-1",
                turn_index=1,
                stage="locate",
                worktree_path=repo,
                semantic_state=self._semantic(),
            )
            path = publish_worker_turn_checkpoint(repo, "run-1", checkpoint)

            self.assertIn("agentteam-worker-turns", str(path))
            self.assertEqual(self._git(repo, "status", "--short"), "")
            loaded = load_worker_turn_checkpoint(
                path,
                task_id="TASK-1",
                attempt_id="ATTEMPT-1",
                expected_stage="implement",
                worktree_path=repo,
            )
            self.assertEqual(loaded["next_stage"], "implement")

            (repo / "source.py").write_text("value = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "stale"):
                load_worker_turn_checkpoint(path, worktree_path=repo)

    def test_lineage_digest_transition_and_path_validation_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(Path(temporary))
            checkpoint = build_worker_turn_checkpoint(
                task_id="TASK-1",
                attempt_id="ATTEMPT-1",
                turn_index=1,
                stage="locate",
                worktree_path=repo,
                semantic_state=self._semantic(),
            )
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "task lineage"):
                validate_worker_turn_checkpoint(checkpoint, task_id="TASK-2")
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "expected stage"):
                validate_worker_turn_checkpoint(checkpoint, expected_stage="verify")

            tampered = dict(checkpoint)
            tampered["remaining_objective"] = "Different"
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "digest"):
                validate_worker_turn_checkpoint(tampered)

            semantic = self._semantic()
            semantic["source_paths"] = ["../secret"]
            with self.assertRaisesRegex(WorkerTurnCheckpointError, "escapes"):
                build_worker_turn_checkpoint(
                    task_id="TASK-1",
                    attempt_id="ATTEMPT-1",
                    turn_index=1,
                    stage="locate",
                    worktree_path=repo,
                    semantic_state=semantic,
                )

    def test_turn_sequence_preserves_diff_and_allows_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(Path(temporary))
            locate = build_worker_turn_checkpoint(
                task_id="TASK-1",
                attempt_id="ATTEMPT-1",
                turn_index=1,
                stage="locate",
                worktree_path=repo,
                semantic_state=self._semantic(),
            )
            locate_path = publish_worker_turn_checkpoint(repo, "run-1", locate)
            load_worker_turn_checkpoint(
                locate_path,
                expected_stage="implement",
                worktree_path=repo,
            )

            (repo / "source.py").write_text("value = 2\n", encoding="utf-8")
            implement = build_worker_turn_checkpoint(
                task_id="TASK-1",
                attempt_id="ATTEMPT-1",
                turn_index=2,
                stage="implement",
                worktree_path=repo,
                semantic_state={
                    **self._semantic("Run focused verification."),
                    "completed_actions": ["Changed source.py value"],
                },
            )
            implement_path = publish_worker_turn_checkpoint(repo, "run-1", implement)
            restored = load_worker_turn_checkpoint(
                implement_path,
                expected_stage="verify",
                worktree_path=repo,
            )
            self.assertEqual(restored["changed_files"], ["source.py"])
            self.assertIn("-value = 1", self._git(repo, "diff"))

    def test_usage_aggregation_is_per_invocation_and_exact(self):
        result = aggregate_worker_turn_usage(
            [
                {
                    "turn_stage": "locate",
                    "token_usage": {
                        "usage_status": "reported",
                        "input_tokens": 100,
                        "cached_input_tokens": 60,
                        "output_tokens": 10,
                        "reasoning_tokens": 2,
                        "total_tokens": 110,
                    },
                },
                {
                    "turn_stage": "implement",
                    "token_usage": {
                        "usage_status": "reported",
                        "input_tokens": 200,
                        "cached_input_tokens": 150,
                        "output_tokens": 20,
                        "reasoning_tokens": 3,
                        "total_tokens": 220,
                    },
                },
            ]
        )
        self.assertEqual(result["invocation_count"], 2)
        self.assertEqual(result["totals"]["total_tokens"], 330)
        self.assertEqual(result["totals"]["uncached_input_tokens"], 90)

        without_status = aggregate_worker_turn_usage(
            [
                {
                    "turn_stage": "locate",
                    "token_usage": {
                        "input_tokens": 90,
                        "cached_input_tokens": 50,
                        "output_tokens": 10,
                        "reasoning_tokens": 1,
                        "total_tokens": 100,
                    },
                }
            ]
        )
        self.assertEqual(without_status["totals"]["total_tokens"], 100)

        with self.assertRaisesRegex(WorkerTurnCheckpointError, "unavailable"):
            aggregate_worker_turn_usage([{"turn_stage": "verify", "token_usage": {}}])

    def test_transcript_profile_counts_cross_turn_repeated_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "sed -n '1,80p' source.py tests/test_source.py",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "rg value source.py",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            profile = profile_worker_turn_transcripts(
                [first, second], ["source.py", "tests/test_source.py"]
            )
            self.assertEqual(profile["command_count"], 2)
            self.assertEqual(profile["cross_turn_repeated_reads"], 1)
            self.assertEqual(profile["source_path_turn_counts"]["source.py"], 2)

    def test_remaining_objective_list_is_normalized_for_provider_compatibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = self._repo(Path(temporary))
            semantic = self._semantic()
            semantic["remaining_objective"] = ["Inspect source.py", "Implement change"]
            checkpoint = build_worker_turn_checkpoint(
                task_id="TASK-1",
                attempt_id="ATTEMPT-1",
                turn_index=1,
                stage="locate",
                worktree_path=repo,
                semantic_state=semantic,
            )
            self.assertEqual(
                checkpoint["remaining_objective"],
                "- Inspect source.py\n- Implement change",
            )


if __name__ == "__main__":
    unittest.main()

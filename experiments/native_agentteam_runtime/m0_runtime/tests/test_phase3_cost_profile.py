from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentteam_runtime.phase3_cost_profile import (
    Phase3CostProfileError,
    profile_phase3_pilot,
    render_phase3_cost_profile,
)


class Phase3CostProfileTests(unittest.TestCase):
    def test_profiles_stage_cached_input_and_infrastructure_waste(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=True)
            bundle = self._bundle(run_dir)

            with patch(
                "agentteam_runtime.phase3_cost_profile.load_experiment_result_bundle",
                return_value={"bundle": bundle},
            ):
                profile = profile_phase3_pilot(root)

        self.assertEqual(profile["run_count"], 1)
        self.assertEqual(profile["complete_usage_run_count"], 1)
        self.assertEqual(
            profile["reported_token_totals"],
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "uncached_input_tokens": 20,
                "output_tokens": 10,
                "reasoning_tokens": 3,
                "total_tokens": 110,
            },
        )
        self.assertEqual(
            profile["by_stage"]["implementation_worker"][
                "reported_token_totals"
            ]["total_tokens"],
            110,
        )
        self.assertEqual(
            profile["by_role"]["implementation_worker"][
                "reported_token_totals"
            ]["total_tokens"],
            110,
        )
        self.assertEqual(
            profile["by_outcome"]["infrastructure_waste"][
                "reported_token_totals"
            ]["total_tokens"],
            110,
        )
        self.assertEqual(profile["wall_time_seconds"]["model_invocations"], 7.0)
        self.assertEqual(
            profile["wall_time_seconds"][
                "orchestration_and_common_acceptance"
            ],
            5.0,
        )
        self.assertEqual(profile["wall_time_seconds"]["official_evaluator"], 30.0)
        self.assertIn("uncached_input=20", render_phase3_cost_profile(profile))

    def test_marks_missing_official_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=False)
            bundle = self._bundle(run_dir)

            with patch(
                "agentteam_runtime.phase3_cost_profile.load_experiment_result_bundle",
                return_value={"bundle": bundle},
            ):
                profile = profile_phase3_pilot(root)

        self.assertEqual(
            profile["evidence_gaps"],
            ["official_evaluator_terminal_evidence_missing"],
        )
        self.assertIn(
            "official_evaluator_terminal_evidence_missing",
            render_phase3_cost_profile(profile),
        )

    def test_evaluator_rejection_cost_is_invalid_not_infrastructure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=False)
            compatibility = {
                "schema_version": "phase3_patch_compatibility.v1",
                "status": "evaluator_patch_conflict",
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "candidate_patch_sha256": "c" * 64,
                "test_patch_sha256": "d" * 64,
                "conflict_file": "tests/test_source.py",
                "conflict_line": 1,
                "diagnostic_sha256": "e" * 64,
            }
            (
                run_dir / "results" / "official-patch-compatibility.json"
            ).write_text(json.dumps(compatibility), encoding="ascii")
            compatibility_sha256 = hashlib.sha256(
                json.dumps(
                    compatibility,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
            ).hexdigest()
            (run_dir / "results" / "official-evaluator-rejection.json").write_text(
                json.dumps(
                    {
                        "schema_version": "phase3_official_evaluator_rejection.v1",
                        "status": "rejected",
                        "failure_class": "evaluator_patch_conflict",
                        "compatibility_sha256": compatibility_sha256,
                        "wall_time_seconds": 0.5,
                    }
                ),
                encoding="ascii",
            )

            with patch(
                "agentteam_runtime.phase3_cost_profile.load_experiment_result_bundle",
                return_value={"bundle": self._bundle(run_dir)},
            ):
                profile = profile_phase3_pilot(root)

        run = profile["runs"][0]
        self.assertEqual(run["cost_outcome"], "invalid_execution")
        self.assertEqual(run["wall_time_seconds"]["official_evaluator"], 0.5)
        self.assertIn("invalid_execution", profile["by_outcome"])
        self.assertNotIn("infrastructure_waste", profile["by_outcome"])

    def test_rejects_sealed_usage_that_differs_from_invocation_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=False)
            bundle = self._bundle(run_dir)
            bundle["usage_totals"]["total_tokens"] += 1

            with patch(
                "agentteam_runtime.phase3_cost_profile.load_experiment_result_bundle",
                return_value={"bundle": bundle},
            ):
                with self.assertRaisesRegex(
                    Phase3CostProfileError,
                    "differs from sealed usage",
                ):
                    profile_phase3_pilot(root)

    def test_profiles_unsealed_invocation_as_infrastructure_waste(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=False)
            (run_dir / "results" / "terminal" / "result.json").unlink()
            (run_dir / "binding.json").write_text(
                json.dumps(
                    {
                        "experiment_run_id": run_dir.name,
                        "mode": "agentteam_direct",
                        "repetition_index": 0,
                    }
                ),
                encoding="ascii",
            )

            profile = profile_phase3_pilot(root)

        self.assertEqual(profile["run_count"], 1)
        self.assertEqual(
            profile["by_outcome"]["infrastructure_waste"][
                "reported_token_totals"
            ]["total_tokens"],
            110,
        )
        self.assertIn("sealed_result_missing", profile["evidence_gaps"])
        self.assertEqual(profile["runs"][0]["terminal_status"], "unsealed")

    def test_profiles_unsealed_raw_terminal_and_reports_prelaunch_allocations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=False)
            (run_dir / "results" / "terminal" / "result.json").unlink()
            (run_dir / "binding.json").write_text(
                json.dumps(
                    {
                        "experiment_run_id": run_dir.name,
                        "mode": "agentteam_direct",
                        "repetition_index": 0,
                    }
                ),
                encoding="ascii",
            )
            reference = (
                run_dir
                / "authority"
                / "experiment_authority"
                / "model-invocation-set.invocation-set.json"
            )
            reference.unlink()
            valid = (
                run_dir
                / "authority"
                / "experiment_lifecycles"
                / "worker"
                / "model_invocations"
                / "INV-fixture"
            )
            (valid / "started.json").write_text("{}", encoding="ascii")
            (valid.parent / "INV-prelaunch-only").mkdir()

            profile = profile_phase3_pilot(root)

        self.assertEqual(profile["run_count"], 1)
        self.assertEqual(profile["reported_token_totals"]["total_tokens"], 110)
        self.assertIn(
            "unsealed_invocation_set_reference_missing",
            profile["evidence_gaps"],
        )
        self.assertIn(
            "unstarted_invocation_allocations_present:1",
            profile["evidence_gaps"],
        )

    def test_rejects_unsealed_run_with_incomplete_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = self._write_run(root, evaluator_failure=False)
            (run_dir / "results" / "terminal" / "result.json").unlink()
            (run_dir / "binding.json").write_text(
                json.dumps(
                    {
                        "experiment_run_id": run_dir.name,
                        "mode": "agentteam_direct",
                        "repetition_index": 0,
                    }
                ),
                encoding="ascii",
            )
            terminal_path = (
                run_dir
                / "authority"
                / "experiment_lifecycles"
                / "worker"
                / "model_invocations"
                / "INV-fixture"
                / "terminal.json"
            )
            terminal = json.loads(terminal_path.read_text(encoding="ascii"))
            terminal["usage_status"] = "unavailable"
            terminal_path.write_text(json.dumps(terminal), encoding="ascii")

            with self.assertRaisesRegex(
                Phase3CostProfileError,
                "requires complete invocation terminal usage",
            ):
                profile_phase3_pilot(root)

    @staticmethod
    def _write_run(root, *, evaluator_failure):
        instance_id = "fixture-instance"
        run_id = "experiment-run-fixture"
        run_dir = root / "mode-runs" / instance_id / "runs" / run_id
        result_dir = run_dir / "results" / "terminal"
        authority = run_dir / "authority" / "experiment_authority"
        lifecycle = run_dir / "authority" / "experiment_lifecycles" / "worker"
        invocation_dir = lifecycle / "model_invocations" / "INV-fixture"
        for path in (result_dir, authority, invocation_dir, root / "state"):
            path.mkdir(parents=True, exist_ok=True)
        terminal = {
            "invocation_id": "INV-fixture",
            "usage_stage": "implementation_worker",
            "role": "implementation_worker",
            "terminal_status": "completed",
            "usage_status": "reported",
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 10,
            "reasoning_tokens": 3,
            "total_tokens": 110,
            "wall_time_seconds": 7.0,
        }
        (invocation_dir / "terminal.json").write_text(
            json.dumps(terminal), encoding="ascii"
        )
        reference = {
            "schema_version": "experiment_model_invocation_manifest.v1",
            "run_id": run_id,
            "invocation_sets": [
                {
                    "invocation_ids": ["INV-fixture"],
                    "lifecycle_authority_root": str(lifecycle),
                }
            ],
        }
        reference_path = authority / "model-invocation-set.invocation-set.json"
        reference_path.write_text(
            json.dumps(reference, sort_keys=True, separators=(",", ":")),
            encoding="ascii",
        )
        entry_id = f"{instance_id}--r0--agentteam_direct"
        (root / "state" / "pilot-state.json").write_text(
            json.dumps(
                {
                    "schedule": [
                        {
                            "entry_id": entry_id,
                            "instance_id": instance_id,
                            "mode": "agentteam_direct",
                            "repetition_index": 0,
                        }
                    ],
                    "terminal_results": {
                        entry_id: {
                            "failure_class": (
                                "evaluator_failure" if evaluator_failure else None
                            )
                        }
                    },
                }
            ),
            encoding="ascii",
        )
        (result_dir / "result.json").write_text("{}", encoding="ascii")
        if evaluator_failure:
            (run_dir / "results" / "official-evaluator-failure.json").write_text(
                json.dumps({"wall_time_seconds": 30.0}),
                encoding="ascii",
            )
        return run_dir

    @staticmethod
    def _bundle(run_dir):
        reference_path = (
            run_dir
            / "authority"
            / "experiment_authority"
            / "model-invocation-set.invocation-set.json"
        )
        return {
            "experiment_run_id": run_dir.name,
            "mode": "agentteam_direct",
            "repetition_index": 0,
            "terminal_status": "completed",
            "usage_totals": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_tokens": 3,
                "total_tokens": 110,
            },
            "usage_coverage": {
                "status": "complete",
                "covered_invocations": 1,
                "total_invocations": 1,
            },
            "budget_result": {"elapsed_wall_time_seconds": 12.0},
            "result_evidence": {
                "invocation_set_reference_sha256": hashlib.sha256(
                    reference_path.read_bytes()
                ).hexdigest()
            },
        }


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.phase3_calibration_runner import (
    Phase3SingleInstanceCalibrationRunner,
    build_phase3_single_instance_calibration_contract,
)
from agentteam_runtime.phase3_pilot_runner import Phase3PilotRunnerError
from test_phase3_pilot import _build, _live_authorization


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, entry, attempt_index):
        self.calls.append((copy.deepcopy(entry), attempt_index))
        return {
            "entry_id": entry["entry_id"],
            "terminal_status": "completed",
            "failure_class": None,
            "usage": {
                "input_tokens": 80,
                "cached_input_tokens": 10,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "total_tokens": 100,
                "coverage_percent": 100,
            },
            "wall_time_seconds": 1.0,
            "official_score": {"status": "completed", "resolved": True},
        }


class Phase3CalibrationRunnerTests(unittest.TestCase):
    def _authorities(self):
        selection, preregistrations, contract = _build()
        instance_id = selection["ordered_instance_ids"][0]
        selection = copy.deepcopy(selection)
        selection["ordered_instance_ids"] = [instance_id]
        selection["ordered_instances"] = [selection["ordered_instances"][0]]
        selection["stratum_quotas"] = {
            selection["ordered_instances"][0]["complexity_stratum"]: 1
        }
        selection["eligible_counts_by_stratum"] = dict(selection["stratum_quotas"])
        from agentteam_runtime.benchmark_adapter import selection_digest

        selection["selection_sha256"] = selection_digest(selection)
        preregistrations = {instance_id: preregistrations[instance_id]}
        preregistration = copy.deepcopy(preregistrations[instance_id])
        preregistration["authorization"]["selection"] = {
            "selection_sha256": selection["selection_sha256"],
            "ordered_instance_ids": [instance_id],
        }
        from agentteam_runtime.experiment_contract import canonical_json_sha256
        from agentteam_runtime.phase3_pilot import build_phase3_pilot_contract

        preregistration["authorization_sha256"] = canonical_json_sha256(
            preregistration["authorization"]
        )
        preregistrations = {instance_id: preregistration}
        body = contract["contract"]
        contract = build_phase3_pilot_contract(
            readiness_binding=body["readiness_binding"],
            dataset_binding=body["dataset_binding"],
            selection=selection,
            preregistrations_by_instance=preregistrations,
            retry_policy=body["retry_policy"],
            abort_conditions=body["abort_conditions"],
        )
        return selection, preregistrations, contract

    def test_runs_each_mode_once_and_seals_completion(self):
        selection, preregistrations, contract = self._authorities()
        calibration = build_phase3_single_instance_calibration_contract(contract)
        executor = _Executor()
        with tempfile.TemporaryDirectory() as temporary:
            runner = Phase3SingleInstanceCalibrationRunner(
                Path(temporary),
                pilot_id="single-instance-calibration",
                pilot_contract=contract,
                live_authorization=_live_authorization(contract),
                selection=selection,
                preregistrations_by_instance=preregistrations,
                expected_epoch_number=1,
                expected_epoch_sha256="6" * 64,
                executor=executor,
                calibration_contract=calibration,
            )
            state = runner.run()
            resumed = runner.run()

        self.assertEqual(state["status"], "completed")
        self.assertEqual(resumed, state)
        self.assertEqual(len(executor.calls), 3)
        self.assertEqual(
            [call[0]["mode"] for call in executor.calls],
            calibration["mode_order"],
        )
        self.assertEqual({call[0]["repetition_index"] for call in executor.calls}, {0})

    def test_rejects_multi_instance_pilot(self):
        selection, preregistrations, contract = _build()
        self.assertGreater(len(selection["ordered_instance_ids"]), 1)
        with self.assertRaisesRegex(
            Phase3PilotRunnerError,
            "exactly one selected instance",
        ):
            build_phase3_single_instance_calibration_contract(contract)


if __name__ == "__main__":
    unittest.main()

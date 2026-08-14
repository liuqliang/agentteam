"""Bounded one-instance calibration on the Phase 3 production executor."""

from __future__ import annotations

import copy

from .experiment_contract import EXPERIMENT_MODES, canonical_json_sha256
from .phase3_pilot import validate_phase3_pilot_contract
from .phase3_pilot_runner import Phase3PilotRunner, Phase3PilotRunnerError


CALIBRATION_CONTRACT_VERSION = "phase3_single_instance_calibration.v1"
BOUNDED_CALIBRATION_CONTRACT_VERSION = "phase3_single_instance_calibration.v2"


def build_phase3_single_instance_calibration_contract(
    pilot_contract,
    *,
    maximum_total_tokens=None,
    maximum_wall_time_seconds=None,
):
    """Bind a pilot authority to exactly one repetition of all three modes."""

    pilot = validate_phase3_pilot_contract(pilot_contract)
    body = pilot["contract"]
    instance_ids = body["selection"]["ordered_instance_ids"]
    if len(instance_ids) != 1:
        raise Phase3PilotRunnerError(
            "single-instance calibration requires exactly one selected instance"
        )
    contract = {
        "schema_version": (
            BOUNDED_CALIBRATION_CONTRACT_VERSION
            if maximum_total_tokens is not None
            or maximum_wall_time_seconds is not None
            else CALIBRATION_CONTRACT_VERSION
        ),
        "decision_id": "DEC-P3B-single-instance-calibration",
        "pilot_contract_sha256": pilot["contract_sha256"],
        "instance_id": instance_ids[0],
        "modes": list(EXPERIMENT_MODES),
        "mode_order": list(body["mode_order"][0]),
        "repetitions": 1,
        "maximum_mode_executions": len(EXPERIMENT_MODES),
        "continuation": "forbidden",
    }
    if contract["schema_version"] == BOUNDED_CALIBRATION_CONTRACT_VERSION:
        contract["maximum_total_tokens"] = maximum_total_tokens
        contract["maximum_wall_time_seconds"] = maximum_wall_time_seconds
    contract["calibration_contract_sha256"] = canonical_json_sha256(contract)
    return validate_phase3_single_instance_calibration_contract(
        contract,
        pilot_contract=pilot,
    )


def validate_phase3_single_instance_calibration_contract(
    calibration_contract,
    *,
    pilot_contract,
):
    """Validate the narrow calibration authority against its pilot source."""

    pilot = validate_phase3_pilot_contract(pilot_contract)
    if not isinstance(calibration_contract, dict):
        raise Phase3PilotRunnerError("calibration contract must be an object")
    value = copy.deepcopy(calibration_contract)
    expected_fields = {
        "schema_version",
        "decision_id",
        "pilot_contract_sha256",
        "instance_id",
        "modes",
        "mode_order",
        "repetitions",
        "maximum_mode_executions",
        "continuation",
        "calibration_contract_sha256",
    }
    version = value.get("schema_version")
    if version == BOUNDED_CALIBRATION_CONTRACT_VERSION:
        expected_fields.update(
            {"maximum_total_tokens", "maximum_wall_time_seconds"}
        )
    if set(value) != expected_fields:
        raise Phase3PilotRunnerError("calibration contract fields are invalid")
    digest_body = dict(value)
    digest = digest_body.pop("calibration_contract_sha256")
    body = pilot["contract"]
    instance_ids = body["selection"]["ordered_instance_ids"]
    if (
        version
        not in {
            CALIBRATION_CONTRACT_VERSION,
            BOUNDED_CALIBRATION_CONTRACT_VERSION,
        }
        or value["decision_id"] != "DEC-P3B-single-instance-calibration"
        or value["pilot_contract_sha256"] != pilot["contract_sha256"]
        or len(instance_ids) != 1
        or value["instance_id"] != instance_ids[0]
        or value["modes"] != list(EXPERIMENT_MODES)
        or value["mode_order"] != list(body["mode_order"][0])
        or set(value["mode_order"]) != set(EXPERIMENT_MODES)
        or value["repetitions"] != 1
        or value["maximum_mode_executions"] != len(EXPERIMENT_MODES)
        or value["continuation"] != "forbidden"
        or digest != canonical_json_sha256(digest_body)
    ):
        raise Phase3PilotRunnerError("calibration contract is invalid")
    if version == BOUNDED_CALIBRATION_CONTRACT_VERSION:
        pilot_ceiling = body["aggregate_budget_ceiling"]
        maximum_total_tokens = value["maximum_total_tokens"]
        maximum_wall_time_seconds = value["maximum_wall_time_seconds"]
        if (
            not isinstance(maximum_total_tokens, int)
            or isinstance(maximum_total_tokens, bool)
            or maximum_total_tokens < 1
            or maximum_total_tokens
            > pilot_ceiling["maximum_total_tokens"]
            or not isinstance(maximum_wall_time_seconds, (int, float))
            or isinstance(maximum_wall_time_seconds, bool)
            or maximum_wall_time_seconds <= 0
            or maximum_wall_time_seconds
            > pilot_ceiling["maximum_wall_time_seconds"]
        ):
            raise Phase3PilotRunnerError(
                "bounded calibration budget is invalid"
            )
    return value


class Phase3SingleInstanceCalibrationRunner(Phase3PilotRunner):
    """Run the production bridge once per mode and then seal completion."""

    def __init__(self, *args, calibration_contract, **kwargs):
        self.calibration_contract = validate_phase3_single_instance_calibration_contract(
            calibration_contract,
            pilot_contract=kwargs["pilot_contract"],
        )
        super().__init__(*args, **kwargs)

    def _expected_manifest(self):
        manifest = super()._expected_manifest()
        manifest["calibration"] = {
            "contract_sha256": self.calibration_contract[
                "calibration_contract_sha256"
            ],
            "repetitions": 1,
            "maximum_mode_executions": len(EXPERIMENT_MODES),
            "continuation": "forbidden",
        }
        if self.calibration_contract["schema_version"] == (
            BOUNDED_CALIBRATION_CONTRACT_VERSION
        ):
            manifest["calibration"]["maximum_total_tokens"] = (
                self.calibration_contract["maximum_total_tokens"]
            )
            manifest["calibration"]["maximum_wall_time_seconds"] = (
                self.calibration_contract["maximum_wall_time_seconds"]
            )
        return manifest

    def _validate_authorities(self):
        super()._validate_authorities()
        if self.calibration_contract["schema_version"] == (
            BOUNDED_CALIBRATION_CONTRACT_VERSION
        ):
            self.permit = copy.deepcopy(self.permit)
            self.permit["maximum_total_tokens"] = self.calibration_contract[
                "maximum_total_tokens"
            ]
            self.permit["maximum_wall_time_seconds"] = (
                self.calibration_contract["maximum_wall_time_seconds"]
            )

    def _initial_schedule(self, manifest):
        instance_id = self.calibration_contract["instance_id"]
        return [
            {
                "entry_id": f"{instance_id}--r0--{mode}",
                "instance_id": instance_id,
                "mode": mode,
                "repetition_index": 0,
            }
            for mode in self.calibration_contract["mode_order"]
        ]

    def _third_repetition_schedule(self, state):
        return []

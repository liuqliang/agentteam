"""Provider-free Phase 3B pilot contract and live-launch admission.

Phase 3A proves that one preregistration is internally fair.  A real pilot has
multiple repository tasks, so Phase 3B binds one preregistration per instance
and then seals their ordered aggregate.  The pilot contract never authorizes a
provider call; a separate epoch-bound operator artifact is required at launch.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator, FormatChecker

from .benchmark_adapter import validate_benchmark_instance_selection
from .benchmark_preregistration import validate_benchmark_preregistration
from .experiment_contract import (
    EXPERIMENT_MODES,
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    schema_path,
)


PILOT_CONTRACT_SCHEMA_VERSION = "phase3_pilot_contract.v1"
LIVE_AUTHORIZATION_SCHEMA_VERSION = "phase3_live_authorization.v1"
PILOT_GATE_ID = "P3-LIVE"


class Phase3PilotError(ExperimentContractError):
    """Raised when a Phase 3B pilot authority is incomplete or inconsistent."""


def build_phase3_pilot_contract(
    *,
    readiness_binding,
    dataset_binding,
    selection,
    preregistrations_by_instance,
    retry_policy,
    abort_conditions,
):
    """Build a provider-free aggregate over per-instance preregistrations."""

    selection = _snapshot(selection, "selection")
    validate_benchmark_instance_selection(selection)
    preregistrations = _snapshot(
        preregistrations_by_instance,
        "preregistrations_by_instance",
    )
    ordered_ids = selection["ordered_instance_ids"]
    if set(preregistrations) != set(ordered_ids):
        raise Phase3PilotError(
            "per-instance preregistrations must cover the selection exactly"
        )

    instance_bindings = []
    common_execution_profile = None
    common_mode_order = None
    maximum_total_tokens = 0
    maximum_wall_time_seconds = 0.0
    for instance_id in ordered_ids:
        preregistration = preregistrations[instance_id]
        validate_benchmark_preregistration(preregistration)
        authorization = preregistration["authorization"]
        if authorization["selection"]["selection_sha256"] != selection[
            "selection_sha256"
        ]:
            raise Phase3PilotError(
                f"instance {instance_id} does not bind the selected manifest"
            )
        if authorization["selection"]["ordered_instance_ids"] != [instance_id]:
            raise Phase3PilotError(
                "each Phase 3B preregistration must bind exactly its own instance"
            )

        visible = authorization["equal_input_bindings"]["shared_visible_inputs"]
        execution_profile = {
            "runtime": visible["runtime"],
            "model": visible["model"],
            "execution": visible["execution"],
        }
        mode_order = authorization["repetition_policy"]["mode_order"]
        if common_execution_profile is None:
            common_execution_profile = execution_profile
            common_mode_order = mode_order
        elif canonical_json_bytes(execution_profile) != canonical_json_bytes(
            common_execution_profile
        ):
            raise Phase3PilotError(
                "all instances must use the same runtime, model, and execution profile"
            )
        elif mode_order != common_mode_order:
            raise Phase3PilotError(
                "all instances must use the same counterbalanced mode order"
            )

        repetitions = authorization["repetition_policy"]["max_repetitions"]
        per_mode_budgets = authorization["budgets"]
        instance_max_tokens = repetitions * sum(
            per_mode_budgets[mode]["max_total_tokens"]
            for mode in EXPERIMENT_MODES
        )
        instance_max_wall = repetitions * sum(
            per_mode_budgets[mode]["max_wall_time_seconds"]
            for mode in EXPERIMENT_MODES
        )
        maximum_total_tokens += instance_max_tokens
        maximum_wall_time_seconds += instance_max_wall
        instance_bindings.append(
            {
                "instance_id": instance_id,
                "preregistration_authorization_sha256": preregistration[
                    "authorization_sha256"
                ],
                "maximum_total_tokens": instance_max_tokens,
                "maximum_wall_time_seconds": instance_max_wall,
            }
        )

    contract_body = {
        "decision_id": "DEC-P3B-live-pilot-execution",
        "provider_calls_authorized": False,
        "readiness_binding": _snapshot(readiness_binding, "readiness_binding"),
        "dataset_binding": _snapshot(dataset_binding, "dataset_binding"),
        "selection": {
            "selection_sha256": selection["selection_sha256"],
            "ordered_instance_ids": ordered_ids,
        },
        "instance_bindings": instance_bindings,
        "modes": list(EXPERIMENT_MODES),
        "execution_profile": common_execution_profile,
        "mode_order": common_mode_order,
        "retry_policy": _snapshot(retry_policy, "retry_policy"),
        "abort_conditions": _snapshot(abort_conditions, "abort_conditions"),
        "aggregate_budget_ceiling": {
            "maximum_total_tokens": maximum_total_tokens,
            "maximum_wall_time_seconds": maximum_wall_time_seconds,
            "max_inflight_model_invocations": 1,
        },
        "live_authorization": {
            "required": True,
            "gate_id": PILOT_GATE_ID,
            "status": "not_authorized",
        },
    }
    contract = {
        "schema_version": PILOT_CONTRACT_SCHEMA_VERSION,
        "contract": contract_body,
        "contract_sha256": canonical_json_sha256(contract_body),
    }
    validate_phase3_pilot_contract(contract)
    return contract


def validate_phase3_pilot_contract(
    contract,
    *,
    selection=None,
    preregistrations_by_instance=None,
):
    """Validate a sealed pilot contract and optional source authorities."""

    value = _snapshot(contract, "phase3 pilot contract")
    _validate_schema(value, "phase3_pilot_contract.schema.json", "pilot contract")
    body = value["contract"]
    if value["contract_sha256"] != canonical_json_sha256(body):
        raise Phase3PilotError("contract_sha256 does not bind canonical content")

    ordered_ids = body["selection"]["ordered_instance_ids"]
    binding_ids = [item["instance_id"] for item in body["instance_bindings"]]
    if binding_ids != ordered_ids or len(binding_ids) != len(set(binding_ids)):
        raise Phase3PilotError(
            "instance bindings must match the ordered selection exactly"
        )
    expected_tokens = sum(
        item["maximum_total_tokens"] for item in body["instance_bindings"]
    )
    expected_wall = sum(
        item["maximum_wall_time_seconds"] for item in body["instance_bindings"]
    )
    ceiling = body["aggregate_budget_ceiling"]
    if ceiling["maximum_total_tokens"] != expected_tokens:
        raise Phase3PilotError("aggregate token ceiling is inconsistent")
    if ceiling["maximum_wall_time_seconds"] != expected_wall:
        raise Phase3PilotError("aggregate wall-time ceiling is inconsistent")

    if selection is not None:
        selection = validate_benchmark_instance_selection(selection)
        if body["selection"] != {
            "selection_sha256": selection["selection_sha256"],
            "ordered_instance_ids": selection["ordered_instance_ids"],
        }:
            raise Phase3PilotError("pilot contract does not match selection authority")
    if preregistrations_by_instance is not None:
        if selection is None:
            raise Phase3PilotError(
                "selection authority is required with source preregistrations"
            )
        preregistrations = _snapshot(
            preregistrations_by_instance,
            "preregistrations_by_instance",
        )
        if set(preregistrations) != set(ordered_ids):
            raise Phase3PilotError(
                "source preregistrations do not cover the pilot instances"
            )
        expected_bindings = []
        expected_profile = None
        expected_mode_order = None
        for instance_id in ordered_ids:
            preregistration = preregistrations[instance_id]
            validate_benchmark_preregistration(preregistration)
            preregistration_authority = preregistration["authorization"]
            if preregistration_authority["selection"] != {
                "selection_sha256": selection["selection_sha256"],
                "ordered_instance_ids": [instance_id],
            }:
                raise Phase3PilotError(
                    "source preregistration does not bind its selected instance"
                )
            visible = preregistration_authority["equal_input_bindings"][
                "shared_visible_inputs"
            ]
            profile = {
                "runtime": visible["runtime"],
                "model": visible["model"],
                "execution": visible["execution"],
            }
            mode_order = preregistration_authority["repetition_policy"][
                "mode_order"
            ]
            if expected_profile is None:
                expected_profile = profile
                expected_mode_order = mode_order
            elif canonical_json_bytes(profile) != canonical_json_bytes(
                expected_profile
            ) or mode_order != expected_mode_order:
                raise Phase3PilotError(
                    "source preregistrations do not share one execution profile"
                )
            repetitions = preregistration_authority["repetition_policy"][
                "max_repetitions"
            ]
            budgets = preregistration_authority["budgets"]
            expected_bindings.append(
                {
                    "instance_id": instance_id,
                    "preregistration_authorization_sha256": preregistration[
                        "authorization_sha256"
                    ],
                    "maximum_total_tokens": repetitions
                    * sum(
                        budgets[mode]["max_total_tokens"]
                        for mode in EXPERIMENT_MODES
                    ),
                    "maximum_wall_time_seconds": repetitions
                    * sum(
                        budgets[mode]["max_wall_time_seconds"]
                        for mode in EXPERIMENT_MODES
                    ),
                }
            )
        if body["instance_bindings"] != expected_bindings:
            raise Phase3PilotError(
                "pilot contract does not match per-instance preregistrations"
            )
        if body["execution_profile"] != expected_profile:
            raise Phase3PilotError(
                "pilot execution profile does not match preregistrations"
            )
        if body["mode_order"] != expected_mode_order:
            raise Phase3PilotError("pilot mode order does not match preregistrations")
    return value


def validate_phase3_live_authorization(
    authorization,
    *,
    pilot_contract,
    selection,
    preregistrations_by_instance,
    expected_epoch_number,
    expected_epoch_sha256,
):
    """Validate exact operator authority for one P3-LIVE gate epoch."""

    pilot = validate_phase3_pilot_contract(
        pilot_contract,
        selection=selection,
        preregistrations_by_instance=preregistrations_by_instance,
    )
    value = _snapshot(authorization, "phase3 live authorization")
    _validate_schema(
        value,
        "phase3_live_authorization.schema.json",
        "live authorization",
    )
    if value["decision"] != "approved":
        raise Phase3PilotError("Phase 3 live pilot is not approved")
    if value["epoch_number"] != expected_epoch_number:
        raise Phase3PilotError("live authorization epoch number is stale")
    if value["epoch_sha256"] != expected_epoch_sha256:
        raise Phase3PilotError("live authorization epoch digest is stale")

    body = pilot["contract"]
    expected = {
        "pilot_contract_sha256": pilot["contract_sha256"],
        "readiness_evidence_sha256": body["readiness_binding"][
            "evidence_sha256"
        ],
        "selection_sha256": body["selection"]["selection_sha256"],
        "agentteam_release_commit": body["execution_profile"]["runtime"][
            "agentteam_release_commit"
        ],
        "model": body["execution_profile"]["model"]["model"],
        "reasoning_profile": body["execution_profile"]["model"][
            "reasoning_profile"
        ],
        "max_total_tokens": body["aggregate_budget_ceiling"][
            "maximum_total_tokens"
        ],
        "max_wall_time_seconds": body["aggregate_budget_ceiling"][
            "maximum_wall_time_seconds"
        ],
        "max_inflight_model_invocations": body["aggregate_budget_ceiling"][
            "max_inflight_model_invocations"
        ],
        "modes": body["modes"],
    }
    for field, expected_value in expected.items():
        if value[field] != expected_value:
            raise Phase3PilotError(
                f"live authorization {field} does not match the pilot contract"
            )
    return value


def admit_phase3_live_launch(
    pilot_contract,
    authorization,
    *,
    selection,
    preregistrations_by_instance,
    expected_epoch_number,
    expected_epoch_sha256,
):
    """Return a bounded permit after validation; this function never launches."""

    pilot = validate_phase3_pilot_contract(
        pilot_contract,
        selection=selection,
        preregistrations_by_instance=preregistrations_by_instance,
    )
    approved = validate_phase3_live_authorization(
        authorization,
        pilot_contract=pilot,
        selection=selection,
        preregistrations_by_instance=preregistrations_by_instance,
        expected_epoch_number=expected_epoch_number,
        expected_epoch_sha256=expected_epoch_sha256,
    )
    return {
        "gate_id": PILOT_GATE_ID,
        "epoch_number": approved["epoch_number"],
        "pilot_contract_sha256": pilot["contract_sha256"],
        "authorization_sha256": canonical_json_sha256(approved),
        "maximum_total_tokens": approved["max_total_tokens"],
        "maximum_wall_time_seconds": approved["max_wall_time_seconds"],
        "max_inflight_model_invocations": approved[
            "max_inflight_model_invocations"
        ],
    }


def _validate_schema(value, filename, label):
    path = schema_path(filename)
    if path.is_symlink() or not path.is_file():
        raise Phase3PilotError(f"{label} schema is missing or unsafe: {path}")
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase3PilotError(f"{label} schema is unreadable") from exc
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise Phase3PilotError(
            f"{label} schema validation failed at {location}: {first.message}"
        )


def _snapshot(value, label):
    try:
        result = json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Phase3PilotError(f"{label} must be JSON-compatible") from exc
    if not isinstance(result, dict):
        raise Phase3PilotError(f"{label} must be a JSON object")
    return result

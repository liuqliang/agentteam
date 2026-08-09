"""Deterministic gate controllers and relation validators."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_readiness import _check_pilot_authorization_with_record
from .experiment_calibration import (
    ExperimentCalibrationError,
    _load_run_record,
    _validate_equal_contract,
    _validate_mode_family,
    _validate_unique_results,
    _validate_usage_coverage,
    load_deterministic_calibration_report,
    load_deterministic_calibration_report_bytes,
    run_deterministic_calibration_from_manifest,
)
from .experiment_contract import (
    allocate_experiment_run,
    build_experiment_run_manifest,
    canonical_json_sha256,
    validate_experiment_protocol,
    validate_experiment_run_manifest,
)
from .experiment_modes import (
    AgentTeamDirectModeAdapter,
    AgentTeamFullModeAdapter,
    ExperimentCommonFinalizer,
    SingleCodexModeAdapter,
    execute_bound_experiment_mode,
)
from .experiment_results import (
    ExperimentResultError,
    load_experiment_result_bundle,
)
from .phase3_readiness import (
    Phase3ReadinessError,
    validate_phase3_readiness_receipt,
)

_MODES = (
    "single_codex",
    "agentteam_direct",
    "agentteam_full",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_RUNTIME_RELEASE_FIELDS = {
    "release_id",
    "release_root",
    "runtime_root",
    "release_manifest_sha256",
    "source_commit",
    "git_object_format",
}
READINESS_CAPABILITY_REGISTRY_VERSION = (
    "phase2_readiness_capability_registry.v1"
)
_CAPABILITY_TEST_ARTIFACT_PATHS = {
    "tests.test_phase1_usage_end_to_end": (
        "experiments/native_agentteam_runtime/m0_runtime/tests/"
        "test_phase1_usage_end_to_end.py"
    ),
    "tests.test_experiment_harness": (
        "experiments/native_agentteam_runtime/m0_runtime/tests/"
        "test_experiment_harness.py"
    ),
}
_CAPABILITY_IDS = frozenset(
    (
        "invocation_level_real_usage",
        "three_mode_experiment_harness",
        "immutable_experiment_manifest",
        "clean_reset_and_blind_gold_isolation",
        "actual_budget_enforcement",
        "operator_action_ledger",
        "machine_readable_result_bundle",
    )
)
_CAPABILITY_TEST_IDS = {
    "invocation_level_real_usage": (
        "tests.test_phase1_usage_end_to_end."
        "Phase1UsageEndToEndTests."
        "test_positive_fixture_has_complete_exact_replay_stable_accounting",
    ),
    "three_mode_experiment_harness": (
        "tests.test_experiment_harness."
        "ExperimentContractSchemaTests."
        "test_protocol_requires_complete_three_mode_equal_input_contract",
        "tests.test_experiment_harness."
        "ExperimentModeAdapterTests."
        "test_counterbalanced_mode_order_is_enforced_and_immutable",
        "tests.test_experiment_harness."
        "ExperimentCalibrationTests."
        "test_deterministic_l1_l2_calibration_rebuilds_all_evidence",
    ),
    "immutable_experiment_manifest": (
        "tests.test_experiment_harness."
        "ExperimentContractSchemaTests."
        "test_run_manifest_binds_mode_repetition_request_and_protocol",
        "tests.test_experiment_harness."
        "ExperimentAllocationTests."
        "test_tampered_published_protocol_fails_closed_without_provider",
        "tests.test_experiment_harness."
        "ExperimentLeaseAndResumeTests."
        "test_resume_accepts_exact_binding_and_rejects_all_identity_drift",
    ),
    "clean_reset_and_blind_gold_isolation": (
        "tests.test_experiment_harness."
        "ExperimentWorkspaceTests."
        "test_allocates_independent_exact_commit_snapshot_and_attestation",
        "tests.test_experiment_harness."
        "ExperimentSandboxTests."
        "test_provider_environment_rejects_canary_content_and_digest",
    ),
    "actual_budget_enforcement": (
        "tests.test_experiment_harness."
        "ExperimentBudgetTests."
        "test_token_warning_and_exhaustion_include_exact_boundaries",
        "tests.test_experiment_harness."
        "ExperimentBudgetTests."
        "test_one_lane_terminal_completion_exposes_overshoot",
        "tests.test_experiment_harness."
        "ExperimentProviderBudgetBoundaryTests."
        "test_denied_prelaunch_creates_no_runner_start_or_process",
        "tests.test_experiment_harness."
        "ExperimentProviderBudgetBoundaryTests."
        "test_interruption_resume_preserves_original_remaining_budget",
        "tests.test_experiment_harness."
        "TwoPhaseSchedulerExperimentBoundaryTests."
        "test_scheduler_denies_worker_dispatch_after_budget_exhaustion",
        "tests.test_experiment_harness."
        "TwoPhaseSchedulerExperimentBoundaryTests."
        "test_preintegration_exhaustion_preserves_patch_and_baseline",
    ),
    "operator_action_ledger": (
        "tests.test_experiment_harness."
        "ExperimentOperatorActionLedgerTests."
        "test_supported_inputs_map_to_closed_vocabulary_without_raw_content",
        "tests.test_experiment_harness."
        "ExperimentOperatorActionLedgerTests."
        "test_replay_is_idempotent_and_conflicting_response_fails_closed",
        "tests.test_experiment_harness."
        "ExperimentOperatorActionLedgerTests."
        "test_experiment_gateways_account_before_applying_runtime_input",
        "tests.test_experiment_harness."
        "ExperimentOperatorActionLedgerTests."
        "test_experiment_stop_capability_validates_against_real_ledger",
    ),
    "machine_readable_result_bundle": (
        "tests.test_experiment_harness."
        "ExperimentResultBundleTests."
        "test_terminal_bundle_is_atomic_idempotent_and_conflict_safe",
        "tests.test_experiment_harness."
        "ExperimentResultBundleTests."
        "test_recovery_snapshots_are_versioned_and_separate",
        "tests.test_experiment_harness."
        "ExperimentResultBundleTests."
        "test_projection_rebuild_preserves_all_outcomes_and_digests",
        "tests.test_experiment_harness."
        "TwoPhaseSchedulerExperimentBoundaryTests."
        "test_provider_terminal_waits_one_tick_for_worker_outbox",
        "tests.test_experiment_harness."
        "TwoPhaseSchedulerExperimentBoundaryTests."
        "test_terminal_without_worker_outbox_reconciles_on_second_tick",
    ),
}
_READINESS_PATH = (
    "experiments/native_agentteam_runtime/m0_runtime/"
    "agentteam_runtime/data/p0_experiment_readiness.v1.json"
)
_ROADMAP_PATH = (
    "experiments/native_agentteam_runtime/"
    "implementation_artifacts/native_runtime_roadmap.md"
)
_REPORT_PATH = (
    "experiments/native_agentteam_runtime/implementation_artifacts/"
    "reports/phase2-experiment-harness.md"
)


class Phase2GateError(RuntimeError):
    pass


@dataclass(frozen=True)
class GateSpec:
    gate_id: str
    controller_entrypoint: str
    relation_validator: str
    evidence_schema: str
    authorization_required: bool = False
    authorization_schema: str | None = None
    action_required: bool = True


GATE_SPECS = {
    "P2-08": GateSpec(
        "P2-08",
        "phase2_readiness_controller_v1",
        "phase2_readiness_relation_v1",
        "experiments/native_agentteam_runtime/schemas/"
        "phase2_readiness_promotion.schema.json",
    ),
    "P2-09": GateSpec(
        "P2-09",
        "phase2_live_calibration_controller_v1",
        "phase2_live_calibration_relation_v1",
        "experiments/native_agentteam_runtime/schemas/"
        "phase2_calibration.schema.json",
        authorization_required=True,
        authorization_schema=(
            "experiments/native_agentteam_runtime/schemas/"
            "phase2_live_authorization.schema.json"
        ),
    ),
    "P2-10": GateSpec(
        "P2-10",
        "phase2_finalization_controller_v1",
        "phase2_finalization_relation_v1",
        "experiments/native_agentteam_runtime/schemas/"
        "phase2_finalization.schema.json",
    ),
    "P3-READY": GateSpec(
        "P3-READY",
        "phase3_readiness_controller_v1",
        "phase3_readiness_relation_v1",
        "experiments/native_agentteam_runtime/schemas/"
        "phase3_readiness_receipt.schema.json",
        action_required=False,
    ),
}
_CONTROLLER_GATE_DEPENDENCIES = {
    "P2-08": [],
    "P2-09": ["P2-08"],
    "P2-10": ["P2-09"],
}
_CONTROLLER_ACTIONS = {
    "P2-08": "promote_readiness",
    "P2-09": "run_live_calibration",
    "P2-10": "finalize_phase2",
}


def validate_controller_gate_graph(declarations):
    if not isinstance(declarations, list):
        raise Phase2GateError(
            "controller gate declarations must be a list"
        )
    gate_ids = [
        item.get("gate_id") if isinstance(item, dict) else None
        for item in declarations
    ]
    if gate_ids == list(_CONTROLLER_GATE_DEPENDENCIES):
        dependencies = _CONTROLLER_GATE_DEPENDENCIES
    elif gate_ids == ["P3-READY"]:
        dependencies = {"P3-READY": []}
    else:
        raise Phase2GateError(
            "controller-only gate graph must be the Phase 2 chain or "
            "standalone P3-READY"
        )
    for declaration in declarations:
        spec = resolve_gate_spec(declaration)
        if declaration.get("depends_on") != (
            dependencies[spec.gate_id]
        ):
            raise Phase2GateError(
                f"{spec.gate_id} dependency topology is invalid"
            )
    return tuple(GATE_SPECS[gate_id] for gate_id in gate_ids)


def resolve_gate_spec(declaration):
    if not isinstance(declaration, dict):
        raise Phase2GateError("gate declaration must be an object")
    gate_id = declaration.get("gate_id")
    spec = GATE_SPECS.get(gate_id)
    if spec is None:
        raise Phase2GateError("gate is not a registered controller")
    expected = {
        "executor": "deterministic_controller",
        "controller_entrypoint": spec.controller_entrypoint,
        "relation_validator": spec.relation_validator,
        "evidence_schema": spec.evidence_schema,
    }
    mismatches = [
        field
        for field, value in expected.items()
        if declaration.get(field) != value
    ]
    if mismatches:
        raise Phase2GateError(
            "gate controller registry binding mismatch: "
            + ", ".join(mismatches)
        )
    if (
        declaration.get("operator_authorization_required") is True
    ) != spec.authorization_required:
        raise Phase2GateError(
            "gate authorization policy differs from registry"
        )
    if spec.authorization_required and declaration.get(
        "operator_authorization_schema"
    ) != spec.authorization_schema:
        raise Phase2GateError(
            "gate authorization schema differs from registry"
        )
    action_input = declaration.get("controller_action_input")
    if spec.action_required:
        _validate_action_input_structure(action_input, spec.gate_id)
    elif action_input is not None:
        raise Phase2GateError(
            f"{spec.gate_id} does not accept controller action input"
        )
    return spec


def validate_gate_relation(spec, artifact_path, context):
    spec = _coerce_spec(spec)
    artifact_path = _safe_file(artifact_path, "gate evidence")
    artifact = _read_json(artifact_path, "gate evidence")
    repository_root = _repository_root(context)
    schema_path = repository_root / spec.evidence_schema
    _validate_schema(artifact, schema_path, "gate evidence")
    if spec.gate_id == "P2-08":
        details = _validate_readiness_relation(
            artifact,
            repository_root,
            context,
        )
    elif spec.gate_id == "P2-09":
        details = _validate_live_calibration_relation(
            artifact,
            repository_root,
            context,
        )
    elif spec.gate_id == "P2-10":
        details = _validate_finalization_relation(
            artifact,
            repository_root,
            context,
        )
    elif spec.gate_id == "P3-READY":
        details = _validate_phase3_readiness_relation(artifact)
    else:
        raise Phase2GateError(
            f"{spec.gate_id} has no registered relation implementation"
        )
    return {
        "gate_id": spec.gate_id,
        "relation_status": "passed",
        "evidence_sha256": _sha256_file(artifact_path),
        **details,
    }


def _validate_phase3_readiness_relation(artifact):
    try:
        receipt = validate_phase3_readiness_receipt(artifact)
    except Phase3ReadinessError as exc:
        raise Phase2GateError(str(exc)) from exc
    if receipt["status"] != "passed":
        raise Phase2GateError(
            "P3-READY receipt does not report passed readiness"
        )
    usage = receipt["usage_reconciliation"]
    return {
        "decision_id": receipt["decision_id"],
        "ordered_instance_count": len(
            receipt["bindings"]["ordered_instance_ids"]
        ),
        "verification_sha256": receipt["verification"][
            "verification_sha256"
        ],
        "provider_calls": usage["live_provider_calls"],
        "target_mutations": 0,
    }


def run_gate_controller(
    spec,
    artifact_path,
    context,
    *,
    result_path,
    provider_launcher=None,
):
    spec = _coerce_spec(spec)
    launch_record = None
    if spec.authorization_required:
        authorization_path = context.get("authorization_path")
        if not authorization_path or not Path(authorization_path).is_file():
            return {
                "gate_id": spec.gate_id,
                "controller_status": "awaiting_operator_authorization",
                "provider_calls": 0,
                "target_mutations": 0,
            }
        permit = (
            _require_current_live_provider_authorization(
                authorization_path,
                context,
            )
            if provider_launcher is not None
            else require_live_provider_authorization(
                authorization_path,
                context,
            )
        )
        if provider_launcher is not None:
            if spec.gate_id != "P2-09" or not callable(
                provider_launcher
            ):
                raise Phase2GateError(
                    "provider launcher is valid only for P2-09"
                )
            launch_record = provider_launcher(dict(permit))
            if (
                not isinstance(launch_record, dict)
                or not launch_record.get("artifact_path")
            ):
                raise Phase2GateError(
                    "P2-09 provider launcher returned no gate artifact"
                )
            artifact_path = launch_record["artifact_path"]
    elif provider_launcher is not None:
        raise Phase2GateError(
            "provider launcher is valid only for an authorized gate"
        )
    relation = validate_gate_relation(spec, artifact_path, context)
    provider_calls = relation.get("provider_calls", 0)
    target_mutations = relation.get("target_mutations", 0)
    if launch_record is not None:
        if launch_record.get("provider_calls") != provider_calls:
            raise Phase2GateError(
                "P2-09 launcher provider count differs from sealed evidence"
            )
        if launch_record.get("target_mutations", 0) != target_mutations:
            raise Phase2GateError(
                "P2-09 launcher mutation count differs from sealed evidence"
            )
    payload = {
        "schema_version": (
            "phase2_gate_controller_result.v1"
            if spec.gate_id.startswith("P2-")
            else "gate_controller_result.v1"
        ),
        "gate_id": spec.gate_id,
        "controller_entrypoint": spec.controller_entrypoint,
        "relation_validator": spec.relation_validator,
        "controller_status": "passed",
        "gate_epoch": _positive_int(context, "epoch_number"),
        "epoch_sha256": _sha256_value(context, "epoch_sha256"),
        "evidence_sha256": relation["evidence_sha256"],
        "provider_calls": provider_calls,
        "target_mutations": target_mutations,
    }
    publication = _publish_immutable_json(result_path, payload)
    return {
        **payload,
        "result_path": publication["path"],
        "result_sha256": publication["sha256"],
        "created": publication["created"],
        "relation": relation,
    }


def publish_live_authorization(path, authorization, context):
    repository_root = _repository_root(context)
    _validate_schema(
        authorization,
        repository_root
        / "experiments/native_agentteam_runtime/schemas/"
        "phase2_live_authorization.schema.json",
        "Phase 2 live authorization",
    )
    _validate_live_authorization(authorization, context)
    return _publish_immutable_json(path, authorization)


def require_live_provider_authorization(path, context):
    authorization_path = _safe_file(
        path,
        "Phase 2 live authorization",
    )
    authorization = _read_json(
        authorization_path,
        "Phase 2 live authorization",
    )
    repository_root = _repository_root(context)
    _validate_schema(
        authorization,
        repository_root
        / "experiments/native_agentteam_runtime/schemas/"
        "phase2_live_authorization.schema.json",
        "Phase 2 live authorization",
    )
    _validate_live_authorization(authorization, context)
    return {
        "provider_launch_authorized": True,
        "authorization_sha256": _sha256_file(authorization_path),
        "epoch_number": authorization["epoch_number"],
        "epoch_sha256": authorization["epoch_sha256"],
    }


def _require_current_live_provider_authorization(path, context):
    permit = require_live_provider_authorization(path, context)
    if (
        permit["epoch_number"] != _positive_int(context, "epoch_number")
        or permit["epoch_sha256"] != _sha256_value(
            context,
            "epoch_sha256",
        )
    ):
        raise Phase2GateError(
            "live provider authorization is not bound to the current "
            "gate epoch"
        )
    return permit


def execute_readiness_promotion_action(
    action_input,
    context,
    *,
    artifact_path,
    pilot_guard_path,
    protocol_path,
    run_manifest_path,
    pilot_manifest_path,
    candidate_guard_runner=None,
):
    configuration = _action_configuration(
        action_input,
        gate_id="P2-08",
    )
    repository_root = _repository_root(context)
    worktree = Path(
        context.get("integration_worktree") or ""
    ).resolve()
    if not worktree.is_dir():
        raise Phase2GateError(
            "readiness promotion integration worktree is unavailable"
        )
    validated_code = _git_oid(
        repository_root,
        context["integration_head"],
    )
    if _git(worktree, "rev-parse", "HEAD") != validated_code:
        raise Phase2GateError(
            "readiness promotion worktree is not at validated code"
        )
    if _git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ):
        raise Phase2GateError(
            "readiness promotion worktree must be clean"
        )
    evidence = _readiness_action_evidence(
        configuration.get("capability_evidence"),
        repository_root,
        validated_code,
    )
    reasons = configuration.get("readiness_reasons")
    if (
        not isinstance(reasons, dict)
        or set(reasons) != _CAPABILITY_IDS
        or any(
            not isinstance(value, str) or not value.strip()
            for value in reasons.values()
        )
    ):
        raise Phase2GateError(
            "readiness promotion reasons are incomplete"
        )
    promoted_at = _utc_text(
        configuration.get("promoted_at"),
        "readiness promoted_at",
    )
    authority = _action_authority_files(
        configuration.get("authority_artifacts"),
        required=(
            "protocol_template",
            "deterministic_calibration_request",
            "deterministic_calibration",
        ),
        authority_roots=context.get("authority_roots"),
    )
    _validated_deterministic_calibration(
        authority["deterministic_calibration"]["path"],
        expected_runtime_source_commit=validated_code,
        calibration_request_path=(
            context.get("calibration_recompute_request_path")
            or authority[
                "deterministic_calibration_request"
            ]["path"]
        ),
        calibration_bytes=authority[
            "deterministic_calibration"
        ]["bytes"],
        calibration_request_bytes=authority[
            "deterministic_calibration_request"
        ]["bytes"],
        calibration_closure_mounts=context.get(
            "calibration_closure_mounts"
        ),
    )
    readiness_path = worktree / _READINESS_PATH
    readiness_before = _safe_file(
        readiness_path,
        "packaged readiness record",
    ).read_bytes()
    readiness = _json_bytes(
        readiness_before,
        "packaged readiness record",
    )
    capabilities = readiness.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or {
            item.get("capability_id")
            for item in capabilities
            if isinstance(item, dict)
        }
        != _CAPABILITY_IDS
    ):
        raise Phase2GateError(
            "packaged readiness capability inventory is invalid"
        )
    for capability in capabilities:
        capability_id = capability["capability_id"]
        capability.update(
            {
                "status": "passed",
                "reason": reasons[capability_id].strip(),
                "evidence": sorted(
                    {
                        item["artifact_path"]
                        for item in evidence[capability_id]
                    }
                ),
            }
        )
    readiness.update(
        {
            "updated_at": promoted_at,
            "overall_status": "passed",
            "pilot_authorized": True,
            "next_required_phase": "P2 bounded live calibration",
        }
    )
    readiness_after = (
        json.dumps(readiness, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    readiness_path.write_bytes(readiness_after)
    if _git_changed_paths(
        worktree,
        validated_code,
        "HEAD",
    ):
        raise Phase2GateError(
            "readiness worktree changed before promotion commit"
        )
    changed = sorted(
        item
        for item in _git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).splitlines()
        if item
    )
    if len(changed) != 1 or not changed[0].endswith(_READINESS_PATH):
        raise Phase2GateError(
            "readiness promotion changed an unexpected path"
        )
    _git(worktree, "add", "--", _READINESS_PATH)
    commit_env = dict(os.environ)
    commit_env.update(
        {
            "GIT_AUTHOR_DATE": promoted_at,
            "GIT_COMMITTER_DATE": promoted_at,
        }
    )
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=AgentTeam Phase 2 Controller",
            "-c",
            "user.email=agentteam-phase2@localhost",
            "commit",
            "--quiet",
            "-m",
            "promote phase 2 experiment readiness",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=commit_env,
    )
    if completed.returncode != 0:
        raise Phase2GateError(
            completed.stderr.strip()
            or "readiness promotion commit failed"
        )
    promotion_head = _git(worktree, "rev-parse", "HEAD")
    if (
        _git_parents(repository_root, promotion_head)
        != [validated_code]
        or _git_changed_paths(
            repository_root,
            validated_code,
            promotion_head,
        )
        != [_READINESS_PATH]
    ):
        raise Phase2GateError(
            "readiness promotion did not create the exact child commit"
        )
    protocol_template = _json_bytes(
        authority["protocol_template"]["bytes"],
        "Phase 2 protocol template",
    )
    if not isinstance(protocol_template.get("repository"), dict):
        raise Phase2GateError(
            "Phase 2 protocol template repository is invalid"
        )
    protocol = {
        **protocol_template,
        "repository": {
            **protocol_template["repository"],
            "commit": promotion_head,
            "tree": _git(worktree, "rev-parse", "HEAD^{tree}"),
            "git_object_format": _git(
                worktree,
                "rev-parse",
                "--show-object-format",
            ),
        },
    }
    try:
        validate_experiment_protocol(protocol)
    except Exception as exc:
        raise Phase2GateError(
            "generated Phase 2 protocol is invalid"
        ) from exc
    protocol_publication = _publish_immutable_json(
        protocol_path,
        protocol,
    )
    run_manifest = build_experiment_run_manifest(
        protocol,
        mode=configuration["pilot_mode"],
        repetition_index=configuration[
            "pilot_repetition_index"
        ],
        stable_request_key=configuration[
            "pilot_stable_request_key"
        ],
    )
    run_manifest_publication = _publish_immutable_json(
        run_manifest_path,
        run_manifest,
    )
    pilot_manifest = _build_pilot_manifest(
        protocol,
        readiness,
        mode=configuration["pilot_mode"],
    )
    pilot_manifest_publication = _publish_immutable_json(
        pilot_manifest_path,
        pilot_manifest,
    )
    if not callable(candidate_guard_runner):
        raise Phase2GateError(
            "candidate release pilot guard runner is required"
        )
    guarded = candidate_guard_runner(
        promotion_head,
        pilot_manifest_path,
    )
    if not isinstance(guarded, dict):
        raise Phase2GateError(
            "candidate release pilot guard returned no evidence"
        )
    pilot_guard = guarded.get("pilot_guard")
    runtime_release = guarded.get("runtime_release")
    _validate_runtime_release_identity(
        runtime_release,
        source_commit=promotion_head,
        require_files=True,
    )
    if (
        not isinstance(pilot_guard, dict)
        or pilot_guard.get("pilot_authorized") is not True
        or pilot_guard.get("provider_calls", 0) != 0
        or pilot_guard.get("target_mutations", 0) != 0
    ):
        raise Phase2GateError(
            "readiness promotion pilot guard is not approved"
        )
    pilot_guard.update(
        {
            "provider_calls": 0,
            "target_mutations": 0,
        }
    )
    pilot_publication = _publish_immutable_json(
        pilot_guard_path,
        pilot_guard,
    )
    artifact = {
        "schema_version": "phase2_readiness_promotion.v1",
        "controller_validation_status": "passed",
        "validated_code_sha": validated_code,
        "protocol_sha256": canonical_json_sha256(protocol),
        "run_manifest_sha256": canonical_json_sha256(run_manifest),
        "deterministic_calibration_sha256": authority[
            "deterministic_calibration"
        ]["sha256"],
        "readiness_before_sha256": hashlib.sha256(
            readiness_before
        ).hexdigest(),
        "readiness_after_sha256": hashlib.sha256(
            readiness_after
        ).hexdigest(),
        "capability_evidence": evidence,
        "pilot_guard_status": "passed",
        "pilot_manifest_sha256": canonical_json_sha256(
            pilot_manifest
        ),
        "pilot_guard_sha256": pilot_publication["sha256"],
        "pilot_guard_provider_calls": 0,
        "pilot_guard_target_mutations": 0,
        "candidate_release": runtime_release,
        "changed_paths": [_READINESS_PATH],
    }
    artifact_publication = _publish_immutable_json(
        artifact_path,
        artifact,
    )
    return {
        "gate_id": "P2-08",
        "action_status": "completed",
        "validated_code_sha": validated_code,
        "integration_head": promotion_head,
        "artifact_path": artifact_publication["path"],
        "artifact_sha256": artifact_publication["sha256"],
        "pilot_guard_path": pilot_publication["path"],
        "pilot_guard_sha256": pilot_publication["sha256"],
        "provider_calls": 0,
        "target_mutations": 0,
        "relation_context": {
            "protocol_path": protocol_publication["path"],
            "run_manifest_path": run_manifest_publication["path"],
            "deterministic_calibration_path": authority[
                "deterministic_calibration"
            ]["path"],
            "deterministic_calibration_request_path": authority[
                "deterministic_calibration_request"
            ]["path"] if not context.get(
                "calibration_recompute_request_path"
            ) else context["calibration_recompute_request_path"],
            "calibration_closure_mounts": context.get(
                "calibration_closure_mounts"
            ),
            "pilot_manifest_path": pilot_manifest_publication["path"],
            "pilot_guard_path": pilot_publication["path"],
            "runtime_release": runtime_release,
        },
    }


def execute_live_calibration_action(
    action_input,
    context,
    *,
    artifact_path,
):
    configuration = _action_configuration(
        action_input,
        gate_id="P2-09",
    )
    repository_root = _repository_root(context)
    integration_head = _git_oid(
        repository_root,
        context["integration_head"],
    )
    if _git(repository_root, "rev-parse", "HEAD") != integration_head:
        raise Phase2GateError(
            "live calibration repository is not at the gate head"
        )
    before_status = _git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if before_status:
        raise Phase2GateError(
            "live calibration repository must be clean"
        )
    authorization_path = context.get("authorization_path")
    permit = _require_current_live_provider_authorization(
        authorization_path,
        context,
    )
    authority = _action_authority_files(
        configuration.get("authority_artifacts"),
        required=("evaluator",),
        authority_roots=context.get("authority_roots"),
    )
    protocol_path = _safe_file(
        context.get("protocol_path"),
        "live calibration protocol",
    )
    _require_authority_root(
        protocol_path,
        context.get("authority_roots"),
        "live calibration protocol",
    )
    protocol = _read_json(
        protocol_path,
        "live calibration protocol",
    )
    try:
        validate_experiment_protocol(protocol)
    except Exception as exc:
        raise Phase2GateError(
            "live calibration protocol is invalid"
        ) from exc
    protocol_sha256 = canonical_json_sha256(protocol)
    if (
        protocol_sha256 != _sha256_value(
            context,
            "protocol_sha256",
        )
        or protocol["repository"]["commit"] != integration_head
    ):
        raise Phase2GateError(
            "live calibration protocol does not bind the gate head"
        )
    environment = protocol["environment"]
    budgets = protocol["budgets"]
    if (
        environment["model"] != _text(context, "model")
        or environment["reasoning_profile"]
        != _text(context, "reasoning_profile")
        or environment["max_inflight_model_invocations"] != 1
        or budgets["max_total_tokens"]
        != _positive_int(context, "max_total_tokens")
        or budgets["max_wall_time_seconds"]
        != context.get("max_wall_time_seconds")
    ):
        raise Phase2GateError(
            "live calibration protocol differs from authorized provider "
            "policy or budgets"
        )
    runtime_release = context.get("runtime_release")
    _validate_runtime_release_identity(
        runtime_release,
        source_commit=integration_head,
        require_files=True,
    )
    repeat_mode = configuration.get("repeat_mode")
    if repeat_mode not in _MODES:
        raise Phase2GateError(
            "live calibration repeat mode is invalid"
        )
    expected_repeat_mode = expected_live_calibration_repeat_mode(
        protocol
    )
    if repeat_mode != expected_repeat_mode:
        raise Phase2GateError(
            "live calibration repeat mode does not match the "
            "counterbalanced protocol order"
        )
    sandbox_configuration = configuration.get(
        "sandbox_configuration"
    )
    if not isinstance(sandbox_configuration, dict):
        raise Phase2GateError(
            "live calibration sandbox configuration is invalid"
        )
    requested_projection_root = Path(
        context.get("projection_root") or ""
    ).expanduser()
    if _path_contains_symlink(requested_projection_root):
        raise Phase2GateError(
            "live calibration projection root is unsafe"
        )
    projection_root = requested_projection_root.resolve()
    _require_authority_root(
        projection_root,
        context.get("authority_roots"),
        "live calibration projection root",
    )
    projection_root.mkdir(parents=True, exist_ok=True)
    evaluator_path = authority["evaluator"]["path"]
    evaluator_bytes = authority["evaluator"]["bytes"]
    direct_taskpack = configuration.get("direct_taskpack")
    if (
        not isinstance(direct_taskpack, dict)
        or set(direct_taskpack) != {"path", "digest_sha256"}
        or not isinstance(direct_taskpack.get("path"), str)
    ):
        raise Phase2GateError(
            "live calibration direct taskpack binding is invalid"
        )
    direct_taskpack_path = str(
        Path(direct_taskpack["path"]).resolve()
    )
    _require_authority_root(
        direct_taskpack_path,
        context.get("authority_roots"),
        "live calibration direct taskpack",
    )
    direct_taskpack_digest = _sha256_value(
        {"digest": direct_taskpack.get("digest_sha256")},
        "digest",
    )
    try:
        from .taskpack import verify_frozen_taskpack_digest

        verify_frozen_taskpack_digest(
            direct_taskpack_path,
            direct_taskpack_digest,
        )
    except Exception as exc:
        raise Phase2GateError(
            "live calibration direct taskpack is invalid"
        ) from exc
    run_records = []
    sequence = [
        (0, mode) for mode in protocol["mode_order"]
    ] + [(1, repeat_mode)]
    for repetition_index, mode in sequence:
        stable_request_key = (
            f"phase2-live-e{context['authorization_epoch_number']}-"
            f"r{repetition_index}-{mode}"
        )
        allocation = allocate_experiment_run(
            projection_root,
            protocol,
            mode=mode,
            repetition_index=repetition_index,
            stable_request_key=stable_request_key,
            runtime_release=runtime_release,
        )
        run_dir = Path(allocation["run_dir"]).resolve()
        sealed_path = run_dir / "results" / "terminal"
        if sealed_path.is_dir():
            sealed = load_experiment_result_bundle(run_dir)
        else:
            if mode == "single_codex":
                adapter = SingleCodexModeAdapter()
            elif mode == "agentteam_direct":
                adapter = AgentTeamDirectModeAdapter(
                    direct_taskpack_path
                )
            else:
                adapter = AgentTeamFullModeAdapter()
            result = execute_bound_experiment_mode(
                allocation,
                sandbox_configuration=sandbox_configuration,
                adapter=adapter,
                common_finalizer=ExperimentCommonFinalizer(
                    evaluator_artifact=evaluator_path,
                    evaluator_bytes=evaluator_bytes,
                    runtime_release_identity=runtime_release,
                ),
            )
            sealed = result.get("sealed_result")
            if not isinstance(sealed, dict):
                raise Phase2GateError(
                    f"live calibration mode did not seal a result: {mode}"
                )
            sealed = load_experiment_result_bundle(run_dir)
        bundle = sealed["bundle"]
        if (
            bundle.get("terminal_status") != "completed"
            or bundle.get("acceptance_result", {}).get("status")
            != "passed"
        ):
            raise Phase2GateError(
                f"live calibration mode did not pass: {mode}"
            )
        run_records.append(
            {
                "run_dir": str(run_dir),
                "sandbox_authority_root": str(
                    run_dir / "authority"
                ),
                "canary_path": sandbox_configuration.get(
                    "canary_path"
                ),
                "manifest": allocation["run_manifest"],
                "mode": mode,
                "repetition_index": repetition_index,
                "sealed": sealed,
            }
        )
    primary_by_mode = {
        item["mode"]: item
        for item in run_records
        if item["repetition_index"] == 0
    }
    repeat_record = next(
        item
        for item in run_records
        if item["repetition_index"] == 1
    )

    def result_item(record):
        bundle = record["sealed"]["bundle"]
        started = datetime.fromisoformat(
            bundle["started_at"].replace("Z", "+00:00")
        )
        finished = datetime.fromisoformat(
            bundle["finished_at"].replace("Z", "+00:00")
        )
        return {
            "mode": bundle["mode"],
            "result_bundle_sha256": record["sealed"][
                "bundle_sha256"
            ],
            "total_tokens": bundle["usage_totals"]["total_tokens"],
            "wall_time_seconds": max(
                0.0,
                (finished - started).total_seconds(),
            ),
        }

    observed_invocations = sum(
        item["sealed"]["bundle"]["usage_coverage"][
            "total_invocations"
        ]
        for item in run_records
    )
    covered_invocations = sum(
        item["sealed"]["bundle"]["usage_coverage"][
            "covered_invocations"
        ]
        for item in run_records
    )
    if (
        observed_invocations < 1
        or covered_invocations != observed_invocations
    ):
        raise Phase2GateError(
            "live calibration usage coverage is incomplete"
        )
    artifact = {
        "schema_version": "phase2_calibration.v1",
        "controller_validation_status": "passed",
        "validated_code_sha": integration_head,
        "readiness_promotion_sha256": _sha256_value(
            context,
            "readiness_promotion_sha256",
        ),
        "protocol_sha256": protocol_sha256,
        "pilot_guard_status": "passed",
        "operator_authorization_sha256": permit[
            "authorization_sha256"
        ],
        "runtime_release": runtime_release,
        "mode_results": [
            result_item(primary_by_mode[mode])
            for mode in _MODES
        ],
        "repeat_result": result_item(repeat_record),
        "lifecycle_coverage_percent": 100,
        "token_coverage_percent": 100,
        "projection_rebuild_status": "passed",
    }
    artifact_publication = _publish_immutable_json(
        artifact_path,
        artifact,
    )
    if (
        _git(repository_root, "rev-parse", "HEAD")
        != integration_head
        or _git(
            repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        != before_status
    ):
        raise Phase2GateError(
            "live calibration changed the source repository"
        )
    relation_records = {
        mode: {
            key: value
            for key, value in primary_by_mode[mode].items()
            if key != "sealed"
        }
        for mode in _MODES
    }
    relation_repeat = {
        key: value
        for key, value in repeat_record.items()
        if key != "sealed"
    }
    return {
        "gate_id": "P2-09",
        "action_status": "completed",
        "integration_head": integration_head,
        "artifact_path": artifact_publication["path"],
        "artifact_sha256": artifact_publication["sha256"],
        "provider_calls": observed_invocations,
        "target_mutations": 0,
        "relation_context": {
            "protocol_path": str(protocol_path),
            "projection_root": str(projection_root),
            "runtime_release": runtime_release,
            "mode_run_records": relation_records,
            "repeat_run_record": relation_repeat,
        },
    }


def expected_live_calibration_repeat_mode(protocol):
    mode_order = protocol.get("mode_order")
    repetition_policy = protocol.get("repetition_policy")
    if (
        not isinstance(mode_order, list)
        or not mode_order
        or not all(mode in _MODES for mode in mode_order)
        or len(set(mode_order)) != len(mode_order)
        or not isinstance(repetition_policy, dict)
        or repetition_policy.get("count", 0) < 2
    ):
        raise Phase2GateError(
            "live calibration protocol cannot derive a repeat mode"
        )
    return mode_order[1 % len(mode_order)]


def execute_phase2_finalization_action(
    action_input,
    context,
    *,
    artifact_path,
    verification_path,
):
    configuration = _action_configuration(
        action_input,
        gate_id="P2-10",
    )
    repository_root = _repository_root(context)
    worktree = Path(
        context.get("integration_worktree") or ""
    ).resolve()
    if not worktree.is_dir():
        raise Phase2GateError(
            "Phase 2 finalization worktree is unavailable"
        )
    validated_code = _git_oid(
        repository_root,
        context["integration_head"],
    )
    if (
        _git(worktree, "rev-parse", "HEAD") != validated_code
        or _git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    ):
        raise Phase2GateError(
            "Phase 2 finalization requires a clean validated worktree"
        )
    prior_artifacts = _action_authority_files(
        context.get("prior_artifacts"),
        required=("readiness_promotion", "live_calibration"),
        authority_roots=context.get("authority_roots"),
    )
    readiness = _read_json(
        prior_artifacts["readiness_promotion"]["path"],
        "readiness promotion",
    )
    calibration = _read_json(
        prior_artifacts["live_calibration"]["path"],
        "live calibration",
    )
    finalized_at = _utc_text(
        configuration.get("finalized_at"),
        "Phase 2 finalized_at",
    )
    report_path = worktree / _REPORT_PATH
    roadmap_path = worktree / _ROADMAP_PATH
    if not roadmap_path.is_file():
        raise Phase2GateError("Phase 2 roadmap is unavailable")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = _render_phase2_report(
        readiness,
        calibration,
        finalized_at=finalized_at,
    )
    report_path.write_text(report_text, encoding="utf-8")
    roadmap_text = roadmap_path.read_text(encoding="utf-8")
    roadmap_path.write_text(
        _render_phase2_roadmap(
            roadmap_text,
            calibration,
            finalized_at=finalized_at,
        ),
        encoding="utf-8",
    )
    status = _git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    changed_status_paths = sorted(
        line.lstrip().split(maxsplit=1)[1]
        for line in status.splitlines()
        if len(line.lstrip().split(maxsplit=1)) == 2
    )
    if changed_status_paths != [_ROADMAP_PATH, _REPORT_PATH]:
        raise Phase2GateError(
            "Phase 2 finalization changed unexpected paths: "
            + ", ".join(changed_status_paths)
        )
    _git(worktree, "add", "--", _ROADMAP_PATH, _REPORT_PATH)
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=AgentTeam Phase 2 Controller",
            "-c",
            "user.email=agentteam-phase2@localhost",
            "commit",
            "--quiet",
            "-m",
            "finalize phase 2 experiment harness",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": finalized_at,
            "GIT_COMMITTER_DATE": finalized_at,
        },
    )
    if completed.returncode != 0:
        raise Phase2GateError(
            completed.stderr.strip()
            or "Phase 2 finalization commit failed"
        )
    final_head = _git(worktree, "rev-parse", "HEAD")
    if (
        _git_parents(repository_root, final_head) != [validated_code]
        or _git_changed_paths(
            repository_root,
            validated_code,
            final_head,
        )
        != [_ROADMAP_PATH, _REPORT_PATH]
    ):
        raise Phase2GateError(
            "Phase 2 finalization did not create the exact report child"
        )
    command = context.get("full_verification_command")
    verification = _run_full_verification_command(
        command,
        worktree,
    )
    if verification["returncode"] != 0:
        raise Phase2GateError(
            "Phase 2 finalization verification failed"
        )
    if _git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise Phase2GateError(
            "Phase 2 finalization verification changed the final commit"
        )
    verification["integration_head"] = final_head
    verification_publication = _publish_immutable_json(
        verification_path,
        verification,
    )
    artifact = {
        "schema_version": "phase2_finalization.v1",
        "controller_validation_status": "passed",
        "validated_code_sha": validated_code,
        "final_report_sha": final_head,
        "readiness_promotion_sha256": prior_artifacts[
            "readiness_promotion"
        ]["sha256"],
        "live_calibration_sha256": prior_artifacts[
            "live_calibration"
        ]["sha256"],
        "report_sha256": hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest(),
        "roadmap_sha256": hashlib.sha256(
            roadmap_path.read_bytes()
        ).hexdigest(),
        "full_verification_sha256": verification_publication[
            "sha256"
        ],
        "changed_paths": [_ROADMAP_PATH, _REPORT_PATH],
        "merge_recommendation": "ready_for_operator_review",
    }
    artifact_publication = _publish_immutable_json(
        artifact_path,
        artifact,
    )
    return {
        "gate_id": "P2-10",
        "action_status": "completed",
        "validated_code_sha": validated_code,
        "integration_head": final_head,
        "artifact_path": artifact_publication["path"],
        "artifact_sha256": artifact_publication["sha256"],
        "verification_path": verification_publication["path"],
        "provider_calls": 0,
        "target_mutations": 0,
        "relation_context": {
            "full_verification_path": verification_publication["path"],
        },
    }


def _validate_readiness_relation(artifact, repository_root, context):
    current_head = _git_oid(
        repository_root,
        context["integration_head"],
    )
    promotion_head = current_head
    current_parents = _git_parents(repository_root, current_head)
    if (
        len(current_parents) == 1
        and _git_changed_paths(
            repository_root,
            current_parents[0],
            current_head,
        )
        == [_ROADMAP_PATH, _REPORT_PATH]
    ):
        promotion_head = current_parents[0]
    parents = _git_parents(repository_root, promotion_head)
    if len(parents) != 1:
        raise Phase2GateError(
            "readiness promotion must have exactly one parent"
        )
    if artifact["validated_code_sha"] != parents[0]:
        raise Phase2GateError(
            "readiness promotion parent differs from validated code"
        )
    changed = _git_changed_paths(
        repository_root,
        parents[0],
        promotion_head,
    )
    if changed != [_READINESS_PATH] or artifact["changed_paths"] != changed:
        raise Phase2GateError(
            "readiness promotion must change only the readiness record"
        )
    before = _git_bytes(repository_root, parents[0], _READINESS_PATH)
    after = _git_bytes(
        repository_root,
        promotion_head,
        _READINESS_PATH,
    )
    if hashlib.sha256(before).hexdigest() != artifact[
        "readiness_before_sha256"
    ]:
        raise Phase2GateError("readiness before digest mismatch")
    if hashlib.sha256(after).hexdigest() != artifact[
        "readiness_after_sha256"
    ]:
        raise Phase2GateError("readiness after digest mismatch")
    readiness = _json_bytes(after, "promoted readiness record")
    capabilities = readiness.get("capabilities")
    capability_ids = {
        item.get("capability_id")
        for item in capabilities
        if isinstance(item, dict)
    } if isinstance(capabilities, list) else set()
    if (
        not isinstance(capabilities, list)
        or len(capabilities) != 7
        or capability_ids != _CAPABILITY_IDS
        or set(artifact["capability_evidence"]) != _CAPABILITY_IDS
        or any(item.get("status") != "passed" for item in capabilities)
        or readiness.get("overall_status") != "passed"
        or readiness.get("pilot_authorized") is not True
    ):
        raise Phase2GateError(
            "promoted readiness record is not seven-of-seven passed"
        )
    readiness_by_id = {
        item["capability_id"]: item for item in capabilities
    }
    observed_test_ids = {}
    for capability_id, entries in artifact[
        "capability_evidence"
    ].items():
        if (
            not isinstance(entries, list)
            or tuple(item.get("test_id") for item in entries)
            != _CAPABILITY_TEST_IDS[capability_id]
            or any(item.get("status") != "passed" for item in entries)
        ):
            raise Phase2GateError(
                f"{capability_id} does not bind its fixed capability test"
            )
        observed_test_ids[capability_id] = tuple(
            item["test_id"] for item in entries
        )
        declared_paths = readiness_by_id[capability_id].get("evidence")
        if not isinstance(declared_paths, list):
            raise Phase2GateError(
                "promoted readiness capability evidence is invalid"
            )
        for evidence in entries:
            if evidence["artifact_path"] not in declared_paths:
                raise Phase2GateError(
                    "capability evidence is not declared by readiness"
                )
            evidence_bytes = _git_bytes(
                repository_root,
                promotion_head,
                evidence["artifact_path"],
            )
            if hashlib.sha256(evidence_bytes).hexdigest() != evidence[
                "sha256"
            ]:
                raise Phase2GateError(
                    "capability evidence digest mismatch"
                )
    _rerun_readiness_capability_tests(
        repository_root,
        current_head,
        observed_test_ids,
    )
    _require_canonical_json_digest(
        context["protocol_path"],
        artifact["protocol_sha256"],
        "protocol",
    )
    _require_canonical_json_digest(
        context["run_manifest_path"],
        artifact["run_manifest_sha256"],
        "run manifest",
    )
    _require_file_digest(
        context["deterministic_calibration_path"],
        artifact["deterministic_calibration_sha256"],
        "deterministic calibration",
    )
    _validated_deterministic_calibration(
        context["deterministic_calibration_path"],
        expected_runtime_source_commit=artifact["validated_code_sha"],
        calibration_request_path=context[
            "deterministic_calibration_request_path"
        ],
        calibration_closure_mounts=context.get(
            "calibration_closure_mounts"
        ),
    )
    _require_canonical_json_digest(
        context["pilot_manifest_path"],
        artifact["pilot_manifest_sha256"],
        "pilot manifest",
    )
    _require_file_digest(
        context["pilot_guard_path"],
        artifact["pilot_guard_sha256"],
        "pilot guard",
    )
    protocol = _read_json(
        context["protocol_path"],
        "readiness protocol",
    )
    run_manifest = _read_json(
        context["run_manifest_path"],
        "readiness run manifest",
    )
    try:
        validate_experiment_protocol(protocol)
        validate_experiment_run_manifest(run_manifest, protocol)
    except Exception as exc:
        raise Phase2GateError(
            "readiness protocol/run authority is invalid"
        ) from exc
    if (
        canonical_json_sha256(protocol)
        != artifact["protocol_sha256"]
        or protocol["repository"]["commit"] != promotion_head
        or protocol["repository"]["tree"]
        != _git(
            repository_root,
            "rev-parse",
            f"{promotion_head}^{{tree}}",
        )
    ):
        raise Phase2GateError(
            "readiness protocol does not bind the promotion commit"
        )
    pilot_manifest = _read_json(
        context["pilot_manifest_path"],
        "pilot manifest",
    )
    if (
        pilot_manifest.get("repository", {}).get("commit")
        != promotion_head
        or pilot_manifest.get("readiness_binding", {}).get(
            "record_sha256"
        )
        != canonical_json_sha256(readiness)
    ):
        raise Phase2GateError(
            "pilot manifest does not bind promoted readiness"
        )
    pilot_guard = _read_json(context["pilot_guard_path"], "pilot guard")
    if (
        artifact["pilot_guard_provider_calls"] != 0
        or artifact["pilot_guard_target_mutations"] != 0
        or pilot_guard.get("provider_calls") != 0
        or pilot_guard.get("target_mutations") != 0
    ):
        raise Phase2GateError(
            "pilot guard did not prove zero calls and mutations"
        )
    runtime_release = context.get("runtime_release")
    _validate_runtime_release_identity(
        runtime_release,
        source_commit=promotion_head,
        require_files=True,
    )
    if artifact["candidate_release"] != runtime_release:
        raise Phase2GateError(
            "candidate release evidence binding mismatch"
        )
    with tempfile.TemporaryDirectory(
        prefix="agentteam-phase2-readiness-"
    ) as temporary:
        temporary = Path(temporary)
        manifest_path = temporary / "pilot-manifest.json"
        readiness_path = temporary / "readiness.json"
        manifest_path.write_bytes(
            _safe_file(
                context["pilot_manifest_path"],
                "pilot manifest",
            ).read_bytes()
        )
        readiness_path.write_bytes(after)
        try:
            recomputed_guard = _check_pilot_authorization_with_record(
                manifest_path,
                readiness_path,
            )
        except Exception as exc:
            raise Phase2GateError(
                "pilot authorization recomputation failed"
            ) from exc
    if recomputed_guard.get("pilot_authorized") is not True:
        raise Phase2GateError(
            "recomputed pilot authorization is not approved"
        )
    return {
        "integration_head": current_head,
        "readiness_promotion_head": promotion_head,
        "parent_commit": parents[0],
        "capability_count": 7,
        "provider_calls": 0,
        "target_mutations": 0,
    }


def _rerun_readiness_capability_tests(
    repository_root,
    integration_head,
    observed_test_ids,
):
    if observed_test_ids != _CAPABILITY_TEST_IDS:
        raise Phase2GateError(
            "readiness capability test inventory differs from registry"
        )
    runtime_root = (
        repository_root
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    )
    if not runtime_root.is_dir():
        raise Phase2GateError(
            "readiness capability test root is unavailable"
        )
    before_head = _git(repository_root, "rev-parse", "HEAD")
    before_status = _git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if before_head != integration_head or before_status:
        raise Phase2GateError(
            "readiness capability tests require a clean evidence commit"
        )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            *(
                test_id
                for test_ids in observed_test_ids.values()
                for test_id in test_ids
            ),
        ],
        cwd=runtime_root,
        env=_candidate_verification_env(repository_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=1800,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise Phase2GateError(
            "fixed readiness capability tests failed: "
            + detail[-2000:]
        )
    if (
        _git(repository_root, "rev-parse", "HEAD") != integration_head
        or _git(
            repository_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    ):
        raise Phase2GateError(
            "fixed readiness capability tests changed the evidence commit"
        )


def _validate_live_calibration_relation(
    artifact,
    repository_root,
    context,
):
    authorization_path = context.get("authorization_path")
    permit = require_live_provider_authorization(
        authorization_path,
        context,
    )
    authorization = _read_json(
        authorization_path,
        "Phase 2 live authorization",
    )
    try:
        authorized_at = datetime.fromisoformat(
            authorization["authorized_at"].replace("Z", "+00:00")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Phase2GateError(
            "live authorization timestamp is invalid"
        ) from exc
    if artifact["operator_authorization_sha256"] != permit[
        "authorization_sha256"
    ]:
        raise Phase2GateError(
            "calibration does not bind current authorization"
        )
    if artifact["readiness_promotion_sha256"] != _sha256_value(
        context,
        "readiness_promotion_sha256",
    ):
        raise Phase2GateError(
            "calibration readiness promotion binding mismatch"
        )
    if artifact["protocol_sha256"] != _sha256_value(
        context,
        "protocol_sha256",
    ):
        raise Phase2GateError("calibration protocol binding mismatch")
    runtime_release = context.get("runtime_release")
    _validate_runtime_release_identity(
        runtime_release,
        source_commit=artifact["validated_code_sha"],
        require_files=True,
    )
    if artifact["runtime_release"] != runtime_release:
        raise Phase2GateError(
            "calibration runtime release binding mismatch"
        )
    validated_code = _git_oid(
        repository_root,
        artifact["validated_code_sha"],
    )
    current_head = _git_oid(
        repository_root,
        context["integration_head"],
    )
    if current_head != validated_code:
        if (
            _git_parents(repository_root, current_head)
            != [validated_code]
            or _git_changed_paths(
                repository_root,
                validated_code,
                current_head,
            )
            != [_ROADMAP_PATH, _REPORT_PATH]
        ):
            raise Phase2GateError(
                "historical live calibration is not followed by the exact "
                "single-child finalization commit"
            )
    modes = [item["mode"] for item in artifact["mode_results"]]
    if tuple(modes) != _MODES or len(set(modes)) != len(_MODES):
        raise Phase2GateError(
            "live calibration modes are incomplete or duplicated"
        )
    if artifact["repeat_result"]["mode"] not in _MODES:
        raise Phase2GateError("repeat result mode is invalid")
    if (
        artifact["lifecycle_coverage_percent"] != 100
        or artifact["token_coverage_percent"] != 100
    ):
        raise Phase2GateError(
            "live calibration usage coverage is incomplete"
        )
    run_records = context.get("mode_run_records")
    repeat_record = context.get("repeat_run_record")
    if (
        not isinstance(run_records, dict)
        or set(run_records) != set(_MODES)
        or not isinstance(repeat_record, dict)
    ):
        raise Phase2GateError(
            "live calibration sealed result inventory is incomplete"
        )
    protocol_path = _safe_file(
        context["protocol_path"],
        "live calibration protocol",
    )
    protocol = _read_json(
        protocol_path,
        "live calibration protocol",
    )
    if canonical_json_sha256(protocol) != artifact["protocol_sha256"]:
        raise Phase2GateError(
            "live calibration protocol digest mismatch"
        )
    projection_root = context.get("projection_root")
    try:
        loaded_primary = [
            _load_run_record(
                run_records[mode],
                protocol,
                artifact["protocol_sha256"],
                Path(projection_root).resolve(),
            )
            for mode in _MODES
        ]
        loaded_repeat = _load_run_record(
            repeat_record,
            protocol,
            artifact["protocol_sha256"],
            Path(projection_root).resolve(),
        )
        _validate_mode_family(loaded_primary, loaded_repeat)
        _validate_unique_results([*loaded_primary, loaded_repeat])
        _validate_equal_contract([*loaded_primary, loaded_repeat])
        recomputed_coverage = _validate_usage_coverage(
            [*loaded_primary, loaded_repeat]
        )
    except (
        ExperimentCalibrationError,
        OSError,
        TypeError,
    ) as exc:
        raise Phase2GateError(
            "live calibration authority recomputation failed"
        ) from exc
    for loaded in [*loaded_primary, loaded_repeat]:
        try:
            started_at = datetime.fromisoformat(
                loaded["bundle"]["started_at"].replace(
                    "Z",
                    "+00:00",
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise Phase2GateError(
                "live calibration start timestamp is invalid"
            ) from exc
        if started_at < authorized_at:
            raise Phase2GateError(
                "live calibration provider run predates authorization"
            )
    if (
        recomputed_coverage.get("lifecycle_percent") != 100
        or recomputed_coverage.get("token_percent") != 100
    ):
        raise Phase2GateError(
            "live calibration recomputed usage coverage is incomplete"
        )
    observed_invocations = 0
    covered_invocations = 0
    for expected, loaded in zip(
        artifact["mode_results"],
        loaded_primary,
    ):
        _validate_mode_result_bundle(
            expected,
            loaded["run_dir"],
            protocol_sha256=artifact["protocol_sha256"],
            source_commit=artifact["validated_code_sha"],
            authorization=context,
            runtime_release=runtime_release,
        )
        coverage = loaded["bundle"]["usage_coverage"]
        if coverage.get("status") != "complete":
            raise Phase2GateError(
                "sealed result usage coverage is not complete"
            )
        observed_invocations += coverage["total_invocations"]
        covered_invocations += coverage["covered_invocations"]
    _validate_mode_result_bundle(
        artifact["repeat_result"],
        loaded_repeat["run_dir"],
        protocol_sha256=artifact["protocol_sha256"],
        source_commit=artifact["validated_code_sha"],
        authorization=context,
        runtime_release=runtime_release,
    )
    repeat_coverage = loaded_repeat["bundle"]["usage_coverage"]
    if repeat_coverage.get("status") != "complete":
        raise Phase2GateError(
            "sealed repeat usage coverage is not complete"
        )
    observed_invocations += repeat_coverage["total_invocations"]
    covered_invocations += repeat_coverage["covered_invocations"]
    if (
        observed_invocations < 1
        or covered_invocations != observed_invocations
    ):
        raise Phase2GateError(
            "sealed result lifecycle coverage is incomplete"
        )
    return {
        "integration_head": current_head,
        "validated_code_sha": validated_code,
        "authorization_sha256": permit["authorization_sha256"],
        "mode_count": 3,
        "provider_calls": observed_invocations,
        "target_mutations": 0,
    }


def _validate_live_authorization(authorization, context):
    authorization_epoch_number = context.get(
        "authorization_epoch_number",
        context.get("epoch_number"),
    )
    authorization_epoch_sha256 = context.get(
        "authorization_epoch_sha256",
        context.get("epoch_sha256"),
    )
    expected = {
        "decision": "approved",
        "gate_id": "P2-09",
        "epoch_number": _positive_int(
            {"authorization_epoch_number": authorization_epoch_number},
            "authorization_epoch_number",
        ),
        "epoch_sha256": _sha256_value(
            {"authorization_epoch_sha256": authorization_epoch_sha256},
            "authorization_epoch_sha256",
        ),
        "protocol_sha256": _sha256_value(context, "protocol_sha256"),
        "readiness_promotion_sha256": _sha256_value(
            context,
            "readiness_promotion_sha256",
        ),
        "model": _text(context, "model"),
        "reasoning_profile": _text(context, "reasoning_profile"),
        "max_total_tokens": _positive_int(
            context,
            "max_total_tokens",
        ),
        "max_wall_time_seconds": context["max_wall_time_seconds"],
        "max_inflight_model_invocations": 1,
        "modes": list(_MODES),
    }
    mismatches = [
        field
        for field, value in expected.items()
        if authorization.get(field) != value
    ]
    if mismatches:
        raise Phase2GateError(
            "live authorization binding mismatch: "
            + ", ".join(mismatches)
        )


def _load_result_bundle(run_dir):
    try:
        return load_experiment_result_bundle(run_dir)
    except (ExperimentResultError, OSError, KeyError) as exc:
        raise Phase2GateError(
            "live calibration sealed result is invalid"
        ) from exc


def _validate_mode_result_bundle(
    expected,
    run_dir,
    *,
    protocol_sha256,
    source_commit,
    authorization,
    runtime_release,
):
    loaded = _load_result_bundle(run_dir)
    bundle = loaded["bundle"]
    run_dir = Path(run_dir).resolve()
    if loaded["bundle_sha256"] != expected["result_bundle_sha256"]:
        raise Phase2GateError(
            "live calibration result bundle digest mismatch"
        )
    if bundle["mode"] != expected["mode"]:
        raise Phase2GateError(
            "live calibration result mode mismatch"
        )
    if bundle["protocol_sha256"] != protocol_sha256:
        raise Phase2GateError(
            "live calibration result protocol mismatch"
        )
    if bundle["source_commit"] != source_commit:
        raise Phase2GateError(
            "live calibration result source commit mismatch"
        )
    _validate_runtime_release_identity(
        runtime_release,
        source_commit=source_commit,
        require_files=True,
    )
    if bundle["runtime_release_identity"] != runtime_release:
        raise Phase2GateError(
            "live calibration result runtime release mismatch"
        )
    authority_root = run_dir.parent.parent
    protocol = _read_json(
        authority_root
        / "protocols"
        / f"{bundle['protocol_sha256']}.json",
        "live calibration protocol authority",
    )
    manifest = _read_json(
        authority_root
        / "run-manifests"
        / f"{bundle['run_manifest_sha256']}.json",
        "live calibration run manifest authority",
    )
    try:
        validate_experiment_protocol(protocol)
        validate_experiment_run_manifest(manifest, protocol)
    except Exception as exc:
        raise Phase2GateError(
            "live calibration protocol authority is invalid"
        ) from exc
    if (
        canonical_json_sha256(protocol) != bundle["protocol_sha256"]
        or canonical_json_sha256(manifest)
        != bundle["run_manifest_sha256"]
    ):
        raise Phase2GateError(
            "live calibration protocol authority digest mismatch"
        )
    environment = protocol["environment"]
    if (
        environment["model"] != _text(authorization, "model")
        or environment["reasoning_profile"]
        != _text(authorization, "reasoning_profile")
        or environment["max_inflight_model_invocations"] != 1
        or manifest["mode"] != expected["mode"]
    ):
        raise Phase2GateError(
            "live calibration provider policy differs from authorization"
        )
    if (
        protocol["budgets"]["max_total_tokens"]
        != _positive_int(authorization, "max_total_tokens")
        or protocol["budgets"]["max_wall_time_seconds"]
        != authorization["max_wall_time_seconds"]
    ):
        raise Phase2GateError(
            "live calibration protocol budget differs from authorization"
        )
    if bundle["usage_totals"]["total_tokens"] != expected["total_tokens"]:
        raise Phase2GateError(
            "live calibration token total mismatch"
        )
    try:
        started = datetime.fromisoformat(
            bundle["started_at"].replace("Z", "+00:00")
        )
        finished = datetime.fromisoformat(
            bundle["finished_at"].replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise Phase2GateError(
            "live calibration result timestamps are invalid"
        ) from exc
    elapsed = max(0.0, (finished - started).total_seconds())
    if abs(elapsed - expected["wall_time_seconds"]) > 1e-6:
        raise Phase2GateError(
            "live calibration wall-time total mismatch"
        )
    budget = bundle["budget_result"]
    if (
        budget.get("max_total_tokens")
        != _positive_int(authorization, "max_total_tokens")
        or budget.get("max_wall_time_seconds")
        != authorization["max_wall_time_seconds"]
    ):
        raise Phase2GateError(
            "live calibration result budget differs from authorization"
        )


def _validate_finalization_relation(
    artifact,
    repository_root,
    context,
):
    head = _git_oid(repository_root, context["integration_head"])
    parents = _git_parents(repository_root, head)
    if len(parents) != 1 or parents[0] != artifact[
        "validated_code_sha"
    ]:
        raise Phase2GateError(
            "finalization must be a single-child report commit"
        )
    if artifact["final_report_sha"] != head:
        raise Phase2GateError("final report SHA differs from integration")
    prior_validated = context.get("prior_gate_validated_code", {})
    if (
        not isinstance(prior_validated, dict)
        or prior_validated.get("P2-09")
        != artifact["validated_code_sha"]
    ):
        raise Phase2GateError(
            "finalization parent is not the live calibration code"
        )
    changed = _git_changed_paths(repository_root, parents[0], head)
    expected_paths = [_ROADMAP_PATH, _REPORT_PATH]
    if changed != expected_paths or artifact["changed_paths"] != expected_paths:
        raise Phase2GateError(
            "finalization commit does not contain the exact report paths"
        )
    if hashlib.sha256(
        _git_bytes(repository_root, head, _REPORT_PATH)
    ).hexdigest() != artifact["report_sha256"]:
        raise Phase2GateError("Phase 2 report digest mismatch")
    if hashlib.sha256(
        _git_bytes(repository_root, head, _ROADMAP_PATH)
    ).hexdigest() != artifact["roadmap_sha256"]:
        raise Phase2GateError("roadmap digest mismatch")
    for field in (
        "readiness_promotion_sha256",
        "live_calibration_sha256",
    ):
        prior_gate_id = (
            "P2-08"
            if field == "readiness_promotion_sha256"
            else "P2-09"
        )
        prior_evidence = context.get("prior_gate_evidence", {})
        if (
            not isinstance(prior_evidence, dict)
            or artifact[field] != prior_evidence.get(prior_gate_id)
        ):
            raise Phase2GateError(f"{field} binding mismatch")
    _validate_full_verification(
        artifact,
        repository_root,
        head,
        context,
    )
    return {
        "integration_head": head,
        "parent_commit": parents[0],
        "changed_paths": expected_paths,
        "provider_calls": 0,
        "target_mutations": 0,
    }


def _validate_full_verification(
    artifact,
    repository_root,
    head,
    context,
):
    worktree = Path(context.get("integration_worktree") or "").resolve()
    if not worktree.is_dir():
        raise Phase2GateError(
            "finalization integration worktree is unavailable"
        )
    if _git(worktree, "rev-parse", "HEAD") != head:
        raise Phase2GateError(
            "finalization worktree is not at the evidence commit"
        )
    if _git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        raise Phase2GateError(
            "finalization integration worktree is not clean"
        )
    command = context.get("full_verification_command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise Phase2GateError(
            "finalization verification command is invalid"
        )
    recomputed = _run_full_verification_command(command, worktree)
    if recomputed["returncode"] != 0:
        raise Phase2GateError(
            "finalization full verification failed"
        )
    recorded = _read_json(
        context["full_verification_path"],
        "full verification",
    )
    expected = {
        key: recomputed[key]
        for key in (
            "command",
            "returncode",
            "normalized_stdout_sha256",
            "normalized_stderr_sha256",
        )
    }
    if (
        {
            key: recorded.get(key)
            for key in expected
        }
        != expected
        or recorded.get("integration_head") != head
    ):
        raise Phase2GateError(
            "full verification evidence differs from recomputation"
        )
    _require_file_digest(
        context["full_verification_path"],
        artifact["full_verification_sha256"],
        "full verification",
    )
    if _git(worktree, "rev-parse", "HEAD") != head or _git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ):
        raise Phase2GateError(
            "full verification changed the finalization worktree"
        )


def _run_full_verification_command(command, worktree):
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise Phase2GateError(
            "finalization verification command is invalid"
        )
    completed = subprocess.run(
        command,
        cwd=worktree,
        env=_candidate_verification_env(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=3600,
    )
    stdout = completed.stdout
    stderr = completed.stderr
    return {
        "command": command,
        "returncode": completed.returncode,
        "normalized_stdout_sha256": hashlib.sha256(
            _normalize_verification_output(stdout).encode("utf-8")
        ).hexdigest(),
        "normalized_stderr_sha256": hashlib.sha256(
            _normalize_verification_output(stderr).encode("utf-8")
        ).hexdigest(),
    }


def _normalize_verification_output(value):
    normalized = re.sub(
        r"(Ran\s+\d+\s+tests?\s+in\s+)"
        r"\d+(?:\.\d+)?s",
        r"\1<elapsed>",
        value,
    )
    return re.sub(
        r"(?m)^(Initialized empty Git repository in )"
        r"(?:.*/)?tmp[A-Za-z0-9_-]+"
        r"(/repo/\.git/)$",
        r"\1<temporary-directory>\2",
        normalized,
    )


def _candidate_verification_env(worktree):
    env = dict(os.environ)
    env.pop("AGENTTEAM_LAUNCHER_SELECTION", None)
    runtime_path = (
        Path(worktree)
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    )
    if runtime_path.is_dir():
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(runtime_path)
            if not existing
            else str(runtime_path) + os.pathsep + existing
        )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _render_phase2_report(readiness, calibration, *, finalized_at):
    mode_lines = []
    for item in calibration.get("mode_results", []):
        mode_lines.append(
            f"| {item['mode']} | {item['total_tokens']} | "
            f"{item['wall_time_seconds']:.6f} |"
        )
    return "\n".join(
        [
            "# Phase 2 Experiment Harness",
            "",
            f"Finalized at: `{finalized_at}`",
            "",
            "## Readiness",
            "",
            f"- Overall status: `{readiness.get('controller_validation_status')}`",
            f"- Validated code: `{readiness.get('validated_code_sha')}`",
            f"- Capability groups: `{len(readiness.get('capability_evidence', {}))}`",
            "",
            "## Live Calibration",
            "",
            "| Mode | Total tokens | Wall time (seconds) |",
            "| --- | ---: | ---: |",
            *mode_lines,
            "",
            f"- Repeat mode: `{calibration.get('repeat_result', {}).get('mode')}`",
            f"- Lifecycle coverage: `{calibration.get('lifecycle_coverage_percent')}%`",
            f"- Token coverage: `{calibration.get('token_coverage_percent')}%`",
            "",
            "The results are calibration evidence for the harness and do not "
            "establish benchmark superiority.",
            "",
        ]
    )


def _render_phase2_roadmap(roadmap, calibration, *, finalized_at):
    start = "<!-- phase2-experiment-harness:start -->"
    end = "<!-- phase2-experiment-harness:end -->"
    section = "\n".join(
        [
            start,
            "## Phase 2 Experiment Harness",
            "",
            f"- Finalized: `{finalized_at}`",
            "- Status: `ready_for_operator_review`",
            (
                "- Live calibration usage coverage: "
                f"`{calibration.get('token_coverage_percent')}%`"
            ),
            "- Next phase: research benchmark execution and comparison.",
            end,
        ]
    )
    if start in roadmap and end in roadmap:
        prefix, remainder = roadmap.split(start, 1)
        _old, suffix = remainder.split(end, 1)
        return prefix.rstrip() + "\n\n" + section + suffix
    return roadmap.rstrip() + "\n\n" + section + "\n"


def _build_pilot_manifest(protocol, readiness, *, mode):
    return {
        "schema_version": "agentteam_experiment_manifest.v1",
        "experiment_id": protocol["experiment_id"],
        "instance_id": protocol["instance_id"],
        "repository": {
            "source": protocol["repository"]["source"],
            "commit": protocol["repository"]["commit"],
            "git_object_format": protocol["repository"][
                "git_object_format"
            ],
        },
        "goal": {
            "summary": protocol["goal"]["summary"],
            "constraints": list(protocol["goal"]["constraints"]),
        },
        "acceptance": {
            "command": list(protocol["acceptance"]["command"]),
        },
        "mode": mode,
        "runtime": {
            "backend": protocol["environment"]["backend"],
            "model": protocol["environment"]["model"],
            "sandbox_policy": protocol["environment"][
                "sandbox_policy"
            ],
        },
        "seed": protocol["seed"],
        "blind_gold": dict(protocol["blind_gold"]),
        "budgets": {
            "max_total_tokens": protocol["budgets"][
                "max_total_tokens"
            ],
            "max_wall_time_seconds": protocol["budgets"][
                "max_wall_time_seconds"
            ],
            "stop_boundary": "scheduler_safe",
        },
        "usage_contract_version": protocol[
            "usage_contract_version"
        ],
        "readiness_binding": {
            "schema_version": readiness["schema_version"],
            "record_sha256": canonical_json_sha256(readiness),
        },
    }


def _coerce_spec(value):
    if isinstance(value, GateSpec):
        return value
    if isinstance(value, dict):
        return resolve_gate_spec(value)
    raise Phase2GateError("gate spec is invalid")


def _action_configuration(action_input, *, gate_id):
    _validate_action_input_structure(action_input, gate_id)
    return action_input["configuration"]


def _validate_action_input_structure(action_input, gate_id):
    if (
        not isinstance(action_input, dict)
        or set(action_input) != {
            "schema_version",
            "action",
            "configuration",
        }
        or action_input.get("schema_version")
        != "phase2_gate_action_input.v1"
        or action_input.get("action")
        != _CONTROLLER_ACTIONS[gate_id]
        or not isinstance(action_input.get("configuration"), dict)
    ):
        raise Phase2GateError(
            f"{gate_id} controller action input is invalid"
        )
    configuration = action_input["configuration"]
    if gate_id == "P2-08":
        expected = {
            "promoted_at",
            "readiness_reasons",
            "capability_evidence",
            "authority_artifacts",
            "pilot_mode",
            "pilot_repetition_index",
            "pilot_stable_request_key",
        }
        if set(configuration) != expected:
            raise Phase2GateError(
                "P2-08 controller action configuration is incomplete"
            )
        _utc_text(configuration["promoted_at"], "readiness promoted_at")
        if (
            not isinstance(configuration["readiness_reasons"], dict)
            or set(configuration["readiness_reasons"]) != _CAPABILITY_IDS
            or any(
                not isinstance(value, str) or not value.strip()
                for value in configuration[
                    "readiness_reasons"
                ].values()
            )
        ):
            raise Phase2GateError(
                "P2-08 readiness reasons are incomplete"
            )
        _validate_capability_evidence_shape(
            configuration["capability_evidence"]
        )
        _validate_authority_bindings_shape(
            configuration["authority_artifacts"],
            required=(
                "protocol_template",
                "deterministic_calibration_request",
                "deterministic_calibration",
            ),
        )
        if configuration["pilot_mode"] not in _MODES:
            raise Phase2GateError("P2-08 pilot mode is invalid")
        repetition = configuration["pilot_repetition_index"]
        if (
            not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or repetition < 0
        ):
            raise Phase2GateError(
                "P2-08 pilot repetition index is invalid"
            )
        if (
            not isinstance(
                configuration["pilot_stable_request_key"],
                str,
            )
            or not _SAFE_ID.fullmatch(
                configuration["pilot_stable_request_key"]
            )
        ):
            raise Phase2GateError(
                "P2-08 pilot stable request key is invalid"
            )
    elif gate_id == "P2-09":
        if set(configuration) != {
            "authority_artifacts",
            "direct_taskpack",
            "repeat_mode",
            "sandbox_configuration",
        }:
            raise Phase2GateError(
                "P2-09 controller action configuration is incomplete"
            )
        _validate_authority_bindings_shape(
            configuration["authority_artifacts"],
            required=("evaluator",),
        )
        _validate_path_digest_binding(
            configuration["direct_taskpack"],
            digest_field="digest_sha256",
            label="P2-09 direct taskpack",
        )
        if configuration["repeat_mode"] not in _MODES:
            raise Phase2GateError("P2-09 repeat mode is invalid")
        if not isinstance(
            configuration["sandbox_configuration"],
            dict,
        ):
            raise Phase2GateError(
                "P2-09 sandbox configuration is invalid"
            )
    else:
        if set(configuration) != {"finalized_at"}:
            raise Phase2GateError(
                "P2-10 controller action configuration is incomplete"
            )
        _utc_text(
            configuration["finalized_at"],
            "Phase 2 finalized_at",
        )


def _validate_authority_bindings_shape(value, *, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise Phase2GateError(
            "controller authority artifact inventory is incomplete"
        )
    for name in required:
        _validate_path_digest_binding(
            value[name],
            digest_field="sha256",
            label=f"{name} controller authority",
        )


def _validate_path_digest_binding(value, *, digest_field, label):
    if (
        not isinstance(value, dict)
        or set(value) != {"path", digest_field}
        or not isinstance(value.get("path"), str)
        or not value["path"]
    ):
        raise Phase2GateError(f"{label} binding is invalid")
    _sha256_value(
        {"digest": value.get(digest_field)},
        "digest",
    )


def _validate_capability_evidence_shape(value):
    if not isinstance(value, dict) or set(value) != _CAPABILITY_IDS:
        raise Phase2GateError(
            "readiness controller capability evidence is incomplete"
        )
    for capability_id, test_ids in _CAPABILITY_TEST_IDS.items():
        entries = value[capability_id]
        if (
            not isinstance(entries, list)
            or tuple(
                item.get("test_id")
                for item in entries
                if isinstance(item, dict)
            )
            != test_ids
            or len(entries) != len(test_ids)
        ):
            raise Phase2GateError(
                f"{capability_id} controller evidence test IDs differ "
                "from the fixed registry"
            )
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry)
                != {
                    "artifact_path",
                    "sha256",
                    "test_id",
                    "status",
                }
                or not isinstance(entry["artifact_path"], str)
                or not entry["artifact_path"]
                or entry["status"] != "passed"
            ):
                raise Phase2GateError(
                    f"{capability_id} controller evidence is invalid"
                )
            _sha256_value(
                {"digest": entry["sha256"]},
                "digest",
            )


def build_readiness_capability_evidence(repository_root, commit):
    """Build the fixed readiness evidence inventory from a Git commit."""
    repository_root = Path(repository_root).resolve()
    commit = _git_oid(repository_root, commit)
    artifact_digests = {}
    evidence = {}
    for capability_id, test_ids in _CAPABILITY_TEST_IDS.items():
        entries = []
        for test_id in test_ids:
            matching_paths = [
                path
                for module, path in (
                    _CAPABILITY_TEST_ARTIFACT_PATHS.items()
                )
                if test_id.startswith(f"{module}.")
            ]
            if len(matching_paths) != 1:
                raise Phase2GateError(
                    f"{capability_id} registry test artifact is ambiguous"
                )
            artifact_path = matching_paths[0]
            if artifact_path not in artifact_digests:
                artifact_digests[artifact_path] = hashlib.sha256(
                    _git_bytes(repository_root, commit, artifact_path)
                ).hexdigest()
            entries.append(
                {
                    "artifact_path": artifact_path,
                    "sha256": artifact_digests[artifact_path],
                    "test_id": test_id,
                    "status": "passed",
                }
            )
        evidence[capability_id] = entries
    _validate_capability_evidence_shape(evidence)
    return evidence


def _action_authority_files(
    value,
    *,
    required,
    authority_roots=None,
):
    if not isinstance(value, dict) or set(value) != set(required):
        raise Phase2GateError(
            "controller authority artifact inventory is incomplete"
        )
    authority = {}
    for name in required:
        item = value[name]
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item.get("path"), str)
        ):
            raise Phase2GateError(
                f"controller authority artifact is invalid: {name}"
            )
        path = _safe_file(
            item["path"],
            f"{name} controller authority",
        )
        _require_authority_root(
            path,
            authority_roots,
            f"{name} controller authority",
        )
        digest = _sha256_value(
            {"sha256": item.get("sha256")},
            "sha256",
        )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise Phase2GateError(
                f"{name} controller authority is unreadable"
            ) from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise Phase2GateError(
                f"{name} controller authority digest mismatch"
            )
        authority[name] = {
            "path": str(path),
            "sha256": digest,
            "bytes": payload,
        }
    return authority


def _require_authority_root(path, authority_roots, label):
    if (
        not isinstance(authority_roots, (list, tuple))
        or not authority_roots
    ):
        raise Phase2GateError(
            f"{label} has no declared authority root"
        )
    requested = Path(path).expanduser()
    if _path_contains_symlink(requested):
        raise Phase2GateError(
            f"{label} is outside its declared authority roots"
        )
    resolved = requested.resolve()
    allowed = False
    for root in authority_roots:
        requested_root = Path(root).expanduser()
        if _path_contains_symlink(requested_root):
            continue
        root = requested_root.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        current = root
        unsafe = False
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                unsafe = True
                break
        if not unsafe:
            allowed = True
            break
    if not allowed:
        raise Phase2GateError(
            f"{label} is outside its declared authority roots"
        )


def _path_contains_symlink(path):
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _validate_runtime_release_identity(
    value,
    *,
    source_commit,
    require_files,
):
    if not isinstance(value, dict) or set(value) != _RUNTIME_RELEASE_FIELDS:
        raise Phase2GateError(
            "runtime release identity fields are invalid"
        )
    if (
        not isinstance(value["release_id"], str)
        or not _SAFE_ID.fullmatch(value["release_id"])
        or value["git_object_format"] not in {"sha1", "sha256"}
    ):
        raise Phase2GateError("runtime release identity is invalid")
    oid_length = 40 if value["git_object_format"] == "sha1" else 64
    if (
        not isinstance(value["source_commit"], str)
        or not re.fullmatch(
            rf"[0-9a-f]{{{oid_length}}}",
            value["source_commit"],
        )
        or value["source_commit"] != source_commit
    ):
        raise Phase2GateError(
            "runtime release source commit mismatch"
        )
    _sha256_value(
        {"release_manifest_sha256": value["release_manifest_sha256"]},
        "release_manifest_sha256",
    )
    for field in ("release_root", "runtime_root"):
        if not isinstance(value[field], str) or not value[field]:
            raise Phase2GateError(
                f"runtime release {field} is invalid"
            )
        requested = Path(value[field]).expanduser()
        if (
            _path_contains_symlink(requested)
            or str(requested.resolve()) != value[field]
        ):
            raise Phase2GateError(
                f"runtime release {field} is unsafe"
            )
    if not require_files:
        return
    release_root = Path(value["release_root"])
    runtime_root = Path(value["runtime_root"])
    expected_runtime_root = (
        release_root
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    )
    if (
        not release_root.is_dir()
        or runtime_root != expected_runtime_root
        or not runtime_root.is_dir()
    ):
        raise Phase2GateError(
            "runtime release layout is invalid"
        )
    manifest_path = release_root / "manifest.json"
    manifest = _read_json(manifest_path, "runtime release manifest")
    if _sha256_file(manifest_path) != value["release_manifest_sha256"]:
        raise Phase2GateError(
            "runtime release manifest digest mismatch"
        )
    manifest_source_commit = (
        manifest.get("source_commit")
        or manifest.get("source_git_commit")
    )
    expected_manifest = {
        "release_id": value["release_id"],
        "release_root": value["release_root"],
        "runtime_root": value["runtime_root"],
        "source_commit": value["source_commit"],
        "git_object_format": value["git_object_format"],
    }
    observed_manifest = {
        "release_id": manifest.get("release_id"),
        "release_root": manifest.get("release_root"),
        "runtime_root": manifest.get("runtime_root"),
        "source_commit": manifest_source_commit,
        "git_object_format": manifest.get("git_object_format"),
    }
    if observed_manifest != expected_manifest:
        raise Phase2GateError(
            "runtime release manifest binding mismatch"
        )
    runtime_entrypoint = (
        runtime_root / "agentteam_runtime" / "agentteam.py"
    )
    if (
        _path_contains_symlink(runtime_entrypoint)
        or not runtime_entrypoint.is_file()
    ):
        raise Phase2GateError(
            "runtime release entrypoint is missing or unsafe"
        )


def _readiness_action_evidence(value, repository_root, commit):
    if not isinstance(value, dict) or set(value) != _CAPABILITY_IDS:
        raise Phase2GateError(
            "readiness controller capability evidence is incomplete"
        )
    normalized = {}
    for capability_id, expected_test_ids in (
        _CAPABILITY_TEST_IDS.items()
    ):
        entries = value[capability_id]
        if (
            not isinstance(entries, list)
            or any(not isinstance(item, dict) for item in entries)
            or tuple(item.get("test_id") for item in entries)
            != expected_test_ids
        ):
            raise Phase2GateError(
                f"{capability_id} controller evidence test IDs differ "
                "from the fixed registry"
            )
        normalized_entries = []
        for item in entries:
            if (
                not isinstance(item, dict)
                or set(item) != {
                    "artifact_path",
                    "sha256",
                    "test_id",
                    "status",
                }
                or item.get("status") != "passed"
            ):
                raise Phase2GateError(
                    f"{capability_id} controller evidence is invalid"
                )
            evidence_bytes = _git_bytes(
                repository_root,
                commit,
                item["artifact_path"],
            )
            if hashlib.sha256(evidence_bytes).hexdigest() != item[
                "sha256"
            ]:
                raise Phase2GateError(
                    f"{capability_id} controller evidence digest mismatch"
                )
            normalized_entries.append(dict(item))
        normalized[capability_id] = normalized_entries
    return normalized


def _utc_text(value, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise Phase2GateError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise Phase2GateError(f"{label} is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise Phase2GateError(f"{label} must be UTC")
    return value


def _repository_root(context):
    if not isinstance(context, dict):
        raise Phase2GateError("gate context must be an object")
    root = Path(context.get("repository_root") or "").resolve()
    if not root.is_dir():
        raise Phase2GateError("gate repository root is unavailable")
    return root


def _validate_schema(value, path, label):
    schema = _read_json(path, f"{label} schema")
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        location = ".".join(
            str(item) for item in errors[0].absolute_path
        ) or "<root>"
        raise Phase2GateError(
            f"{label} schema validation failed at {location}: "
            f"{errors[0].message}"
        )


def _publish_immutable_json(path, value):
    requested_path = Path(path).expanduser()
    if requested_path.is_symlink():
        raise Phase2GateError(
            "immutable gate artifact path must not be a symlink"
        )
    path = requested_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise Phase2GateError(
                "immutable gate artifact conflicts with existing bytes"
            )
        created = False
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        created = True
    return {
        "path": str(path),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "created": created,
    }


def _read_json(path, label):
    path = _safe_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase2GateError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise Phase2GateError(f"{label} must be an object")
    return value


def _json_bytes(value, label):
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase2GateError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise Phase2GateError(f"{label} must be an object")
    return decoded


def _safe_file(path, label):
    requested_path = Path(path).expanduser()
    if requested_path.is_symlink():
        raise Phase2GateError(f"{label} is missing or unsafe")
    path = requested_path.resolve()
    if not path.is_file():
        raise Phase2GateError(f"{label} is missing or unsafe")
    return path


def _require_file_digest(path, expected, label):
    if _sha256_file(_safe_file(path, label)) != expected:
        raise Phase2GateError(f"{label} digest mismatch")


def _validated_deterministic_calibration(
    path,
    *,
    expected_runtime_source_commit,
    calibration_request_path,
    calibration_bytes=None,
    calibration_request_bytes=None,
    calibration_closure_mounts=None,
):
    try:
        loaded = (
            load_deterministic_calibration_report(path)
            if calibration_bytes is None
            else load_deterministic_calibration_report_bytes(
                calibration_bytes
            )
        )
    except (ExperimentCalibrationError, OSError) as exc:
        raise Phase2GateError(
            "deterministic calibration authority is invalid"
        ) from exc
    runtime_release = loaded["report"].get(
        "runtime_release_identity"
    )
    _validate_runtime_release_identity(
        runtime_release,
        source_commit=expected_runtime_source_commit,
        require_files=True,
    )
    request_sha256 = loaded["report"].get(
        "calibration_request_sha256"
    )
    if not request_sha256:
        raise Phase2GateError(
            "deterministic calibration request authority is missing"
        )
    request_bytes = calibration_request_bytes
    if request_bytes is None:
        request_bytes = _safe_file(
            calibration_request_path,
            "deterministic calibration request",
        ).read_bytes()
    if hashlib.sha256(request_bytes).hexdigest() != request_sha256:
        raise Phase2GateError(
            "deterministic calibration request digest mismatch"
        )
    try:
        _validate_calibration_closure_mounts(
            calibration_closure_mounts
        )
        recomputed = run_deterministic_calibration_from_manifest(
            calibration_request_path,
            manifest_bytes=request_bytes,
        )
    except (ExperimentCalibrationError, OSError) as exc:
        raise Phase2GateError(
            "deterministic calibration recomputation failed"
        ) from exc
    _validate_calibration_closure_mounts(
        calibration_closure_mounts
    )
    if recomputed["report_sha256"] != loaded["report_sha256"]:
        raise Phase2GateError(
            "deterministic calibration report differs from sealed runs"
        )
    _validate_git_release_tree(runtime_release)
    if (
        loaded["report"].get("runtime_source_commit")
        != expected_runtime_source_commit
    ):
        raise Phase2GateError(
            "deterministic calibration runtime source commit mismatch"
        )
    return loaded


def _validate_calibration_closure_mounts(mounts):
    if mounts is None:
        return
    if not isinstance(mounts, list) or not mounts:
        raise Phase2GateError(
            "deterministic calibration closure mounts are invalid"
        )
    from .taskpack import _digest_directory_tree

    for item in mounts:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "source_root",
                "snapshot_root",
                "tree_sha256",
            }
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                item.get("tree_sha256", ""),
            )
        ):
            raise Phase2GateError(
                "deterministic calibration closure mount is invalid"
            )
        snapshot_root = Path(item["snapshot_root"])
        source_root = Path(item["source_root"])
        if (
            snapshot_root.is_symlink()
            or not snapshot_root.is_dir()
            or source_root.is_symlink()
            or not source_root.is_dir()
            or _digest_directory_tree(snapshot_root)
            != item["tree_sha256"]
            or _digest_directory_tree(source_root)
            != item["tree_sha256"]
        ):
            raise Phase2GateError(
                "deterministic calibration closure drifted"
            )


def _validate_git_release_tree(runtime_release):
    release_root = Path(runtime_release["release_root"])
    manifest = _read_json(
        release_root / "manifest.json",
        "runtime release manifest",
    )
    source_repo = manifest.get("source_repo")
    source_commit = manifest.get("source_commit")
    if (
        manifest.get("install_method") != "git_ref"
        or not isinstance(source_repo, str)
        or not source_repo
        or source_commit != runtime_release["source_commit"]
    ):
        raise Phase2GateError(
            "deterministic calibration requires a Git-installed release"
        )
    source_repo_path = Path(source_repo).expanduser()
    if (
        _path_contains_symlink(source_repo_path)
        or not source_repo_path.resolve().is_dir()
    ):
        raise Phase2GateError(
            "runtime release source repository is unavailable"
        )
    environment = dict(os.environ)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(source_repo_path.resolve()),
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            source_commit,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise Phase2GateError(
            "runtime release source commit is unavailable"
        )
    expected = {}
    for encoded in completed.stdout.split(b"\0"):
        if not encoded:
            continue
        try:
            metadata, raw_path = encoded.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode(
                "ascii"
            ).split()
            relative_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise Phase2GateError(
                "runtime release Git tree inventory is invalid"
            ) from exc
        if object_type == "commit" and mode == "160000":
            continue
        if (
            object_type != "blob"
            or mode not in {"100644", "100755", "120000"}
            or relative_path == "manifest.json"
        ):
            raise Phase2GateError(
                "runtime release Git tree contains unsupported entries"
            )
        expected[relative_path] = (mode, object_id)

    actual_paths = {}
    for path in release_root.rglob("*"):
        relative = path.relative_to(release_root)
        if (
            relative.as_posix() == "manifest.json"
            or (path.is_dir() and not path.is_symlink())
        ):
            continue
        actual_paths[relative.as_posix()] = path
    if set(actual_paths) != set(expected):
        raise Phase2GateError(
            "runtime release file inventory differs from source commit"
        )

    hash_name = runtime_release["git_object_format"]
    for relative_path, (expected_mode, expected_oid) in expected.items():
        path = actual_paths[relative_path]
        if expected_mode == "120000":
            if not path.is_symlink():
                raise Phase2GateError(
                    "runtime release file mode differs from source commit"
                )
            payload = os.readlink(os.fsencode(path))
            actual_mode = "120000"
        else:
            if path.is_symlink() or not path.is_file():
                raise Phase2GateError(
                    "runtime release file type differs from source commit"
                )
            payload = path.read_bytes()
            actual_mode = (
                "100755"
                if path.stat().st_mode & 0o111
                else "100644"
            )
        digest = hashlib.new(hash_name)
        digest.update(f"blob {len(payload)}\0".encode("ascii"))
        digest.update(payload)
        if (
            actual_mode != expected_mode
            or digest.hexdigest() != expected_oid
        ):
            raise Phase2GateError(
                "runtime release content differs from source commit"
            )


def _require_canonical_json_digest(path, expected, label):
    value = _read_json(path, label)
    if canonical_json_sha256(value) != expected:
        raise Phase2GateError(f"{label} digest mismatch")


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha256_value(context, field):
    value = context.get(field)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase2GateError(f"{field} must be a SHA-256 digest")
    return value


def _positive_int(context, field):
    value = context.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise Phase2GateError(f"{field} must be a positive integer")
    return value


def _text(context, field):
    value = context.get(field)
    if not isinstance(value, str) or not value:
        raise Phase2GateError(f"{field} must be non-empty")
    return value


def _git_oid(repository, value):
    object_format = _git(repository, "rev-parse", "--show-object-format")
    expected = 40 if object_format == "sha1" else 64
    if (
        not isinstance(value, str)
        or len(value) != expected
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise Phase2GateError("integration head is not a canonical Git OID")
    resolved = _git(repository, "rev-parse", "--verify", f"{value}^{{commit}}")
    if resolved != value:
        raise Phase2GateError("integration head resolution changed")
    return value


def _git_parents(repository, commit):
    values = _git(repository, "rev-list", "--parents", "-n", "1", commit).split()
    if not values or values[0] != commit:
        raise Phase2GateError("commit parent relation is unavailable")
    return values[1:]


def _git_changed_paths(repository, parent, child):
    return sorted(
        item
        for item in _git(
            repository,
            "diff",
            "--name-only",
            f"{parent}..{child}",
        ).splitlines()
        if item
    )


def _git_bytes(repository, commit, path):
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise Phase2GateError("Git evidence path is unsafe")
    completed = subprocess.run(
        ["git", "-C", str(repository), "show", f"{commit}:{relative.as_posix()}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise Phase2GateError("Git evidence path is unavailable")
    return completed.stdout


def _git(repository, *arguments):
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise Phase2GateError(
            completed.stderr.strip() or "Phase 2 Git relation failed"
        )
    return completed.stdout.strip()

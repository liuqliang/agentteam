"""Deterministic calibration of the Phase 2 experiment harness."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

from .experiment_contract import (
    ExperimentContractError,
    allocate_experiment_run,
    canonical_json_bytes,
    canonical_json_sha256,
    publish_immutable_json,
    validate_experiment_protocol,
    validate_experiment_run_binding,
)
from .experiment_modes import _registered_invocation_usage
from .experiment_results import (
    load_experiment_result_bundle,
    render_experiment_comparison,
)
from .experiment_sandbox import (
    _load_historical_model_invocation_set_reference,
    _load_historical_provider_sandbox_reference,
    _validate_historical_evaluation_evidence,
)
from .experiment_workspace import load_clean_snapshot_attestation
from .projection_db import (
    build_project_stats,
    check_project_projection_db,
    read_projected_experiment_results,
    rebuild_project_projection_db,
)


CALIBRATION_SCHEMA_VERSION = "phase2_deterministic_calibration.v1"
CALIBRATION_CLAIM_SCOPE = (
    "experiment_harness_readiness_only_not_benchmark_evidence"
)
_MODES = (
    "single_codex",
    "agentteam_direct",
    "agentteam_full",
)
_CONTROLLED_FAILURE_STATUSES = {
    "failed",
    "infrastructure_failed",
    "interrupted",
}
_PROJECTION_IDENTITY_FIELDS = (
    "runs",
    "invocations",
    "invocation_digest",
    "experiment_results",
    "experiment_result_digest",
    "experiment_recovery",
    "experiment_recovery_digest",
)


class ExperimentCalibrationError(RuntimeError):
    """Deterministic calibration evidence is incomplete or inconsistent."""


def run_deterministic_experiment_calibration(
    *,
    protocol,
    projection_root,
    primary_runs,
    repeat_run,
    controlled_runs,
    duplicate_request_evidence,
    fixture_roots,
    output_path=None,
):
    """Recompute the bounded Phase 2 calibration and optionally publish it.

    Every run record must contain ``run_dir`` and ``sandbox_authority_root``.
    The run itself remains authority for the sealed result, clean-snapshot
    attestation, invocation manifest, and evaluation evidence.
    """

    protocol = copy.deepcopy(protocol)
    validate_experiment_protocol(protocol)
    protocol_sha256 = canonical_json_sha256(protocol)
    projection_root = _existing_directory(
        projection_root,
        "calibration projection root",
    )

    primary = [
        _load_run_record(
            record,
            protocol,
            protocol_sha256,
            projection_root,
        )
        for record in primary_runs
    ]
    repeat = _load_run_record(
        repeat_run,
        protocol,
        protocol_sha256,
        projection_root,
    )
    controlled = [
        _load_run_record(
            record,
            protocol,
            protocol_sha256,
            projection_root,
        )
        for record in controlled_runs
    ]
    _validate_mode_family(primary, repeat)

    all_runs = [*primary, repeat, *controlled]
    _validate_unique_results(all_runs)
    equal_input = _validate_equal_contract(
        primary + [repeat],
        protocol,
    )
    coverage = _validate_usage_coverage(all_runs)
    isolation = _validate_isolation(all_runs)
    outcomes = _validate_controlled_outcomes(controlled)
    idempotency = _validate_duplicate_request(
        duplicate_request_evidence,
        all_runs,
        protocol,
        projection_root,
    )
    fixtures = _fixture_evidence(fixture_roots, protocol)
    ledger = _operator_ledger_evidence(all_runs)
    projection = _rebuild_projection(
        projection_root,
        all_runs,
        coverage,
    )
    comparison = render_experiment_comparison(
        [item["sealed"] for item in all_runs]
    )
    if any(
        claim in comparison.lower()
        for claim in ("better than", "best mode", "superior")
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration made a superiority claim"
        )

    report = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "calibration_status": "passed",
        "claim_scope": CALIBRATION_CLAIM_SCOPE,
        "protocol_sha256": protocol_sha256,
        "source_commit": protocol["repository"]["commit"],
        "fixture_evidence": fixtures,
        "equal_input_evidence": equal_input,
        "mode_results": [
            _result_summary(item)
            for item in sorted(
                primary,
                key=lambda item: _MODES.index(item["bundle"]["mode"]),
            )
        ],
        "repeat_result": _result_summary(repeat),
        "repeat_drift": _repeat_drift_evidence(primary, repeat),
        "controlled_outcomes": outcomes,
        "duplicate_request": idempotency,
        "usage_coverage": coverage,
        "isolation": isolation,
        "operator_ledger": ledger,
        "projection_rebuild": projection,
        "comparison": {
            "status": "complete",
            "result_count": len(all_runs),
            "sha256": hashlib.sha256(
                comparison.encode("utf-8")
            ).hexdigest(),
            "retained_terminal_statuses": sorted(
                {
                    item["bundle"]["terminal_status"]
                    for item in all_runs
                }
            ),
        },
        "readiness_promotion_candidate": {
            "status": "eligible",
            "live_provider_calls_required": 0,
            "benchmark_superiority_claim": False,
        },
    }
    validate_deterministic_calibration_report(report)
    published = None
    if output_path is not None:
        try:
            published = publish_immutable_json(
                output_path,
                report,
                label="deterministic experiment calibration",
            )
        except ExperimentContractError as exc:
            raise ExperimentCalibrationError(str(exc)) from exc
    return {
        "report": report,
        "report_sha256": canonical_json_sha256(report),
        "publication": published,
        "comparison": comparison,
    }


def validate_deterministic_calibration_report(report):
    if not isinstance(report, dict):
        raise ExperimentCalibrationError(
            "deterministic calibration report must be an object"
        )
    if report.get("schema_version") != CALIBRATION_SCHEMA_VERSION:
        raise ExperimentCalibrationError(
            "deterministic calibration schema version is invalid"
        )
    if report.get("calibration_status") != "passed":
        raise ExperimentCalibrationError(
            "deterministic calibration did not pass"
        )
    if report.get("claim_scope") != CALIBRATION_CLAIM_SCOPE:
        raise ExperimentCalibrationError(
            "deterministic calibration claim scope is invalid"
        )
    if set(report) != {
        "schema_version",
        "calibration_status",
        "claim_scope",
        "protocol_sha256",
        "source_commit",
        "fixture_evidence",
        "equal_input_evidence",
        "mode_results",
        "repeat_result",
        "repeat_drift",
        "controlled_outcomes",
        "duplicate_request",
        "usage_coverage",
        "isolation",
        "operator_ledger",
        "projection_rebuild",
        "comparison",
        "readiness_promotion_candidate",
    }:
        raise ExperimentCalibrationError(
            "deterministic calibration report fields are invalid"
        )
    if not _is_sha256(report.get("protocol_sha256")):
        raise ExperimentCalibrationError(
            "deterministic calibration protocol digest is invalid"
        )
    fixtures = report.get("fixture_evidence")
    if (
        not isinstance(fixtures, dict)
        or set(fixtures)
        != {"deterministic_l1", "bounded_l2"}
        or any(
            not isinstance(item, dict)
            or not _is_sha256(item.get("fixture_sha256"))
            or not isinstance(item.get("file_count"), int)
            or item["file_count"] <= 0
            for item in fixtures.values()
        )
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration fixture evidence is invalid"
        )
    equal_input = report.get("equal_input_evidence")
    if (
        not isinstance(equal_input, dict)
        or equal_input.get("status") != "passed"
        or equal_input.get("protocol_family")
        != "phase2_three_mode_equal_input"
        or equal_input.get("modes") != list(_MODES)
        or equal_input.get("protocol_sha256")
        != report["protocol_sha256"]
        or equal_input.get("source_commit")
        != report["source_commit"]
        or not _is_sha256(
            equal_input.get("environment_contract_sha256")
        )
        or not _is_sha256(
            equal_input.get("runtime_release_identity_sha256")
        )
        or not _is_sha256(
            equal_input.get("common_evaluation_inputs_sha256")
        )
        or equal_input.get("repetition_count") != 4
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration equal-input evidence is invalid"
        )
    modes = [
        item.get("mode")
        for item in report.get("mode_results", [])
        if isinstance(item, dict)
    ]
    if (
        modes != list(_MODES)
        or any(
            not _is_sha256(item.get("bundle_sha256"))
            for item in report["mode_results"]
        )
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration mode inventory is invalid"
        )
    repeat = report.get("repeat_result")
    drift = report.get("repeat_drift")
    if (
        not isinstance(repeat, dict)
        or repeat.get("mode") not in _MODES
        or not _is_sha256(repeat.get("bundle_sha256"))
        or not isinstance(drift, dict)
        or drift.get("status") != "complete"
        or drift.get("mode") != repeat["mode"]
        or drift.get("acceptance_status_equal") is not True
        or drift.get("changed_files_equal") is not True
        or not _is_sha256(drift.get("baseline_bundle_sha256"))
        or not _is_sha256(drift.get("repeat_bundle_sha256"))
        or drift["baseline_bundle_sha256"]
        == drift["repeat_bundle_sha256"]
        or drift.get("superiority_interpretation") is not False
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration repeat evidence is invalid"
        )
    baseline_mode_result = next(
        item
        for item in report["mode_results"]
        if item["mode"] == repeat["mode"]
    )
    if (
        drift["baseline_bundle_sha256"]
        != baseline_mode_result["bundle_sha256"]
        or drift["repeat_bundle_sha256"]
        != repeat["bundle_sha256"]
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration repeat bundle binding is invalid"
        )
    controlled = report.get("controlled_outcomes")
    controlled_failure_run_ids = (
        controlled.get("controlled_failure_run_ids")
        if isinstance(controlled, dict)
        else None
    )
    budget_stopped_run_ids = (
        controlled.get("budget_stopped_run_ids")
        if isinstance(controlled, dict)
        else None
    )
    controlled_bundles = (
        controlled.get("retained_result_bundles")
        if isinstance(controlled, dict)
        else None
    )
    if (
        not isinstance(controlled, dict)
        or controlled.get("retention_status") != "passed"
        or not isinstance(controlled_failure_run_ids, list)
        or not controlled_failure_run_ids
        or any(
            not isinstance(value, str)
            for value in controlled_failure_run_ids
        )
        or not isinstance(budget_stopped_run_ids, list)
        or not budget_stopped_run_ids
        or any(
            not isinstance(value, str)
            for value in budget_stopped_run_ids
        )
        or not isinstance(controlled_bundles, list)
        or len(controlled_bundles)
        != (
            len(controlled_failure_run_ids)
            + len(budget_stopped_run_ids)
        )
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("experiment_run_id"), str)
            or item.get("terminal_status")
            not in _CONTROLLED_FAILURE_STATUSES
            | {"budget_stopped"}
            or not _is_sha256(item.get("bundle_sha256"))
            for item in controlled_bundles
        )
        or {
            item.get("experiment_run_id")
            for item in controlled_bundles
            if item.get("terminal_status") == "budget_stopped"
        }
        != set(budget_stopped_run_ids)
        or {
            item.get("experiment_run_id")
            for item in controlled_bundles
            if item.get("terminal_status")
            in _CONTROLLED_FAILURE_STATUSES
        }
        != set(controlled_failure_run_ids)
        or not isinstance(
            controlled.get("terminal_statuses"),
            list,
        )
        or any(
            not isinstance(value, str)
            for value in controlled.get("terminal_statuses", [])
        )
        or set(controlled.get("terminal_statuses", []))
        != _CONTROLLED_FAILURE_STATUSES | {"budget_stopped"}
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration controlled outcomes are invalid"
        )
    duplicate = report.get("duplicate_request")
    if (
        not isinstance(duplicate, dict)
        or duplicate.get("status") != "passed"
        or duplicate.get("allocation_status") != "existing"
        or duplicate.get(
            "provider_calls_during_duplicate_allocation"
        )
        != 0
        or duplicate.get("replay_status") != "passed"
        or not isinstance(
            duplicate.get("stable_request_key"),
            str,
        )
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]*",
            duplicate.get("stable_request_key", ""),
        )
        is None
        or not _is_sha256(
            duplicate.get("result_bundle_sha256")
        )
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration idempotency evidence is invalid"
        )
    coverage = report.get("usage_coverage")
    if (
        not isinstance(coverage, dict)
        or coverage.get("lifecycle_percent") != 100
        or coverage.get("token_percent") != 100
        or coverage.get("cached_input_distinct") is not True
        or not isinstance(coverage.get("covered_invocations"), int)
        or isinstance(coverage.get("covered_invocations"), bool)
        or not isinstance(coverage.get("total_invocations"), int)
        or isinstance(coverage.get("total_invocations"), bool)
        or coverage.get("covered_invocations", 0) <= 0
        or coverage.get("covered_invocations")
        != coverage.get("total_invocations")
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration usage coverage is incomplete"
        )
    isolation = report.get("isolation")
    sealed_bundle_sha256s = (
        isolation.get("sealed_bundle_sha256s")
        if isinstance(isolation, dict)
        else None
    )
    cleanup_receipt_sha256s = (
        isolation.get("cleanup_receipt_sha256s")
        if isinstance(isolation, dict)
        else None
    )
    if (
        not isinstance(isolation, dict)
        or isolation.get("clean_snapshot_status") != "passed"
        or isolation.get("canary_denial_status") != "passed"
        or isolation.get("retained_leak_scan_status") != "passed"
        or isolation.get("cleanup_receipt_status") != "passed"
        or isolation.get("sealed_result_preservation_status")
        != "passed"
        or not isinstance(
            isolation.get("sealed_result_count"),
            int,
        )
        or isinstance(isolation.get("sealed_result_count"), bool)
        or not isinstance(
            isolation.get("cleanup_receipt_count"),
            int,
        )
        or isinstance(isolation.get("cleanup_receipt_count"), bool)
        or isolation.get("sealed_result_count")
        != isolation.get("cleanup_receipt_count")
        or isolation.get("sealed_result_count", 0) <= 0
        or not isinstance(sealed_bundle_sha256s, list)
        or len(sealed_bundle_sha256s)
        != isolation.get("sealed_result_count")
        or not isinstance(cleanup_receipt_sha256s, list)
        or len(cleanup_receipt_sha256s)
        != isolation.get("cleanup_receipt_count")
        or len(set(cleanup_receipt_sha256s))
        != isolation.get("cleanup_receipt_count")
        or any(
            not _is_sha256(value)
            for value in sealed_bundle_sha256s
        )
        or any(
            not _is_sha256(value)
            for value in cleanup_receipt_sha256s
        )
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration isolation is incomplete"
        )
    ledger = report.get("operator_ledger")
    ledger_counts = (
        ledger.get("operator_action_counts")
        if isinstance(ledger, dict)
        else None
    )
    if (
        not isinstance(ledger_counts, dict)
        or ledger.get("status") != "passed"
        or ledger_counts.get("expected_operator_action", 0) <= 0
        or ledger_counts.get("corrective_intervention", 0) <= 0
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration operator ledger is incomplete"
        )
    projection = report.get("projection_rebuild")
    first_identity = (
        projection.get("first_identity")
        if isinstance(projection, dict)
        else None
    )
    second_identity = (
        projection.get("second_identity")
        if isinstance(projection, dict)
        else None
    )
    projected_bundles = (
        projection.get("retained_result_bundles")
        if isinstance(projection, dict)
        else None
    )
    if (
        not isinstance(projection, dict)
        or projection.get("status") != "passed"
        or projection.get("projection_source") != "db"
        or projection.get("identity_fields_equal") is not True
        or not isinstance(first_identity, dict)
        or not isinstance(second_identity, dict)
        or first_identity != second_identity
        or set(first_identity)
        != set(_PROJECTION_IDENTITY_FIELDS)
        or first_identity.get("experiment_results")
        != projection.get("result_count")
        or first_identity.get("invocations")
        != coverage.get("total_invocations")
        or not isinstance(projection.get("result_count"), int)
        or isinstance(projection.get("result_count"), bool)
        or not isinstance(
            projection.get("terminal_statuses"),
            list,
        )
        or any(
            not isinstance(value, str)
            for value in projection.get("terminal_statuses", [])
        )
        or set(projection.get("terminal_statuses", []))
        != (
            _CONTROLLED_FAILURE_STATUSES
            | {"budget_stopped", "completed"}
        )
        or not isinstance(projected_bundles, list)
        or len(projected_bundles)
        != projection.get("result_count")
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("experiment_run_id"), str)
            or not _is_sha256(item.get("bundle_sha256"))
            or item.get("terminal_status")
            not in (
                _CONTROLLED_FAILURE_STATUSES
                | {"budget_stopped", "completed"}
            )
            for item in projected_bundles
        )
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration projection rebuild failed"
        )
    calibration_bundle_sha256s = [
        item["bundle_sha256"] for item in report["mode_results"]
    ]
    calibration_bundle_sha256s.append(repeat["bundle_sha256"])
    calibration_bundle_sha256s.extend(
        item["bundle_sha256"]
        for item in controlled_bundles
    )
    projected_bundle_sha256s = [
        item["bundle_sha256"]
        for item in projected_bundles
    ]
    if (
        len(set(calibration_bundle_sha256s))
        != len(calibration_bundle_sha256s)
        or sorted(calibration_bundle_sha256s)
        != sorted(sealed_bundle_sha256s)
        or sorted(calibration_bundle_sha256s)
        != sorted(projected_bundle_sha256s)
        or duplicate["result_bundle_sha256"]
        not in calibration_bundle_sha256s
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration bundle retention is inconsistent"
        )
    promotion = report.get("readiness_promotion_candidate")
    if (
        not isinstance(promotion, dict)
        or promotion.get("status") != "eligible"
        or promotion.get("live_provider_calls_required") != 0
        or promotion.get("benchmark_superiority_claim") is not False
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration promotion boundary is invalid"
        )
    comparison = report.get("comparison")
    retained = (
        set(comparison.get("retained_terminal_statuses", []))
        if isinstance(comparison, dict)
        else set()
    )
    if (
        not isinstance(comparison, dict)
        or comparison.get("status") != "complete"
        or not _is_sha256(comparison.get("sha256"))
        or not (
            _CONTROLLED_FAILURE_STATUSES
            | {"budget_stopped", "completed"}
        ).issubset(retained)
    ):
        raise ExperimentCalibrationError(
            "deterministic calibration comparison evidence is invalid"
        )
    return copy.deepcopy(report)


def load_deterministic_calibration_report(path):
    path = _regular_file(path, "deterministic calibration report")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentCalibrationError(
            "deterministic calibration report is unreadable"
        ) from exc
    validate_deterministic_calibration_report(report)
    expected = canonical_json_sha256(report)
    if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(
        _canonical_payload(report)
    ).hexdigest():
        raise ExperimentCalibrationError(
            "deterministic calibration report is not canonical"
        )
    return {"report": report, "report_sha256": expected, "path": str(path)}


def _load_run_record(record, protocol, protocol_sha256, projection_root):
    if not isinstance(record, dict):
        raise ExperimentCalibrationError(
            "calibration run record must be an object"
        )
    run_dir = _existing_directory(
        record.get("run_dir"),
        "calibration run directory",
    )
    runs_root = projection_root / "runs"
    try:
        run_dir.relative_to(runs_root)
    except ValueError as exc:
        raise ExperimentCalibrationError(
            "calibration run is outside the projection runs root"
        ) from exc
    sealed = load_experiment_result_bundle(run_dir)
    bundle = sealed["bundle"]
    if bundle["protocol_sha256"] != protocol_sha256:
        raise ExperimentCalibrationError(
            "calibration result belongs to another protocol"
        )
    if bundle["source_commit"] != protocol["repository"]["commit"]:
        raise ExperimentCalibrationError(
            "calibration result source commit differs"
        )
    attestation = load_clean_snapshot_attestation(
        run_dir / "clean-snapshot.json"
    )
    cleanup_reference = sealed.get("cleanup_receipt")
    if (
        not isinstance(cleanup_reference, dict)
        or not isinstance(cleanup_reference.get("receipt"), dict)
        or sealed.get("cleanup_status")
        not in {"removed", "already_absent"}
    ):
        raise ExperimentCalibrationError(
            "calibration run lacks complete cleanup receipt evidence"
        )
    cleanup_receipt = cleanup_reference["receipt"]
    if attestation["repository"] != protocol["repository"]:
        raise ExperimentCalibrationError(
            "clean snapshot attestation repository differs"
        )
    if (
        not attestation["worktree_clean"]
        or not attestation["common_dirs_distinct"]
        or attestation["path_preexisted"]
        or attestation["prior_run_state_detected"]
        or attestation["remotes"]
        or attestation["alternates"]
        or attestation["extra_refs"]
    ):
        raise ExperimentCalibrationError(
            "clean snapshot attestation is not isolated"
        )

    authority_root = _existing_directory(
        record.get("sandbox_authority_root"),
        "sandbox authority root",
    )
    invocation_reference = _discover_authority_reference(
        authority_root,
        bundle["result_evidence"][
            "invocation_set_reference_sha256"
        ],
        suffix=".invocation-set.json",
        schema_version=(
            "experiment_model_invocation_set_reference.v1"
        ),
    )
    invocation_manifest = _load_historical_model_invocation_set_reference(
        invocation_reference,
        authority_root,
        sealed_result=sealed,
        cleanup_receipt=cleanup_receipt,
        clean_snapshot_attestation=attestation,
    )
    recomputed_usage, recomputed_coverage, _statuses = (
        _registered_invocation_usage(
            invocation_reference,
            authority_root,
            invocation_manifest=invocation_manifest,
        )
    )
    if (
        recomputed_usage != bundle["usage_totals"]
        or recomputed_coverage != bundle["usage_coverage"]
    ):
        raise ExperimentCalibrationError(
            "sealed usage differs from invocation terminal authority"
        )
    sandbox_digests = []
    invocation_ids = []
    for invocation_set in invocation_manifest["invocation_sets"]:
        reference = invocation_set["sandbox_reference"]
        descriptor = _load_historical_provider_sandbox_reference(
            reference,
            authority_root,
            sealed_result=sealed,
            cleanup_receipt=cleanup_receipt,
            clean_snapshot_attestation=attestation,
        )
        evidence = descriptor["namespace_evidence"]
        if (
            evidence["evidence_status"] != "complete"
            or evidence["denial_status"] != "denied"
            or evidence["path_visible"]
            or evidence["content_readable"]
        ):
            raise ExperimentCalibrationError(
                "provider sandbox did not deny the gold canary"
            )
        sandbox_digests.append(reference["sha256"])
        invocation_ids.extend(invocation_set["invocation_ids"])
    if (
        len(invocation_ids)
        != bundle["usage_coverage"]["total_invocations"]
        or len(set(invocation_ids)) != len(invocation_ids)
    ):
        raise ExperimentCalibrationError(
            "calibration invocation manifest differs from sealed result"
        )

    evaluation_path = run_dir / bundle["result_evidence"][
        "evaluation_relative_path"
    ]
    evaluation_path = _regular_file(
        evaluation_path,
        "calibration evaluation evidence",
    )
    evaluation_payload = evaluation_path.read_bytes()
    evaluation_sha256 = hashlib.sha256(evaluation_payload).hexdigest()
    if (
        evaluation_sha256
        != bundle["result_evidence"]["evaluation_sha256"]
        or evaluation_sha256
        != bundle["acceptance_result"]["evaluation_sha256"]
    ):
        raise ExperimentCalibrationError(
            "calibration evaluation digest differs from sealed result"
        )
    try:
        evaluation = json.loads(evaluation_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentCalibrationError(
            "calibration evaluation evidence is invalid"
        ) from exc
    protocol_reference = _discover_authority_reference(
        authority_root,
        bundle["result_evidence"]["protocol_reference_sha256"],
        suffix=".protocol.json",
        schema_version="experiment_protocol_reference.v1",
    )
    candidate_sandbox_reference = _discover_authority_reference(
        authority_root,
        bundle["result_evidence"][
            "provider_sandbox_reference_sha256"
        ],
        suffix=".sandbox.json",
        schema_version="experiment_provider_sandbox_reference.v1",
    )
    canary_path = _regular_file(
        record.get("canary_path"),
        "calibration gold canary",
    )
    try:
        _validate_historical_evaluation_evidence(
            evaluation,
            sealed_result=sealed,
            cleanup_receipt=cleanup_receipt,
            clean_snapshot_attestation=attestation,
            expected_run_id=bundle["experiment_run_id"],
            expected_taskpack_ids=bundle["result_evidence"][
                "taskpack_ids"
            ],
            expected_protocol_sha256=protocol_sha256,
            expected_protocol_reference_sha256=(
                protocol_reference["sha256"]
            ),
            expected_acceptance_command_sha256=bundle[
                "result_evidence"
            ]["acceptance_command_sha256"],
            expected_acceptance_executable_sha256=bundle[
                "result_evidence"
            ]["acceptance_executable_sha256"],
            expected_evaluator_sha256=bundle["result_evidence"][
                "evaluator_sha256"
            ],
            expected_invocation_set_reference_sha256=(
                invocation_reference["sha256"]
            ),
            expected_provider_sandbox_reference_sha256=(
                candidate_sandbox_reference["sha256"]
            ),
            authority_root=authority_root,
            invocation_set_reference=invocation_reference,
            experiment_protocol_reference=protocol_reference,
            provider_sandbox_reference=(
                candidate_sandbox_reference
            ),
            canary_path=canary_path,
        )
    except Exception as exc:
        raise ExperimentCalibrationError(
            "calibration evaluation authority is invalid"
        ) from exc
    for field in ("pre_run_leak_scan", "post_run_leak_scan"):
        if evaluation[field]["scan_status"] != "clean":
            raise ExperimentCalibrationError(
                "calibration retained artifacts failed canary leak scan"
            )
    return {
        "run_dir": run_dir,
        "sealed": sealed,
        "bundle": bundle,
        "attestation_sha256": hashlib.sha256(
            (run_dir / "clean-snapshot.json").read_bytes()
        ).hexdigest(),
        "sandbox_reference_sha256s": sorted(sandbox_digests),
        "evaluation_sha256": evaluation_sha256,
        "recomputed_usage": recomputed_usage,
        "recomputed_coverage": recomputed_coverage,
        "cleanup_status": sealed["cleanup_status"],
        "cleanup_receipt_sha256": cleanup_reference["sha256"],
    }


def _discover_authority_reference(
    authority_root,
    expected_sha256,
    *,
    suffix,
    schema_version,
):
    matches = []
    for path in sorted(authority_root.rglob(f"*{suffix}")):
        if path.is_symlink() or not path.is_file():
            raise ExperimentCalibrationError(
                "calibration reference authority is unsafe"
            )
        payload = path.read_bytes()
        if len(payload) > 4 * 1024 * 1024:
            raise ExperimentCalibrationError(
                "calibration reference authority is oversized"
            )
        if hashlib.sha256(payload).hexdigest() == expected_sha256:
            matches.append((path, payload))
    if not matches:
        raise ExperimentCalibrationError(
            "sealed reference authority is unavailable"
        )
    return {
        "schema_version": schema_version,
        "path": str(matches[0][0]),
        "sha256": expected_sha256,
    }


def _validate_mode_family(primary, repeat):
    if len(primary) != len(_MODES):
        raise ExperimentCalibrationError(
            "calibration requires exactly one primary result per mode"
        )
    by_mode = {item["bundle"]["mode"]: item for item in primary}
    if set(by_mode) != set(_MODES) or len(by_mode) != len(primary):
        raise ExperimentCalibrationError(
            "calibration primary mode inventory is incomplete"
        )
    if any(item["bundle"]["repetition_index"] != 0 for item in primary):
        raise ExperimentCalibrationError(
            "calibration primary modes must use repetition zero"
        )
    if any(
        item["bundle"]["terminal_status"] != "completed"
        or item["bundle"]["acceptance_result"]["status"] != "passed"
        for item in primary
    ):
        raise ExperimentCalibrationError(
            "calibration primary modes did not complete acceptance"
        )
    if (
        repeat["bundle"]["mode"] not in _MODES
        or repeat["bundle"]["repetition_index"] <= 0
        or repeat["bundle"]["terminal_status"] != "completed"
        or repeat["bundle"]["acceptance_result"]["status"] != "passed"
    ):
        raise ExperimentCalibrationError(
            "calibration repeat result is not a later mode repetition"
        )


def _validate_unique_results(runs):
    run_ids = [item["bundle"]["experiment_run_id"] for item in runs]
    digests = [item["sealed"]["bundle_sha256"] for item in runs]
    if len(set(run_ids)) != len(run_ids):
        raise ExperimentCalibrationError(
            "calibration counted an experiment run more than once"
        )
    if len(set(digests)) != len(digests):
        raise ExperimentCalibrationError(
            "calibration counted a result bundle more than once"
        )


def _validate_equal_contract(runs, protocol=None):
    first = runs[0]["bundle"]
    fields = (
        "protocol_sha256",
        "source_commit",
        "runtime_release_identity",
    )
    evidence_fields = (
        "protocol_reference_sha256",
        "acceptance_command_sha256",
        "acceptance_executable_sha256",
        "evaluator_sha256",
    )
    for item in runs[1:]:
        bundle = item["bundle"]
        if any(bundle[field] != first[field] for field in fields):
            raise ExperimentCalibrationError(
                "calibration modes do not share one immutable input contract"
            )
        if any(
            bundle["result_evidence"][field]
            != first["result_evidence"][field]
            for field in evidence_fields
        ):
            raise ExperimentCalibrationError(
                "calibration modes differ in common evaluation inputs"
            )
    common_evaluation_inputs = {
        field: first["result_evidence"][field]
        for field in evidence_fields
    }
    evidence = {
        "status": "passed",
        "protocol_family": "phase2_three_mode_equal_input",
        "modes": list(_MODES),
        "repetition_count": len(runs),
        "protocol_sha256": first["protocol_sha256"],
        "source_commit": first["source_commit"],
        "runtime_release_identity_sha256": canonical_json_sha256(
            first["runtime_release_identity"]
        ),
        "common_evaluation_inputs_sha256": canonical_json_sha256(
            common_evaluation_inputs
        ),
    }
    if protocol is not None:
        evidence["environment_contract_sha256"] = (
            canonical_json_sha256(protocol["environment"])
        )
    return evidence


def _validate_usage_coverage(runs):
    covered = 0
    total = 0
    aggregate = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    cached_observed = False
    for item in runs:
        bundle = item["bundle"]
        coverage = item["recomputed_coverage"]
        if (
            coverage["status"] != "complete"
            or coverage["covered_invocations"]
            != coverage["total_invocations"]
            or coverage["total_invocations"] <= 0
        ):
            raise ExperimentCalibrationError(
                "calibration contains incomplete invocation usage"
            )
        covered += coverage["covered_invocations"]
        total += coverage["total_invocations"]
        usage = item["recomputed_usage"]
        for field in aggregate:
            value = usage.get(field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ExperimentCalibrationError(
                    "calibration token totals are unavailable"
                )
            aggregate[field] += value
        if usage["cached_input_tokens"] > 0:
            cached_observed = True
        if usage["cached_input_tokens"] > usage["input_tokens"]:
            raise ExperimentCalibrationError(
                "cached input exceeds total input tokens"
            )
    if not cached_observed:
        raise ExperimentCalibrationError(
            "calibration did not preserve cached input separately"
        )
    return {
        "lifecycle_percent": 100 * covered // total,
        "token_percent": 100 * covered // total,
        "covered_invocations": covered,
        "total_invocations": total,
        "cached_input_distinct": True,
        "reported_token_totals": aggregate,
    }


def _validate_isolation(runs):
    sandbox_reference_count = sum(
        len(item["sandbox_reference_sha256s"]) for item in runs
    )
    return {
        "clean_snapshot_status": "passed",
        "clean_snapshot_count": len(runs),
        "cleanup_receipt_status": "passed",
        "cleanup_receipt_count": len(runs),
        "cleanup_receipt_sha256s": sorted(
            item["cleanup_receipt_sha256"] for item in runs
        ),
        "sealed_result_preservation_status": "passed",
        "sealed_result_count": len(runs),
        "sealed_bundle_sha256s": sorted(
            item["sealed"]["bundle_sha256"] for item in runs
        ),
        "canary_denial_status": "passed",
        "sandbox_reference_count": sandbox_reference_count,
        "retained_leak_scan_status": "passed",
        "evaluation_count": len(runs),
    }


def _validate_controlled_outcomes(controlled):
    failures_by_status = {
        status: [
            item
            for item in controlled
            if item["bundle"]["terminal_status"] == status
        ]
        for status in _CONTROLLED_FAILURE_STATUSES
    }
    missing_failures = sorted(
        status
        for status, items in failures_by_status.items()
        if not items
    )
    failures = [
        item
        for items in failures_by_status.values()
        for item in items
    ]
    budget_stops = []
    for item in controlled:
        bundle = item["bundle"]
        if bundle["terminal_status"] != "budget_stopped":
            continue
        budget = bundle["budget_result"]
        if (
            budget.get("status") != "budget_stopped"
            or budget.get("exhausted") is not True
            or budget.get("usage_complete") is not True
            or budget.get("total_tokens", -1)
            < budget.get("max_total_tokens", 0)
            or budget.get("overshoot_tokens", -1) < 0
        ):
            raise ExperimentCalibrationError(
                "budget stop is not backed by exhausted controller evidence"
            )
        budget_stops.append(item)
    if missing_failures or not budget_stops:
        raise ExperimentCalibrationError(
            "calibration must retain failed, infrastructure_failed, "
            "interrupted, and budget_stopped outcomes"
        )
    return {
        "controlled_failure_run_ids": sorted(
            item["bundle"]["experiment_run_id"] for item in failures
        ),
        "budget_stopped_run_ids": sorted(
            item["bundle"]["experiment_run_id"] for item in budget_stops
        ),
        "terminal_statuses": sorted(
            {
                item["bundle"]["terminal_status"]
                for item in [*failures, *budget_stops]
            }
        ),
        "retained_result_bundles": sorted(
            (
                {
                    "experiment_run_id": item["bundle"][
                        "experiment_run_id"
                    ],
                    "terminal_status": item["bundle"][
                        "terminal_status"
                    ],
                    "bundle_sha256": item["sealed"][
                        "bundle_sha256"
                    ],
                }
                for item in [*failures, *budget_stops]
            ),
            key=lambda item: item["experiment_run_id"],
        ),
        "retention_status": "passed",
    }


def _validate_duplicate_request(
    evidence,
    runs,
    protocol,
    projection_root,
):
    if not isinstance(evidence, dict):
        raise ExperimentCalibrationError(
            "duplicate request evidence must be an object"
        )
    stable_request_key = evidence.get("stable_request_key")
    experiment_run_id = evidence.get("experiment_run_id")
    matching = [
        item
        for item in runs
        if item["bundle"]["experiment_run_id"]
        == experiment_run_id
    ]
    if (
        not isinstance(stable_request_key, str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]*",
            stable_request_key,
        )
        is None
        or len(matching) != 1
    ):
        raise ExperimentCalibrationError(
            "duplicate request evidence is not bound to a result"
        )
    bundle = matching[0]["bundle"]
    request_path = (
        projection_root
        / "requests"
        / f"{stable_request_key}.json"
    )
    request_path = _regular_file(
        request_path,
        "duplicate request binding",
    )
    try:
        request_binding = json.loads(
            request_path.read_text(encoding="utf-8")
        )
        validate_experiment_run_binding(request_binding)
    except Exception as exc:
        raise ExperimentCalibrationError(
            "duplicate request binding is invalid"
        ) from exc
    if (
        request_path.read_bytes()
        != canonical_json_bytes(request_binding) + b"\n"
        or request_binding["experiment_run_id"]
        != experiment_run_id
    ):
        raise ExperimentCalibrationError(
            "duplicate request binding differs from sealed result"
        )
    try:
        duplicate = allocate_experiment_run(
            projection_root,
            protocol,
            mode=bundle["mode"],
            repetition_index=bundle["repetition_index"],
            stable_request_key=stable_request_key,
            runtime_release=bundle["runtime_release_identity"],
        )
    except ExperimentContractError as exc:
        raise ExperimentCalibrationError(
            "duplicate request replay was rejected"
        ) from exc
    if (
        duplicate.get("experiment_run_id") != experiment_run_id
        or duplicate.get("created") is not False
        or duplicate.get("allocation_status") != "existing"
        or duplicate.get("provider_calls") != 0
        or duplicate.get("target_mutations") != 0
        or evidence.get("result_bundle_sha256")
        != matching[0]["sealed"]["bundle_sha256"]
    ):
        raise ExperimentCalibrationError(
            "duplicate request consumed new provider work"
        )
    return {
        "status": "passed",
        "replay_status": "passed",
        "stable_request_key": stable_request_key,
        "experiment_run_id": experiment_run_id,
        "allocation_status": duplicate["allocation_status"],
        "provider_calls_during_duplicate_allocation": duplicate[
            "provider_calls"
        ],
        "result_bundle_sha256": evidence[
            "result_bundle_sha256"
        ],
    }


def _repeat_drift_evidence(primary, repeat):
    baseline = next(
        item
        for item in primary
        if item["bundle"]["mode"] == repeat["bundle"]["mode"]
    )
    baseline_bundle = baseline["bundle"]
    repeat_bundle = repeat["bundle"]
    return {
        "status": "complete",
        "mode": repeat_bundle["mode"],
        "baseline_repetition_index": baseline_bundle[
            "repetition_index"
        ],
        "repeat_repetition_index": repeat_bundle["repetition_index"],
        "baseline_bundle_sha256": baseline["sealed"][
            "bundle_sha256"
        ],
        "repeat_bundle_sha256": repeat["sealed"]["bundle_sha256"],
        "total_token_delta": (
            repeat_bundle["usage_totals"]["total_tokens"]
            - baseline_bundle["usage_totals"]["total_tokens"]
        ),
        "acceptance_status_equal": (
            repeat_bundle["acceptance_result"]["status"]
            == baseline_bundle["acceptance_result"]["status"]
        ),
        "changed_files_equal": (
            repeat_bundle["changed_files"]
            == baseline_bundle["changed_files"]
        ),
        "superiority_interpretation": False,
    }


def _fixture_evidence(fixture_roots, protocol):
    if not isinstance(fixture_roots, dict) or set(fixture_roots) != {
        "deterministic_l1",
        "bounded_l2",
    }:
        raise ExperimentCalibrationError(
            "calibration fixture inventory is incomplete"
        )
    evidence = {}
    for name, root in sorted(fixture_roots.items()):
        root = _existing_directory(root, f"{name} fixture")
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ExperimentCalibrationError(
                    f"{name} fixture contains a symlink"
                )
            if path.is_file():
                files.append(
                    {
                        "path": str(path.relative_to(root)),
                        "sha256": hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest(),
                    }
                )
        if not files:
            raise ExperimentCalibrationError(
                f"{name} fixture is empty"
            )
        for item in files:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    protocol["repository"]["source"],
                    "show",
                    (
                        f"{protocol['repository']['commit']}:"
                        f"{name}/{item['path']}"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if (
                completed.returncode != 0
                or hashlib.sha256(completed.stdout).hexdigest()
                != item["sha256"]
            ):
                raise ExperimentCalibrationError(
                    f"{name} fixture differs from the protocol commit"
                )
        evidence[name] = {
            "file_count": len(files),
            "fixture_sha256": canonical_json_sha256(files),
        }
    return evidence


def _operator_ledger_evidence(runs):
    counts = {
        "expected_operator_action": 0,
        "corrective_intervention": 0,
        "decision_escalation": 0,
    }
    for item in runs:
        for action_class in counts:
            counts[action_class] += item["bundle"][
                "operator_action_counts"
            ][action_class]
    if (
        counts["expected_operator_action"] <= 0
        or counts["corrective_intervention"] <= 0
    ):
        raise ExperimentCalibrationError(
            "calibration did not exercise the operator action ledger"
        )
    return {"status": "passed", "operator_action_counts": counts}


def _rebuild_projection(projection_root, runs, coverage):
    first = rebuild_project_projection_db(projection_root)
    first_check = check_project_projection_db(projection_root)
    stats = build_project_stats(projection_root)
    second = rebuild_project_projection_db(projection_root)
    second_check = check_project_projection_db(projection_root)
    if (
        first_check["check_status"] != "passed"
        or second_check["check_status"] != "passed"
    ):
        raise ExperimentCalibrationError(
            "calibration projection does not match file authority"
        )
    if any(first.get(field) != second.get(field) for field in _PROJECTION_IDENTITY_FIELDS):
        raise ExperimentCalibrationError(
            "calibration projection rebuild changed authoritative totals"
        )
    if (
        first["experiment_results"] != len(runs)
        or first["invocations"] != coverage["total_invocations"]
    ):
        raise ExperimentCalibrationError(
            "calibration projection counted results or invocations incorrectly"
        )
    invocation_usage = stats["model_invocation_usage"]
    lifecycle = invocation_usage["lifecycle_terminal_coverage"]
    token = invocation_usage["token_usage_coverage"]
    projected_totals = invocation_usage["reported_token_totals"]
    if (
        lifecycle.get("percent") != 100
        or token.get("percent") != 100
        or invocation_usage.get("open_invocations") != 0
        or any(
            projected_totals.get(field) != value
            for field, value in coverage[
                "reported_token_totals"
            ].items()
        )
        or projected_totals.get("contributing_invocation_count")
        != coverage["total_invocations"]
    ):
        raise ExperimentCalibrationError(
            "calibration projection usage differs from sealed results"
        )
    projected = read_projected_experiment_results(projection_root)
    expected_results = {
        item["bundle"]["experiment_run_id"]: {
            "experiment_run_id": item["bundle"][
                "experiment_run_id"
            ],
            "terminal_status": item["bundle"]["terminal_status"],
            "bundle_sha256": item["sealed"]["bundle_sha256"],
            "cleanup_status": item["cleanup_status"],
            "cleanup_receipt_sha256": item[
                "cleanup_receipt_sha256"
            ],
        }
        for item in runs
    }
    projected_results = {
        item["bundle"]["experiment_run_id"]: {
            "experiment_run_id": item["bundle"][
                "experiment_run_id"
            ],
            "terminal_status": item["bundle"]["terminal_status"],
            "bundle_sha256": item["bundle_sha256"],
            "cleanup_status": item["cleanup_status"],
            "cleanup_receipt_sha256": (
                item["cleanup_receipt"]["sha256"]
                if isinstance(item.get("cleanup_receipt"), dict)
                else None
            ),
        }
        for item in projected
    }
    if (
        any(item.get("projection_source") != "db" for item in projected)
        or projected_results != expected_results
    ):
        raise ExperimentCalibrationError(
            "calibration projection did not retain sealed terminal results"
        )
    first_identity = {
        field: first.get(field)
        for field in _PROJECTION_IDENTITY_FIELDS
    }
    second_identity = {
        field: second.get(field)
        for field in _PROJECTION_IDENTITY_FIELDS
    }
    return {
        "status": "passed",
        "schema_version": first["schema_version"],
        "projection_source": "db",
        "identity_fields_equal": True,
        "first_identity": first_identity,
        "second_identity": second_identity,
        "result_count": first["experiment_results"],
        "result_digest": first["experiment_result_digest"],
        "invocation_count": first["invocations"],
        "invocation_digest": first["invocation_digest"],
        "terminal_statuses": sorted(
            {
                item["terminal_status"]
                for item in projected_results.values()
            }
        ),
        "retained_result_bundles": sorted(
            projected_results.values(),
            key=lambda item: item["experiment_run_id"],
        ),
    }


def _result_summary(item):
    bundle = item["bundle"]
    return {
        "mode": bundle["mode"],
        "repetition_index": bundle["repetition_index"],
        "experiment_run_id": bundle["experiment_run_id"],
        "terminal_status": bundle["terminal_status"],
        "acceptance_status": bundle["acceptance_result"]["status"],
        "total_tokens": bundle["usage_totals"]["total_tokens"],
        "bundle_sha256": item["sealed"]["bundle_sha256"],
    }


def _existing_directory(path, label):
    try:
        path = Path(path)
        if path.is_symlink():
            raise ExperimentCalibrationError(f"{label} is unsafe")
        path = path.resolve(strict=True)
    except (TypeError, OSError) as exc:
        raise ExperimentCalibrationError(f"{label} is unavailable") from exc
    if path.is_symlink() or not path.is_dir():
        raise ExperimentCalibrationError(f"{label} is unsafe")
    return path


def _regular_file(path, label):
    try:
        path = Path(path)
        if path.is_symlink():
            raise ExperimentCalibrationError(f"{label} is unsafe")
        path = path.resolve(strict=True)
    except (TypeError, OSError) as exc:
        raise ExperimentCalibrationError(f"{label} is unavailable") from exc
    if path.is_symlink() or not path.is_file():
        raise ExperimentCalibrationError(f"{label} is unsafe")
    return path


def _canonical_payload(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )

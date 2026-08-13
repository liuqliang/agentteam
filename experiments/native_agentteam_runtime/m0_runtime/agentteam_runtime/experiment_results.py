"""Immutable experiment results and versioned recovery snapshots."""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import uuid
from datetime import UTC, datetime
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    schema_path,
    validate_experiment_protocol,
    validate_experiment_run_manifest,
    validate_resume_binding,
)
from .experiment_controller import (
    ExperimentControllerError,
    load_experiment_controller,
    validate_experiment_controller_reference,
)
from .experiment_sandbox import (
    load_scan_scope_reference,
    publish_scan_scope_reference,
    validate_evaluation_evidence,
)
from .experiment_workspace import (
    ATTESTATION_FILE_NAME,
    CLEANUP_RECEIPT_FILE_NAME,
    ExperimentWorkspaceError,
    load_clean_snapshot_attestation,
    load_clean_snapshot_cleanup_receipt,
)


RESULT_SCHEMA_VERSION = "experiment_result_bundle.v1"
RECOVERY_SCHEMA_VERSION = "experiment_recovery_snapshot.v1"
PROJECTION_EXPECTATION_SCHEMA_VERSION = (
    "experiment_projection_expectation.v1"
)
_RESULT_FILE = "result.json"
_RESULT_DIGEST_FILE = "result.sha256"
_SNAPSHOT_FILE = "snapshot.json"
_SNAPSHOT_DIGEST_FILE = "snapshot.sha256"
_MAX_AUTHORITY_BYTES = 16 * 1024 * 1024
_MAX_ARTIFACT_FILES = 100_000
_MAX_ARTIFACT_BYTES = 1 << 40
_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
_EXCLUDED_FILE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
}
_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "infrastructure_failed",
    "interrupted",
    "budget_stopped",
}


class ExperimentResultError(RuntimeError):
    """Base error for experiment result publication."""


class ExperimentResultIntegrityError(ExperimentResultError):
    """Raised when immutable result authority is inconsistent."""


class ExperimentResultConflict(ExperimentResultIntegrityError):
    """Raised when terminal authority already contains another result."""


def publish_result_scan_scope(
    authority_root,
    retained_roots,
    *,
    reference_id="experiment-result-scan-scope",
):
    """Publish the complete retained prompt/context/taskpack/artifact roots."""
    normalized = _normalize_retained_roots(retained_roots)
    return publish_scan_scope_reference(
        authority_root,
        normalized,
        reference_id=reference_id,
    )


def measure_experiment_artifacts(
    run_dir,
    *,
    artifact_roots,
    raw_spool_roots=(),
):
    """Measure authoritative run artifacts without Git/cache/DB/raw spools."""
    run_dir = _existing_directory(run_dir, "experiment run directory")
    spool_roots = _normalize_descendant_roots(
        run_dir,
        raw_spool_roots,
        "raw spool root",
    )
    artifact_roots = _normalize_descendant_roots(
        run_dir,
        artifact_roots,
        "authoritative artifact root",
    )
    artifact_bytes = 0
    artifact_files = 0
    spool_files = {
        path.resolve()
        for root in spool_roots
        for path in _bounded_files(root, prune_excluded=False)
    }
    raw_spool_bytes = sum(
        path.stat(follow_symlinks=False).st_size for path in spool_files
    )
    raw_spool_files = len(spool_files)
    seen = set()
    for path in (
        path
        for root in artifact_roots
        for path in _bounded_files(
            root,
            prune_excluded=True,
            excluded_roots=spool_roots,
        )
    ):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        size = path.stat(follow_symlinks=False).st_size
        if _under_any(path, spool_roots):
            continue
        relative = path.relative_to(run_dir)
        if (
            any(part in _EXCLUDED_DIRECTORY_NAMES for part in relative.parts)
            or path.suffix.lower() in _EXCLUDED_FILE_SUFFIXES
            or path.name.endswith(("-wal", "-shm", "-journal"))
            or path.name.endswith(".lock")
            or relative.parts[:2] == ("results", "terminal")
            or relative.parts
            and relative.parts[0].startswith(".result-staging-")
        ):
            continue
        artifact_bytes += size
        artifact_files += 1
        if artifact_bytes > _MAX_ARTIFACT_BYTES:
            raise ExperimentResultError(
                "authoritative artifact bytes exceed bounded limit"
            )
    if raw_spool_bytes > _MAX_ARTIFACT_BYTES:
        raise ExperimentResultError("raw spool bytes exceed bounded limit")
    return {
        "artifact_bytes_written": artifact_bytes,
        "artifact_file_count": artifact_files,
        "raw_spool_bytes_written": raw_spool_bytes,
        "raw_spool_file_count": raw_spool_files,
    }


def build_experiment_result_bundle(
    *,
    protocol,
    run_manifest,
    runtime_release_identity,
    started_at,
    finished_at,
    terminal_status,
    acceptance_result,
    usage_totals,
    usage_coverage,
    budget_result,
    attempt_counts,
    verified_milestones,
    operator_action_counts,
    retry_and_repair_counts,
    changed_files,
    regressions,
    artifact_bytes_written,
    raw_spool_bytes_written,
    workspace_diff_sha256,
    result_evidence,
    cleanup_status,
):
    protocol = copy.deepcopy(protocol)
    run_manifest = copy.deepcopy(run_manifest)
    validate_experiment_protocol(protocol)
    validate_experiment_run_manifest(run_manifest, protocol)
    if terminal_status not in _TERMINAL_STATUSES:
        raise ExperimentResultError(
            f"unsupported terminal status: {terminal_status!r}"
        )
    projection_identity = _projection_identity(
        {
            "experiment_run_id": run_manifest["experiment_run_id"],
            "protocol_sha256": run_manifest["protocol_sha256"],
            "mode": run_manifest["mode"],
            "terminal_status": terminal_status,
            "acceptance_result": acceptance_result,
            "usage_totals": usage_totals,
            "usage_coverage": usage_coverage,
            "operator_action_counts": operator_action_counts,
            "artifact_bytes_written": artifact_bytes_written,
            "raw_spool_bytes_written": raw_spool_bytes_written,
        }
    )
    bundle = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "experiment_run_id": run_manifest["experiment_run_id"],
        "protocol_sha256": run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(run_manifest),
        "runtime_release_identity": copy.deepcopy(
            runtime_release_identity
        ),
        "mode": run_manifest["mode"],
        "repetition_index": run_manifest["repetition_index"],
        "source_commit": protocol["repository"]["commit"],
        "started_at": started_at,
        "finished_at": finished_at,
        "terminal_status": terminal_status,
        "acceptance_result": copy.deepcopy(acceptance_result),
        "usage_totals": copy.deepcopy(usage_totals),
        "usage_coverage": copy.deepcopy(usage_coverage),
        "budget_result": copy.deepcopy(budget_result),
        "attempt_counts": copy.deepcopy(attempt_counts),
        "verified_milestones": sorted(set(verified_milestones)),
        "operator_action_counts": copy.deepcopy(
            operator_action_counts
        ),
        "retry_and_repair_counts": copy.deepcopy(
            retry_and_repair_counts
        ),
        "changed_files": sorted(set(changed_files)),
        "regressions": sorted(set(regressions)),
        "artifact_bytes_written": artifact_bytes_written,
        "raw_spool_bytes_written": raw_spool_bytes_written,
        "workspace_diff_sha256": workspace_diff_sha256,
        "result_evidence": copy.deepcopy(result_evidence),
        "projection_reconciliation": {
            "schema_version": PROJECTION_EXPECTATION_SCHEMA_VERSION,
            "identity_sha256": projection_identity,
        },
        "cleanup_status": cleanup_status,
    }
    validate_experiment_result_bundle(bundle)
    return bundle


def write_experiment_recovery_snapshot(
    run_dir,
    *,
    protocol,
    run_manifest,
    runtime_release_identity,
    snapshot_sequence,
    captured_at,
    controller_snapshot,
    operator_action_counts,
    resume_binding_sha256,
    recovery_context,
):
    """Publish one immutable, monotonically versioned resumable snapshot."""
    run_dir = _existing_directory(run_dir, "experiment run directory")
    if (run_dir / "results" / "terminal").exists():
        raise ExperimentResultIntegrityError(
            "terminal experiment cannot publish a recovery snapshot"
        )
    protocol = copy.deepcopy(protocol)
    run_manifest = copy.deepcopy(run_manifest)
    validate_experiment_protocol(protocol)
    validate_experiment_run_manifest(run_manifest, protocol)
    if run_dir.name != run_manifest["experiment_run_id"]:
        raise ExperimentResultIntegrityError(
            "recovery directory does not match experiment_run_id"
        )
    if (
        not isinstance(snapshot_sequence, int)
        or isinstance(snapshot_sequence, bool)
        or snapshot_sequence < 1
    ):
        raise ExperimentResultError(
            "snapshot_sequence must be a positive integer"
        )
    controller_status = (
        controller_snapshot.get("controller_status")
        if isinstance(controller_snapshot, dict)
        else None
    )
    resumable = controller_status == "interrupted"
    if not isinstance(recovery_context, dict):
        raise ExperimentResultError("recovery_context must be an object")
    controller_reference = recovery_context.get("controller_reference")
    clean_snapshot_reference = recovery_context.get(
        "clean_snapshot_attestation"
    )
    validated = _validate_live_recovery_authorities(
        run_dir,
        experiment_run_id=run_manifest["experiment_run_id"],
        protocol_sha256=run_manifest["protocol_sha256"],
        run_manifest_sha256=canonical_json_sha256(run_manifest),
        runtime_release_identity=runtime_release_identity,
        controller_reference=controller_reference,
        clean_snapshot_reference=clean_snapshot_reference,
        resume_binding_sha256=resume_binding_sha256,
    )
    if controller_snapshot != validated["controller_snapshot"]:
        raise ExperimentResultIntegrityError(
            "recovery controller snapshot differs from live authority"
        )
    if not resumable:
        raise ExperimentResultIntegrityError(
            "recovery snapshot requires an interrupted controller"
        )
    snapshot = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "experiment_run_id": run_manifest["experiment_run_id"],
        "protocol_sha256": run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(run_manifest),
        "runtime_release_identity": copy.deepcopy(
            runtime_release_identity
        ),
        "mode": run_manifest["mode"],
        "repetition_index": run_manifest["repetition_index"],
        "snapshot_sequence": snapshot_sequence,
        "captured_at": captured_at,
        "resume_phase": recovery_context.get("resume_phase"),
        "reason": recovery_context.get("reason"),
        "run_state_version": recovery_context.get("run_state_version"),
        "controller_status": controller_status,
        "controller_reference": validated["controller_reference"],
        "controller_checkpoint_sequence": controller_snapshot.get(
            "checkpoint_sequence"
        ),
        "controller_state_sha256": canonical_json_sha256(
            controller_snapshot
        ),
        "budget_result": _budget_summary(
            controller_snapshot.get("budget_state", {})
        ),
        "operator_action_counts": copy.deepcopy(
            operator_action_counts
        ),
        "resume_binding_sha256": resume_binding_sha256,
        "clean_snapshot_attestation": validated[
            "clean_snapshot_reference"
        ],
        "scan_scope_reference": copy.deepcopy(
            recovery_context.get("scan_scope_reference")
        ),
        "invocation_set_reference": copy.deepcopy(
            recovery_context.get("invocation_set_reference")
        ),
        "open_invocation_ids": sorted(
            set(recovery_context.get("open_invocation_ids", []))
        ),
        "integration_active": recovery_context.get(
            "integration_active",
            False,
        ),
        "adapter_checkpoint": copy.deepcopy(
            recovery_context.get("adapter_checkpoint")
        ),
        "resumable": resumable,
    }
    validate_experiment_recovery_snapshot(snapshot)
    with _publication_lock(_run_publication_lock_path(run_dir)):
        if (run_dir / "results" / "terminal").exists():
            raise ExperimentResultIntegrityError(
                "terminal experiment cannot publish a recovery snapshot"
            )
        recovery_root = run_dir / "recovery"
        recovery_root.mkdir(mode=0o700, exist_ok=True)
        final_dir = recovery_root / f"snapshot-{snapshot_sequence:08d}"
        existing = _validate_snapshot_sequence(recovery_root)
        if (
            not final_dir.exists()
            and snapshot_sequence != len(existing) + 1
        ):
            raise ExperimentResultIntegrityError(
                "recovery snapshot sequence is not contiguous"
            )
        digest = _publish_sealed_directory(
            final_dir,
            snapshot,
            payload_name=_SNAPSHOT_FILE,
            digest_name=_SNAPSHOT_DIGEST_FILE,
            staging_prefix=".snapshot-staging-",
        )
        _validate_snapshot_sequence(recovery_root)
    return {
        "snapshot_status": "published",
        "snapshot_dir": str(final_dir),
        "snapshot_sha256": digest,
        "snapshot": snapshot,
    }


def load_latest_experiment_recovery_snapshot(run_dir):
    run_dir = _existing_directory(run_dir, "experiment run directory")
    recovery_root = run_dir / "recovery"
    snapshots = _validate_snapshot_sequence(recovery_root)
    if not snapshots:
        return None
    _sequence, path = snapshots[-1]
    snapshot, digest = _load_sealed_directory(
        path,
        payload_name=_SNAPSHOT_FILE,
        digest_name=_SNAPSHOT_DIGEST_FILE,
    )
    validate_experiment_recovery_snapshot(snapshot)
    terminal_exists = (run_dir / "results" / "terminal").exists()
    if not terminal_exists and snapshot["resumable"]:
        _validate_live_recovery_authorities(
            run_dir,
            experiment_run_id=snapshot["experiment_run_id"],
            protocol_sha256=snapshot["protocol_sha256"],
            run_manifest_sha256=snapshot["run_manifest_sha256"],
            runtime_release_identity=snapshot[
                "runtime_release_identity"
            ],
            controller_reference=snapshot["controller_reference"],
            clean_snapshot_reference=snapshot[
                "clean_snapshot_attestation"
            ],
            resume_binding_sha256=snapshot[
                "resume_binding_sha256"
            ],
            expected_controller_state_sha256=snapshot[
                "controller_state_sha256"
            ],
            expected_controller_checkpoint_sequence=snapshot[
                "controller_checkpoint_sequence"
            ],
            expected_controller_status=snapshot["controller_status"],
        )
    return {
        "snapshot_dir": str(path),
        "snapshot_sha256": digest,
        "snapshot": snapshot,
        "resumable": (
            snapshot["resumable"]
            and not terminal_exists
        ),
        "superseded_by_terminal": terminal_exists,
    }


def seal_experiment_result_bundle(
    run_dir,
    bundle,
    *,
    protocol,
    run_manifest,
    runtime_release_identity,
    authority_root,
    evaluation_path,
    invocation_set_reference,
    protocol_reference,
    provider_sandbox_reference,
    scan_scope_reference,
    retained_roots,
    canary_path,
    raw_spool_roots=(),
):
    """Validate all bound authorities and atomically publish a terminal result."""
    run_dir = _existing_directory(run_dir, "experiment run directory")
    bundle = copy.deepcopy(bundle)
    validate_experiment_result_bundle(bundle)
    protocol = copy.deepcopy(protocol)
    run_manifest = copy.deepcopy(run_manifest)
    validate_experiment_protocol(protocol)
    validate_experiment_run_manifest(run_manifest, protocol)
    if run_dir.name != run_manifest["experiment_run_id"]:
        raise ExperimentResultIntegrityError(
            "result directory does not match experiment_run_id"
        )
    binding = {
        "experiment_run_id": run_manifest["experiment_run_id"],
        "protocol_sha256": run_manifest["protocol_sha256"],
        "run_manifest_sha256": canonical_json_sha256(run_manifest),
        "runtime_release_identity": runtime_release_identity,
        "mode": run_manifest["mode"],
        "repetition_index": run_manifest["repetition_index"],
        "source_commit": protocol["repository"]["commit"],
    }
    if any(bundle.get(key) != value for key, value in binding.items()):
        raise ExperimentResultIntegrityError(
            "result bundle identity differs from frozen run authority"
        )
    retained_roots = _normalize_retained_roots(retained_roots)
    scan_scope, scan_scope_digest = load_scan_scope_reference(
        scan_scope_reference,
        authority_root,
    )
    if scan_scope != retained_roots:
        raise ExperimentResultIntegrityError(
            "result scan scope is not the complete retained root set"
        )
    evidence_path = _existing_file(
        evaluation_path,
        "experiment evaluation evidence",
    )
    try:
        evaluation = json.loads(
            evidence_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentResultIntegrityError(
            "experiment evaluation evidence is unreadable"
        ) from exc
    expected = bundle["result_evidence"]
    evidence_relative_path = _relative_path(evidence_path, run_dir)
    evidence_sha256 = _file_sha256(evidence_path)
    expected_values = {
        "evaluation_relative_path": evidence_relative_path,
        "evaluation_sha256": evidence_sha256,
        "protocol_reference_sha256": protocol_reference["sha256"],
        "invocation_set_reference_sha256": (
            invocation_set_reference["sha256"]
        ),
        "scan_scope_reference_sha256": scan_scope_reference["sha256"],
        "scan_scope_sha256": scan_scope_digest,
        "provider_sandbox_reference_sha256": (
            provider_sandbox_reference["sha256"]
        ),
    }
    if any(expected.get(key) != value for key, value in expected_values.items()):
        raise ExperimentResultIntegrityError(
            "result evidence authority binding mismatch"
        )
    resource_relative_path = expected.get("resource_evidence_relative_path")
    if resource_relative_path is not None:
        resource_path = _existing_file(
            run_dir / resource_relative_path,
            "Phase 3 resource evidence index",
        )
        if _relative_path(resource_path, run_dir) != resource_relative_path:
            raise ExperimentResultIntegrityError(
                "resource evidence path differs from run authority"
            )
        try:
            resource_index = json.loads(
                resource_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ExperimentResultIntegrityError(
                "resource evidence index is unreadable"
            ) from exc
        if (
            expected.get("resource_evidence_sha256")
            != canonical_json_sha256(resource_index)
            or resource_index.get("experiment_run_id")
            != run_manifest["experiment_run_id"]
            or resource_index.get("mode") != run_manifest["mode"]
        ):
            raise ExperimentResultIntegrityError(
                "resource evidence index differs from run authority"
            )
    protocol_bindings = {
        "acceptance_command_sha256": canonical_json_sha256(
            protocol["acceptance"]["command"]
        ),
        "evaluator_sha256": protocol["evaluator"]["artifact_sha256"],
    }
    if any(
        expected.get(key) != value
        for key, value in protocol_bindings.items()
    ):
        raise ExperimentResultIntegrityError(
            "result evidence differs from protocol authority"
        )
    if (
        bundle["acceptance_result"]["evaluation_sha256"]
        != evidence_sha256
    ):
        raise ExperimentResultIntegrityError(
            "acceptance result differs from evaluation authority"
        )
    validate_evaluation_evidence(
        evaluation,
        expected_run_id=bundle["experiment_run_id"],
        expected_taskpack_ids=expected["taskpack_ids"],
        expected_protocol_sha256=bundle["protocol_sha256"],
        expected_protocol_reference_sha256=(
            expected["protocol_reference_sha256"]
        ),
        expected_acceptance_command_sha256=(
            expected["acceptance_command_sha256"]
        ),
        expected_acceptance_executable_sha256=(
            expected["acceptance_executable_sha256"]
        ),
        expected_evaluator_sha256=expected["evaluator_sha256"],
        expected_invocation_set_reference_sha256=(
            expected["invocation_set_reference_sha256"]
        ),
        expected_provider_sandbox_reference_sha256=(
            expected["provider_sandbox_reference_sha256"]
        ),
        authority_root=authority_root,
        invocation_set_reference=invocation_set_reference,
        experiment_protocol_reference=protocol_reference,
        provider_sandbox_reference=provider_sandbox_reference,
        canary_path=canary_path,
    )
    if evaluation["scan_scope_sha256"] != scan_scope_digest:
        raise ExperimentResultIntegrityError(
            "evaluation scan scope differs from result authority"
        )
    metrics = measure_experiment_artifacts(
        run_dir,
        artifact_roots=retained_roots["artifacts"],
        raw_spool_roots=raw_spool_roots,
    )
    if (
        bundle["artifact_bytes_written"]
        != metrics["artifact_bytes_written"]
        or bundle["raw_spool_bytes_written"]
        != metrics["raw_spool_bytes_written"]
    ):
        raise ExperimentResultIntegrityError(
            "result artifact byte metrics differ from measured authority"
        )
    expected_projection = _projection_identity(bundle)
    if (
        bundle["projection_reconciliation"]["identity_sha256"]
        != expected_projection
    ):
        raise ExperimentResultIntegrityError(
            "result projection expectation is inconsistent"
        )
    final_dir = run_dir / "results" / "terminal"
    final_dir.parent.mkdir(mode=0o700, exist_ok=True)
    with _publication_lock(_run_publication_lock_path(run_dir)):
        digest = _publish_sealed_directory(
            final_dir,
            bundle,
            payload_name=_RESULT_FILE,
            digest_name=_RESULT_DIGEST_FILE,
            staging_prefix=".result-staging-",
        )
    return {
        "result_status": "sealed",
        "result_dir": str(final_dir),
        "bundle_sha256": digest,
        "bundle": bundle,
    }


def load_experiment_result_bundle(run_dir):
    run_dir = _existing_directory(run_dir, "experiment run directory")
    result_dir = run_dir / "results" / "terminal"
    bundle, digest = _load_sealed_directory(
        result_dir,
        payload_name=_RESULT_FILE,
        digest_name=_RESULT_DIGEST_FILE,
    )
    validate_experiment_result_bundle(bundle)
    if (
        bundle["projection_reconciliation"]["identity_sha256"]
        != _projection_identity(bundle)
    ):
        raise ExperimentResultIntegrityError(
            "sealed result projection expectation is inconsistent"
        )
    sealed = {
        "result_dir": str(result_dir),
        "bundle_sha256": digest,
        "bundle": bundle,
    }
    receipt_path = run_dir / CLEANUP_RECEIPT_FILE_NAME
    if receipt_path.exists() or receipt_path.is_symlink():
        try:
            receipt = load_clean_snapshot_cleanup_receipt(
                run_dir,
                sealed_result=sealed,
            )
        except ExperimentWorkspaceError as exc:
            raise ExperimentResultIntegrityError(
                "clean snapshot cleanup receipt is invalid"
            ) from exc
        sealed["cleanup_status"] = receipt["receipt"][
            "cleanup_status"
        ]
        sealed["cleanup_receipt"] = receipt
    else:
        sealed["cleanup_status"] = "pending"
        sealed["cleanup_receipt"] = None
    return sealed


def validate_experiment_result_bundle(bundle):
    bundle = _validate_schema(
        bundle,
        "experiment_result_bundle.schema.json",
        ExperimentResultIntegrityError,
    )
    if _parse_timestamp(bundle["finished_at"]) < _parse_timestamp(
        bundle["started_at"]
    ):
        raise ExperimentResultIntegrityError(
            "experiment result finished_at precedes started_at"
        )
    return bundle


def validate_experiment_recovery_snapshot(snapshot):
    return _validate_schema(
        snapshot,
        "experiment_recovery_snapshot.schema.json",
        ExperimentResultIntegrityError,
    )


def render_experiment_result(bundle, *, bundle_sha256=None):
    cleanup_status = None
    if (
        isinstance(bundle, dict)
        and isinstance(bundle.get("bundle"), dict)
    ):
        result = bundle
        bundle = result["bundle"]
        bundle_sha256 = (
            bundle_sha256 or result.get("bundle_sha256")
        )
        cleanup_status = result.get("cleanup_status")
    validate_experiment_result_bundle(bundle)
    cleanup_status = cleanup_status or bundle["cleanup_status"]
    acceptance = bundle["acceptance_result"]["status"]
    usage = bundle["usage_totals"]
    coverage = bundle["usage_coverage"]
    interventions = bundle["operator_action_counts"].get(
        "corrective_intervention",
        0,
    )
    return "\n".join(
        (
            f"run: {bundle['experiment_run_id']}",
            f"mode: {bundle['mode']}",
            f"status: {bundle['terminal_status']}",
            f"acceptance: {acceptance}",
            (
                "tokens: "
                f"{usage.get('total_tokens')} "
                f"coverage={coverage.get('covered_invocations')}/"
                f"{coverage.get('total_invocations')}"
            ),
            f"interventions: {interventions}",
            f"changed_files: {len(bundle['changed_files'])}",
            f"cleanup: {cleanup_status}",
            f"bundle_sha256: {bundle_sha256 or 'unsealed'}",
        )
    )


def render_experiment_comparison(results):
    validate_experiment_comparison_compatibility(results)
    normalized = []
    for result in results:
        bundle = result.get("bundle") if isinstance(result, dict) else None
        digest = (
            result.get("bundle_sha256")
            if isinstance(result, dict)
            else None
        )
        validate_experiment_result_bundle(bundle)
        cleanup_status = (
            result.get("cleanup_status")
            if isinstance(result, dict)
            else None
        ) or bundle["cleanup_status"]
        normalized.append((bundle, digest, cleanup_status))
    lines = [
        (
            "mode | status | acceptance | cleanup | tokens | coverage | "
            "interventions | digest"
        )
    ]
    for bundle, digest, cleanup_status in sorted(
        normalized,
        key=lambda item: (
            item[0]["mode"],
            item[0]["repetition_index"],
            item[0]["experiment_run_id"],
        ),
    ):
        lines.append(
            " | ".join(
                (
                    bundle["mode"],
                    bundle["terminal_status"],
                    bundle["acceptance_result"]["status"],
                    cleanup_status,
                    str(bundle["usage_totals"].get("total_tokens")),
                    (
                        f"{bundle['usage_coverage'].get('covered_invocations')}/"
                        f"{bundle['usage_coverage'].get('total_invocations')}"
                    ),
                    str(
                        bundle["operator_action_counts"].get(
                            "corrective_intervention",
                            0,
                        )
                    ),
                    digest or "unsealed",
                )
            )
        )
    return "\n".join(lines)


def validate_experiment_comparison_compatibility(results):
    bundles = []
    for result in results:
        bundle = result.get("bundle") if isinstance(result, dict) else None
        validate_experiment_result_bundle(bundle)
        bundles.append(bundle)
    if not bundles:
        raise ExperimentResultIntegrityError(
            "experiment comparison requires at least one result"
        )
    expected = {
        "schema_version": bundles[0]["schema_version"],
        "protocol_sha256": bundles[0]["protocol_sha256"],
        "source_commit": bundles[0]["source_commit"],
        "runtime_release_identity": bundles[0]["runtime_release_identity"],
    }
    for bundle in bundles[1:]:
        mismatches = [
            key for key, value in expected.items() if bundle.get(key) != value
        ]
        if mismatches:
            raise ExperimentResultIntegrityError(
                "experiment comparison mixes incompatible authority: "
                + ", ".join(mismatches)
            )
    return {
        "comparison_status": "compatible",
        "result_count": len(bundles),
        **copy.deepcopy(expected),
    }


def _projection_identity(bundle):
    fields = {
        key: copy.deepcopy(bundle[key])
        for key in (
            "experiment_run_id",
            "protocol_sha256",
            "mode",
            "terminal_status",
            "acceptance_result",
            "usage_totals",
            "usage_coverage",
            "operator_action_counts",
            "artifact_bytes_written",
            "raw_spool_bytes_written",
        )
    }
    return canonical_json_sha256(fields)


def _budget_summary(value):
    if not isinstance(value, dict):
        raise ExperimentResultError("controller budget state is invalid")
    status = value.get("status")
    if status is None:
        status = "exhausted" if value.get("exhausted") else "within_budget"
    summary = {"status": status}
    for field in (
        "max_total_tokens",
        "max_wall_time_seconds",
        "total_tokens",
        "elapsed_wall_time_seconds",
        "overshoot_tokens",
        "usage_complete",
        "exhausted",
    ):
        if field in value:
            summary[field] = value[field]
    return summary


def _normalize_retained_roots(value):
    if not isinstance(value, dict) or set(value) != {
        "prompt",
        "context",
        "taskpack",
        "artifacts",
    }:
        raise ExperimentResultError(
            "retained roots must define prompt, context, taskpack, artifacts"
        )
    normalized = {}
    for group in ("prompt", "context", "taskpack", "artifacts"):
        roots = value[group]
        if isinstance(roots, (str, os.PathLike)):
            roots = [roots]
        if not isinstance(roots, (list, tuple)) or not roots:
            raise ExperimentResultError(
                f"retained {group} roots must be non-empty"
            )
        normalized[group] = sorted(
            str(_existing_path(root, f"retained {group} root"))
            for root in roots
        )
    return normalized


def _validate_live_recovery_authorities(
    run_dir,
    *,
    experiment_run_id,
    protocol_sha256,
    run_manifest_sha256,
    runtime_release_identity,
    controller_reference,
    clean_snapshot_reference,
    resume_binding_sha256,
    expected_controller_state_sha256=None,
    expected_controller_checkpoint_sequence=None,
    expected_controller_status=None,
):
    try:
        bound = validate_resume_binding(
            run_dir,
            runtime_release=runtime_release_identity,
            experiment_run_id=experiment_run_id,
            protocol_sha256=protocol_sha256,
            run_manifest_sha256=run_manifest_sha256,
        )
        validated_controller_reference = (
            validate_experiment_controller_reference(
                controller_reference
            )
        )
        controller = load_experiment_controller(
            validated_controller_reference
        )
        controller_snapshot = controller.snapshot()
        clean_snapshot_reference = _validate_clean_snapshot_reference(
            run_dir,
            clean_snapshot_reference,
            expected_experiment_run_id=experiment_run_id,
            expected_repository=bound["protocol"]["repository"],
        )
    except (
        ExperimentContractError,
        ExperimentControllerError,
        ExperimentWorkspaceError,
        OSError,
    ) as exc:
        raise ExperimentResultIntegrityError(
            "recovery authority validation failed"
        ) from exc
    expected_resume_binding = canonical_json_sha256(bound["binding"])
    if resume_binding_sha256 != expected_resume_binding:
        raise ExperimentResultIntegrityError(
            "recovery resume binding differs from immutable run authority"
        )
    protocol = bound["protocol"]
    controller_metadata = controller._metadata
    budget_state = controller_snapshot.get("budget_state", {})
    protocol_bindings = {
        "protocol_id": (
            validated_controller_reference["protocol_id"],
            protocol["experiment_id"],
        ),
        "protocol_sha256": (
            controller_metadata.get("protocol_sha256"),
            canonical_json_sha256(protocol),
        ),
        "scored": (
            controller_metadata.get("scored"),
            protocol["scored"],
        ),
        "operator_limits": (
            controller_metadata.get("operator_limits"),
            protocol["operator_limits"],
        ),
        "max_total_tokens": (
            budget_state.get("max_total_tokens"),
            protocol["budgets"]["max_total_tokens"],
        ),
        "max_wall_time_seconds": (
            budget_state.get("max_wall_time_seconds"),
            protocol["budgets"]["max_wall_time_seconds"],
        ),
        "soft_warning_ratio": (
            budget_state.get("soft_warning_ratio"),
            protocol["budgets"]["soft_warning_ratio"],
        ),
    }
    if any(actual != expected for actual, expected in protocol_bindings.values()):
        raise ExperimentResultIntegrityError(
            "recovery controller differs from run protocol authority"
        )
    controller_state_sha256 = canonical_json_sha256(controller_snapshot)
    expected_values = {
        "controller_state_sha256": (
            controller_state_sha256,
            expected_controller_state_sha256,
        ),
        "controller_checkpoint_sequence": (
            controller_snapshot.get("checkpoint_sequence"),
            expected_controller_checkpoint_sequence,
        ),
        "controller_status": (
            controller_snapshot.get("controller_status"),
            expected_controller_status,
        ),
    }
    for label, (actual, expected) in expected_values.items():
        if expected is not None and actual != expected:
            raise ExperimentResultIntegrityError(
                f"recovery {label} differs from live controller authority"
            )
    return {
        "binding": copy.deepcopy(bound["binding"]),
        "controller_reference": copy.deepcopy(
            validated_controller_reference
        ),
        "controller_snapshot": controller_snapshot,
        "clean_snapshot_reference": clean_snapshot_reference,
    }


def _validate_clean_snapshot_reference(
    run_dir,
    reference,
    *,
    expected_experiment_run_id,
    expected_repository,
):
    if (
        not isinstance(reference, dict)
        or set(reference) != {"schema_version", "path", "sha256"}
        or reference.get("schema_version")
        != "clean_snapshot_attestation.v1"
    ):
        raise ExperimentResultIntegrityError(
            "clean snapshot authority reference fields are invalid"
        )
    path = _existing_file(
        reference["path"],
        "clean snapshot attestation",
    )
    expected_path = Path(run_dir) / ATTESTATION_FILE_NAME
    if path != expected_path.resolve(strict=True):
        raise ExperimentResultIntegrityError(
            "clean snapshot attestation is outside the bound run"
        )
    if _file_sha256(path) != reference["sha256"]:
        raise ExperimentResultIntegrityError(
            "clean snapshot attestation digest changed"
        )
    attestation = load_clean_snapshot_attestation(path)
    if (
        attestation["experiment_run_id"] != expected_experiment_run_id
        or attestation["repository"] != expected_repository
    ):
        raise ExperimentResultIntegrityError(
            "clean snapshot attestation differs from run authority"
        )
    return copy.deepcopy(reference)


def _publish_sealed_directory(
    final_dir,
    payload,
    *,
    payload_name,
    digest_name,
    staging_prefix,
):
    final_dir = Path(final_dir)
    canonical = canonical_json_bytes(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    final_dir.parent.mkdir(mode=0o700, exist_ok=True)
    lock_path = final_dir.parent / ".sealed-publication.lock"
    with _publication_lock(lock_path):
        if final_dir.exists():
            existing, existing_digest = _load_sealed_directory(
                final_dir,
                payload_name=payload_name,
                digest_name=digest_name,
            )
            if (
                existing_digest != digest
                or canonical_json_bytes(existing) != canonical
            ):
                raise ExperimentResultConflict(
                    "sealed result authority conflicts with replay"
                )
            return digest
        staging = final_dir.parent / f"{staging_prefix}{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            _write_fsynced(staging / payload_name, canonical)
            _write_fsynced(
                staging / digest_name,
                f"{digest}  {payload_name}\n".encode("ascii"),
            )
            _fsync_directory(staging)
            try:
                _rename_directory_noreplace(staging, final_dir)
            except FileExistsError:
                existing, existing_digest = _load_sealed_directory(
                    final_dir,
                    payload_name=payload_name,
                    digest_name=digest_name,
                )
                if (
                    existing_digest != digest
                    or canonical_json_bytes(existing) != canonical
                ):
                    raise ExperimentResultConflict(
                        "sealed result authority conflicts with replay"
                    )
            _fsync_directory(final_dir.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return digest


def _run_publication_lock_path(run_dir):
    return Path(run_dir) / ".experiment-result-publication.lock"


def _load_sealed_directory(final_dir, *, payload_name, digest_name):
    final_dir = _existing_directory(final_dir, "sealed result directory")
    try:
        names = {entry.name for entry in os.scandir(final_dir)}
    except OSError as exc:
        raise ExperimentResultIntegrityError(
            "sealed result directory is unreadable"
        ) from exc
    if names != {payload_name, digest_name}:
        raise ExperimentResultIntegrityError(
            "sealed result directory fields are invalid"
        )
    payload_path = _existing_file(
        final_dir / payload_name,
        "sealed result payload",
    )
    digest_path = _existing_file(
        final_dir / digest_name,
        "sealed result digest",
    )
    payload = payload_path.read_bytes()
    if len(payload) > _MAX_AUTHORITY_BYTES:
        raise ExperimentResultIntegrityError(
            "sealed result payload exceeds bounded size"
        )
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ExperimentResultIntegrityError(
            "sealed result payload is invalid"
        ) from exc
    if canonical_json_bytes(value) != payload:
        raise ExperimentResultIntegrityError(
            "sealed result payload is not canonical"
        )
    digest = hashlib.sha256(payload).hexdigest()
    expected_sidecar = f"{digest}  {payload_name}\n"
    if digest_path.read_text(encoding="ascii") != expected_sidecar:
        raise ExperimentResultIntegrityError(
            "sealed result digest sidecar mismatch"
        )
    return value, digest


def _validate_snapshot_sequence(recovery_root):
    recovery_root = Path(recovery_root)
    if not recovery_root.exists():
        return []
    snapshots = []
    for path in sorted(recovery_root.iterdir()):
        if not path.name.startswith("snapshot-"):
            continue
        try:
            sequence = int(path.name.removeprefix("snapshot-"))
        except ValueError as exc:
            raise ExperimentResultIntegrityError(
                "recovery snapshot name is invalid"
            ) from exc
        snapshot, _digest = _load_sealed_directory(
            path,
            payload_name=_SNAPSHOT_FILE,
            digest_name=_SNAPSHOT_DIGEST_FILE,
        )
        validate_experiment_recovery_snapshot(snapshot)
        if snapshot["snapshot_sequence"] != sequence:
            raise ExperimentResultIntegrityError(
                "recovery snapshot sequence binding mismatch"
            )
        snapshots.append((sequence, path))
    if [item[0] for item in snapshots] != list(
        range(1, len(snapshots) + 1)
    ):
        raise ExperimentResultIntegrityError(
            "recovery snapshot sequence is not contiguous"
        )
    return snapshots


def _validate_schema(value, filename, error_type):
    try:
        schema = json.loads(
            schema_path(filename).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise error_type(f"{filename} is unavailable") from exc
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(value),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(
            str(item) for item in first.absolute_path
        ) or "<root>"
        raise error_type(
            f"{filename} failed at {location}: {first.message}"
        )
    return value


def _normalize_descendant_roots(run_dir, roots, label):
    normalized = []
    for root in roots:
        path = _existing_path(root, label)
        try:
            path.relative_to(run_dir)
        except ValueError as exc:
            raise ExperimentResultError(
                f"{label} must be below the experiment run directory"
            ) from exc
        normalized.append(path)
    return tuple(sorted(set(normalized), key=str))


def _bounded_files(root, *, prune_excluded, excluded_roots=()):
    if _under_any(root, excluded_roots):
        return []
    if root.is_file():
        return [root]
    files = []
    for current_root, directories, names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        directories[:] = sorted(
            name
            for name in directories
            if not (current / name).is_symlink()
            and (not prune_excluded or name not in _EXCLUDED_DIRECTORY_NAMES)
            and not _under_any(current / name, excluded_roots)
        )
        for name in sorted(names):
            path = current / name
            if path.is_symlink() or _under_any(path, excluded_roots):
                continue
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                files.append(path)
                if len(files) > _MAX_ARTIFACT_FILES:
                    raise ExperimentResultError(
                        "experiment artifact file count exceeds bounded limit"
                    )
    return files


def _under_any(path, roots):
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _existing_path(value, label):
    path = Path(value)
    if path.is_symlink():
        raise ExperimentResultIntegrityError(f"{label} cannot be a symlink")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ExperimentResultIntegrityError(
            f"{label} is unavailable"
        ) from exc


def _existing_directory(value, label):
    path = _existing_path(value, label)
    if not path.is_dir():
        raise ExperimentResultIntegrityError(
            f"{label} must be a directory"
        )
    return path


def _existing_file(value, label):
    path = _existing_path(value, label)
    if not path.is_file():
        raise ExperimentResultIntegrityError(f"{label} must be a file")
    return path


def _relative_path(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ExperimentResultIntegrityError(
            "result evidence must be retained below the run directory"
        ) from exc


def _write_fsynced(path, payload):
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ExperimentResultIntegrityError(
                    "sealed result write made no progress"
                )
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_directory_noreplace(source, destination):
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise ExperimentResultIntegrityError(
            "atomic no-replace directory publication is unavailable"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise ExperimentResultIntegrityError(
            "atomic no-replace directory publication is unavailable"
        )
    raise OSError(error, os.strerror(error), destination)


@contextmanager
def _publication_lock(path):
    fd = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ExperimentResultIntegrityError(
            "experiment result timestamp is invalid"
        ) from exc
    return parsed

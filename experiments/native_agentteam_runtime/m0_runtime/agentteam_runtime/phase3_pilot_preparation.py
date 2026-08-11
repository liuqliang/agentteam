"""Provider-free Phase 3B inventory binding and selection preparation.

This module deliberately separates operator-owned experiment choices from the
mechanical selection replay.  An incomplete or invalid decision input produces
a structured ``decision_input_required`` report and no selection artifact.
"""

from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator, FormatChecker

from .benchmark_adapter import (
    BenchmarkAdapterError,
    select_complexity_stratified_instances,
    validate_benchmark_instance_selection,
    validate_swe_evo_metadata,
)
from .experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    schema_path,
)

FIXED_SOURCE_COMMIT = "9b83d5af943ba7a17567336f5b18239f73960219"
FIXED_DATASET_ARTIFACT_SHA256 = "74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520"
FIXED_DATASET_SPLIT = "test"
FIXED_DATASET_ROW_COUNT = 48
FIXED_DATASET_ARTIFACT_PATH = "hf_out/hf_dataset/test/data-00000-of-00001.arrow"
FIXED_SOURCE_REPOSITORY = "https://github.com/SWE-EVO/SWE-EVO.git"
ROUTING_MANIFEST_SCHEMA_VERSION = "phase3_routing_manifest.v1"
PILOT_DECISIONS_SCHEMA_VERSION = "phase3_pilot_decisions.v1"
DECISION_INPUT_REPORT_SCHEMA_VERSION = "phase3_decision_input_report.v1"
SELECTION_AUTHORITY_SCHEMA_VERSION = "phase3_selection_authority.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROUTING_FIELDS = (
    "instance_id",
    "repository",
    "complexity_stratum",
    "language",
    "tags",
)

_DECISION_LEAF_PATHS = (
    "schema_version",
    "authority.decision_id",
    "authority.revision",
    "authority.status",
    "authority.decided_by",
    "authority.decided_at",
    "complexity.proxy.name",
    "complexity.proxy.source",
    "complexity.proxy.extraction_rule",
    "complexity.proxy.source_fields",
    "complexity.gold_blind",
    "complexity.stratum_boundaries",
    "selection.metadata_revision",
    "selection.filters.repositories",
    "selection.filters.languages",
    "selection.filters.required_tags",
    "selection.filters.excluded_instance_ids",
    "selection.seed",
    "selection.stratum_quotas",
    "selection.sample_size",
    "execution.model",
    "execution.reasoning_profile",
    "execution.per_instance_budget.max_total_tokens",
    "execution.per_instance_budget.max_wall_time_seconds",
    "thresholds.non_inferiority_margin",
    "thresholds.max_token_cost_ratio",
    "thresholds.max_wall_time_cost_ratio",
    "thresholds.preselected_secondary_benefit_metric",
)


class Phase3PreparationError(BenchmarkAdapterError):
    """Raised when fixed inventory or gold-blind routing input is unsafe."""


def fixed_dataset_binding():
    """Return the immutable upstream binding used by Phase 3B."""
    return {
        "benchmark": "swe_evo",
        "source_repository": FIXED_SOURCE_REPOSITORY,
        "source_commit": FIXED_SOURCE_COMMIT,
        "artifact_path": FIXED_DATASET_ARTIFACT_PATH,
        "artifact_sha256": FIXED_DATASET_ARTIFACT_SHA256,
        "split": FIXED_DATASET_SPLIT,
        "row_count": FIXED_DATASET_ROW_COUNT,
    }


def convert_swe_evo_inventory(inventory, *, metadata_revision="swe-evo-fixed-r1"):
    """Convert an upstream inventory to an allowlisted, gold-blind manifest.

    ``inventory`` must contain the fixed binding fields and either ``instances``
    or ``rows``.  Rows are reduced to the exact metadata allowlist before any
    selection code can consume them; evaluator-only fields therefore cannot
    cross this boundary.
    """
    if not isinstance(inventory, dict):
        raise Phase3PreparationError("upstream inventory must be an object")
    binding = fixed_dataset_binding()
    allowed_top_level = set(binding) | {"instances", "rows"}
    unknown_top_level = set(inventory) - allowed_top_level
    if unknown_top_level:
        raise Phase3PreparationError(
            f"upstream inventory contains non-routing fields: {sorted(unknown_top_level)}"
        )
    if "instances" in inventory and "rows" in inventory:
        raise Phase3PreparationError("upstream inventory must use exactly one row container")
    for field, expected in binding.items():
        if inventory.get(field) != expected:
            raise Phase3PreparationError(f"fixed inventory binding mismatch: {field}")
    rows = inventory.get("instances", inventory.get("rows"))
    if not isinstance(rows, list):
        raise Phase3PreparationError("upstream inventory must contain instances or rows")
    if len(rows) != FIXED_DATASET_ROW_COUNT:
        raise Phase3PreparationError(
            "fixed inventory row count mismatch: "
            f"expected {FIXED_DATASET_ROW_COUNT}, got {len(rows)}"
        )
    # Reject before projection: silently dropping gold would make the boundary
    # ambiguous and could permit a caller to believe it was routed.
    allowed_row_fields = set(ROUTING_FIELDS)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise Phase3PreparationError(f"inventory row {index} must be an object")
        unknown = set(row) - allowed_row_fields
        if unknown:
            raise Phase3PreparationError(f"inventory row {index} contains non-routing fields: {sorted(unknown)}")
    metadata = {
        "schema_version": "swe_evo_metadata.v1",
        "benchmark": "swe_evo",
        "metadata_revision": metadata_revision,
        "instances": rows,
    }
    try:
        metadata = validate_swe_evo_metadata(metadata, expected_revision=metadata_revision)
    except BenchmarkAdapterError as exc:
        raise Phase3PreparationError(str(exc)) from exc
    manifest_body = {
        "dataset": binding,
        "metadata": metadata,
        "gold_visibility": "evaluator_only",
        "allowlisted_fields": list(ROUTING_FIELDS),
    }
    manifest = {
        "schema_version": ROUTING_MANIFEST_SCHEMA_VERSION,
        "manifest": manifest_body,
        "manifest_sha256": canonical_json_sha256(manifest_body),
    }
    validate_routing_manifest(manifest)
    return manifest


build_gold_blind_routing_manifest = convert_swe_evo_inventory
convert_fixed_swe_evo_inventory = convert_swe_evo_inventory


def validate_routing_manifest(manifest):
    value = json.loads(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    schema = json.loads(schema_path("phase3_routing_manifest.schema.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors:
        raise Phase3PreparationError(f"routing manifest schema validation failed: {errors[0].message}")
    if value["manifest_sha256"] != canonical_json_sha256(value["manifest"]):
        raise Phase3PreparationError("manifest_sha256 does not bind canonical content")
    body = value["manifest"]
    if body["dataset"] != fixed_dataset_binding():
        raise Phase3PreparationError("routing manifest dataset binding is not fixed")
    if body["allowlisted_fields"] != list(ROUTING_FIELDS):
        raise Phase3PreparationError("routing manifest allowlisted_fields are not fixed")
    metadata = validate_swe_evo_metadata(
        body["metadata"],
        expected_revision=body["metadata"]["metadata_revision"],
    )
    if len(metadata["instances"]) != FIXED_DATASET_ROW_COUNT:
        raise Phase3PreparationError("routing manifest row count is not fixed")
    return value


def routing_manifest_bytes(manifest):
    """Return canonical bytes, proving equal inputs produce equal output."""
    return json.dumps(validate_routing_manifest(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def decision_input_report(decisions, *, routing_manifest=None):
    """Return deterministic evidence that decisions are ready or still required.

    The report never fills a missing value.  When its status is
    ``decision_input_required``, it intentionally contains no selection.
    """

    missing = _missing_decision_paths(decisions)
    invalid = _decision_schema_errors(decisions)
    snapshot = _json_object_snapshot(decisions)
    if snapshot is not None and not missing and not invalid:
        invalid.extend(_decision_semantic_errors(snapshot, routing_manifest))
    status = (
        "decision_input_ready"
        if not missing and not invalid
        else "decision_input_required"
    )
    body = {
        "status": status,
        "decision_schema_version": PILOT_DECISIONS_SCHEMA_VERSION,
        "missing_decisions": missing,
        "invalid_decisions": invalid,
        "selection_freeze": "ready" if status == "decision_input_ready" else "blocked",
    }
    return {
        "schema_version": DECISION_INPUT_REPORT_SCHEMA_VERSION,
        **body,
        "report_sha256": canonical_json_sha256(body),
    }


report_missing_pilot_decisions = decision_input_report


def validate_phase3_pilot_decisions(decisions, *, routing_manifest=None):
    """Validate and return a detached, complete experiment-decision input."""

    report = decision_input_report(decisions, routing_manifest=routing_manifest)
    if report["status"] != "decision_input_ready":
        raise Phase3PreparationError(
            "decision_input_required: "
            f"missing={report['missing_decisions']}, "
            f"invalid={report['invalid_decisions']}"
        )
    return json.loads(canonical_json_bytes(decisions).decode("utf-8"))


validate_experiment_decision_input = validate_phase3_pilot_decisions


def prepare_phase3_pilot_selection(routing_manifest, decisions):
    """Freeze selection only after every claim-bearing decision is authorized.

    Missing, schema-invalid, or routing-mismatched inputs return structured
    ``decision_input_required`` evidence.  Successful output binds the complete
    decision input, routing manifest, and deterministic adapter selection.
    """

    report = decision_input_report(decisions)
    if report["status"] != "decision_input_ready":
        return report
    try:
        manifest = validate_routing_manifest(routing_manifest)
    except (BenchmarkAdapterError, ExperimentContractError, ValueError) as exc:
        return _decision_required_report(
            [],
            [{"path": "routing_manifest", "message": str(exc)}],
        )
    report = decision_input_report(decisions, routing_manifest=manifest)
    if report["status"] != "decision_input_ready":
        return report
    decision = validate_phase3_pilot_decisions(
        decisions,
        routing_manifest=manifest,
    )
    selection_input = decision["selection"]
    metadata = manifest["manifest"]["metadata"]
    try:
        selection = select_complexity_stratified_instances(
            metadata,
            metadata_revision=selection_input["metadata_revision"],
            filters=selection_input["filters"],
            seed=selection_input["seed"],
            stratum_quotas=selection_input["stratum_quotas"],
        )
    except BenchmarkAdapterError as exc:
        return _decision_required_report(
            [],
            [{"path": "selection", "message": str(exc)}],
        )
    authority = {
        "schema_version": SELECTION_AUTHORITY_SCHEMA_VERSION,
        "status": "selection_frozen",
        "decision_id": decision["authority"]["decision_id"],
        "decision_revision": decision["authority"]["revision"],
        "decision_input_sha256": canonical_json_sha256(decision),
        "routing_manifest_sha256": manifest["manifest_sha256"],
        "selection": selection,
    }
    authority["selection_authority_sha256"] = selection_authority_digest(
        authority
    )
    return validate_phase3_selection_authority(
        authority,
        routing_manifest=manifest,
        decisions=decision,
    )


replay_deterministic_selection = prepare_phase3_pilot_selection


def replay_phase3_pilot_selection(routing_manifest, decisions, frozen_authority):
    """Replay selection and reject any difference from frozen authority."""

    replay = prepare_phase3_pilot_selection(routing_manifest, decisions)
    if replay.get("status") != "selection_frozen":
        return replay
    frozen = validate_phase3_selection_authority(
        frozen_authority,
        routing_manifest=routing_manifest,
        decisions=decisions,
    )
    if replay != frozen:
        raise Phase3PreparationError(
            "selection authority does not match deterministic replay"
        )
    return replay


def selection_authority_digest(authority):
    """Digest every selection-authority field except the digest itself."""

    value = _json_object_snapshot(authority)
    if value is None:
        raise Phase3PreparationError("selection authority must be a JSON object")
    value.pop("selection_authority_sha256", None)
    return canonical_json_sha256(value)


def selection_authority_bytes(authority):
    """Return canonical bytes for a validated frozen selection authority."""

    return canonical_json_bytes(validate_phase3_selection_authority(authority))


def validate_phase3_selection_authority(
    authority,
    *,
    routing_manifest=None,
    decisions=None,
):
    """Validate digest bindings and optionally replay against source inputs."""

    value = _json_object_snapshot(authority)
    required = {
        "schema_version",
        "status",
        "decision_id",
        "decision_revision",
        "decision_input_sha256",
        "routing_manifest_sha256",
        "selection",
        "selection_authority_sha256",
    }
    if value is None or set(value) != required:
        raise Phase3PreparationError(
            "selection authority fields do not match the fixed allowlist"
        )
    if value["schema_version"] != SELECTION_AUTHORITY_SCHEMA_VERSION:
        raise Phase3PreparationError("unsupported selection authority schema_version")
    if value["status"] != "selection_frozen":
        raise Phase3PreparationError("selection authority is not frozen")
    if (
        not isinstance(value["decision_id"], str)
        or _SAFE_ID.fullmatch(value["decision_id"]) is None
    ):
        raise Phase3PreparationError(
            "selection authority decision_id must be a safe identifier"
        )
    if (
        not isinstance(value["decision_revision"], int)
        or isinstance(value["decision_revision"], bool)
        or value["decision_revision"] < 1
    ):
        raise Phase3PreparationError(
            "selection authority decision_revision must be a positive integer"
        )
    for field in (
        "decision_input_sha256",
        "routing_manifest_sha256",
        "selection_authority_sha256",
    ):
        if (
            not isinstance(value[field], str)
            or _SHA256.fullmatch(value[field]) is None
        ):
            raise Phase3PreparationError(
                f"selection authority {field} must be a lowercase SHA-256"
            )
    if value["selection_authority_sha256"] != selection_authority_digest(value):
        raise Phase3PreparationError(
            "selection_authority_sha256 does not bind canonical content"
        )
    try:
        selection = validate_benchmark_instance_selection(value["selection"])
    except BenchmarkAdapterError as exc:
        raise Phase3PreparationError(str(exc)) from exc
    if routing_manifest is not None:
        manifest = validate_routing_manifest(routing_manifest)
        if value["routing_manifest_sha256"] != manifest["manifest_sha256"]:
            raise Phase3PreparationError(
                "selection authority does not bind the routing manifest"
            )
        try:
            validate_benchmark_instance_selection(
                selection,
                metadata=manifest["manifest"]["metadata"],
            )
        except BenchmarkAdapterError as exc:
            raise Phase3PreparationError(str(exc)) from exc
    if decisions is not None:
        decision = validate_phase3_pilot_decisions(
            decisions,
            routing_manifest=routing_manifest,
        )
        if value["decision_id"] != decision["authority"]["decision_id"]:
            raise Phase3PreparationError("selection authority decision_id mismatch")
        if value["decision_revision"] != decision["authority"]["revision"]:
            raise Phase3PreparationError("selection authority decision revision mismatch")
        if value["decision_input_sha256"] != canonical_json_sha256(decision):
            raise Phase3PreparationError(
                "selection authority does not bind the decision input"
            )
    return value


def _missing_decision_paths(decisions):
    if not isinstance(decisions, dict):
        return list(_DECISION_LEAF_PATHS)
    missing = []
    for path in _DECISION_LEAF_PATHS:
        value = decisions
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                missing.append(path)
                break
            value = value[part]
    return missing


def _decision_schema_errors(decisions):
    snapshot = _json_object_snapshot(decisions)
    if snapshot is None:
        return [{"path": "<root>", "message": "decision input must be a JSON object"}]
    schema = json.loads(
        schema_path("phase3_pilot_decisions.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(snapshot),
        key=lambda error: (
            [str(part) for part in error.absolute_path],
            error.validator or "",
            error.message,
        ),
    )
    return [
        {
            "path": ".".join(str(part) for part in error.absolute_path)
            or "<root>",
            "message": error.message,
        }
        for error in errors
        if error.validator != "required"
    ]


def _decision_semantic_errors(decision, routing_manifest):
    errors = []
    quotas = decision["selection"]["stratum_quotas"]
    if decision["selection"]["sample_size"] != sum(quotas.values()):
        errors.append(
            {
                "path": "selection.sample_size",
                "message": "sample_size must equal the sum of stratum_quotas",
            }
        )
    boundary_names = [
        item["stratum"] for item in decision["complexity"]["stratum_boundaries"]
    ]
    if len(boundary_names) != len(set(boundary_names)):
        errors.append(
            {
                "path": "complexity.stratum_boundaries",
                "message": "stratum names must be unique",
            }
        )
    if set(boundary_names) != set(quotas):
        errors.append(
            {
                "path": "complexity.stratum_boundaries",
                "message": "stratum boundaries must cover exactly the quota strata",
            }
        )
    if routing_manifest is not None:
        try:
            manifest = validate_routing_manifest(routing_manifest)
        except (BenchmarkAdapterError, ExperimentContractError, ValueError) as exc:
            errors.append({"path": "routing_manifest", "message": str(exc)})
        else:
            metadata = manifest["manifest"]["metadata"]
            if decision["selection"]["metadata_revision"] != metadata["metadata_revision"]:
                errors.append(
                    {
                        "path": "selection.metadata_revision",
                        "message": "metadata_revision does not match the routing manifest",
                    }
                )
            available_strata = {
                item["complexity_stratum"] for item in metadata["instances"]
            }
            if not set(boundary_names).issubset(available_strata):
                errors.append(
                    {
                        "path": "complexity.stratum_boundaries",
                        "message": "authorized strata are absent from the routing manifest",
                    }
                )
    return errors


def _decision_required_report(missing, invalid):
    body = {
        "status": "decision_input_required",
        "decision_schema_version": PILOT_DECISIONS_SCHEMA_VERSION,
        "missing_decisions": list(missing),
        "invalid_decisions": list(invalid),
        "selection_freeze": "blocked",
    }
    return {
        "schema_version": DECISION_INPUT_REPORT_SCHEMA_VERSION,
        **body,
        "report_sha256": canonical_json_sha256(body),
    }


def _json_object_snapshot(value):
    if not isinstance(value, dict):
        return None
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (ExperimentContractError, json.JSONDecodeError):
        return None

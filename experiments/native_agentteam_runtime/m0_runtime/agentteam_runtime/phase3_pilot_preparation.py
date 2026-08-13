"""Provider-free Phase 3B inventory binding and selection preparation.

This module deliberately separates operator-owned experiment choices from the
mechanical selection replay.  An incomplete or invalid decision input produces
a structured ``decision_input_required`` report and no selection artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .benchmark_adapter import (
    BenchmarkAdapterError,
    select_complexity_stratified_instances,
    validate_benchmark_instance_selection,
    validate_swe_evo_metadata,
)
from .benchmark_preregistration import (
    BenchmarkPreregistrationError,
    build_benchmark_preregistration,
    validate_benchmark_preregistration,
)
from .experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    schema_path,
)
from .phase3_pilot import (
    Phase3PilotError,
    build_phase3_pilot_contract,
    validate_phase3_pilot_contract,
)
from .resource_envelope import (
    ResourceEnvelopeError,
    approved_phase3_resource_envelope_binding,
    canonical_resource_envelope_sha256,
    validate_phase3_resource_preflight_receipt,
)

FIXED_SOURCE_COMMIT = "9b83d5af943ba7a17567336f5b18239f73960219"
FIXED_DATASET_ARTIFACT_SHA256 = "74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520"
FIXED_DATASET_SPLIT = "test"
FIXED_DATASET_ROW_COUNT = 48
FIXED_DATASET_ARTIFACT_PATH = "hf_out/hf_dataset/test/data-00000-of-00001.arrow"
FIXED_SOURCE_REPOSITORY = "https://github.com/SWE-EVO/SWE-EVO.git"
ROUTING_MANIFEST_SCHEMA_VERSION = "phase3_routing_manifest.v1"
COMPLEXITY_PROJECTION_SCHEMA_VERSION = "phase3_complexity_projection.v1"
PILOT_DECISIONS_SCHEMA_VERSION = "phase3_pilot_decisions.v1"
DECISION_INPUT_REPORT_SCHEMA_VERSION = "phase3_decision_input_report.v1"
SELECTION_AUTHORITY_SCHEMA_VERSION = "phase3_selection_authority.v1"
DIRECT_TASKPACK_SCHEMA_VERSION = "phase3_direct_taskpack.v1"
INSTANCE_MATERIALIZATION_SCHEMA_VERSION = "phase3_instance_materialization.v1"
PILOT_PREFLIGHT_SCHEMA_VERSION = "phase3_pilot_preflight.v1"
PILOT_PREFLIGHT_V2_SCHEMA_VERSION = "phase3_pilot_preflight.v2"
PILOT_PREFLIGHT_DECISION_ID = "DEC-P3B-provider-free-preparation"
READINESS_BINDING = {
    "gate_id": "P3-READY",
    "controller_id": "phase3_readiness_controller_v1",
    "relation_id": "phase3_readiness_relation_v1",
    "evidence_sha256": "d87486c41ef79707d52408215b05d22b52d736d16380001590d031c4fb483af7",
    "receipt_content_sha256": "5fa7ff18044f2fcf5c01ee7d33b2592091d6af91bd09c1de641eeffe3a0bfc9d",
    "integration_head": "ebce7b10a0ab7b2f9dc3e3936df3f7c2e1e77a02",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ROUTING_FIELDS = (
    "instance_id",
    "repository",
    "complexity_stratum",
    "language",
    "tags",
)

COMPLEXITY_PROXY_NAME = "dataset_pr_record_count"
COMPLEXITY_PROXY_EXTRACTION_RULE = 'len(instance["PRs"])'
COMPLEXITY_METADATA_REVISION = "swe-evo-pr-count-r1"
EXCLUDED_CALIBRATION_INSTANCE_ID = "psf__requests_v2.27.0_v2.27.1"
PILOT_SELECTION_SEED = 20260812
PILOT_STRATUM_QUOTAS = {"high": 1, "low": 1, "medium": 1}
EXPECTED_POPULATION_COUNTS = {"high": 15, "low": 21, "medium": 12}
EXPECTED_ELIGIBLE_POPULATION_COUNTS = {
    "high": 15,
    "low": 20,
    "medium": 12,
}
EXPECTED_SELECTION_PREVIEW = (
    "psf__requests_v2.4.0_v2.4.1",
    "dask__dask_2023.3.2_2023.4.0",
    "iterative__dvc_2.19.0_2.20.0",
)
COMPLEXITY_STRATUM_BOUNDARIES = (
    {"stratum": "low", "minimum": 0, "maximum": 2},
    {"stratum": "medium", "minimum": 3, "maximum": 6},
    {"stratum": "high", "minimum": 7, "maximum": None},
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


def project_swe_evo_arrow_inventory(
    arrow_path,
    *,
    source_commit,
    split,
    metadata_revision=COMPLEXITY_METADATA_REVISION,
):
    """Project the fixed Arrow artifact into gold-blind routing metadata.

    This is a trusted evaluator-side boundary. The Arrow rows may contain gold
    and evaluator data, but only ``instance_id``, the repository identifier,
    and ``len(PRs)`` are inspected. The count is immediately reduced to the
    approved stratum and is never emitted per instance.

    ``pyarrow`` is imported lazily so provider-free runtime users that only
    consume a reviewed projection do not acquire an undeclared dependency.
    """

    if source_commit != FIXED_SOURCE_COMMIT:
        raise Phase3PreparationError(
            "fixed Arrow source drift: source_commit does not match"
        )
    if split != FIXED_DATASET_SPLIT:
        raise Phase3PreparationError(
            "fixed Arrow source drift: split does not match"
        )
    if (
        not isinstance(metadata_revision, str)
        or _SAFE_ID.fullmatch(metadata_revision) is None
    ):
        raise Phase3PreparationError(
            "complexity projection metadata_revision must be a safe identifier"
        )

    path = Path(arrow_path)
    if not path.is_file():
        raise Phase3PreparationError(
            f"fixed Arrow artifact is unavailable: {path}"
        )
    artifact_sha256 = _file_sha256(path)
    if artifact_sha256 != FIXED_DATASET_ARTIFACT_SHA256:
        raise Phase3PreparationError(
            "fixed Arrow source drift: artifact SHA-256 does not match"
        )

    rows = _read_swe_evo_arrow_rows(path)
    routing_rows, population_counts = _project_swe_evo_complexity_rows(rows)
    eligible_counts = dict(population_counts)
    excluded_rows = [
        row
        for row in routing_rows
        if row["instance_id"] == EXCLUDED_CALIBRATION_INSTANCE_ID
    ]
    if len(excluded_rows) != 1:
        raise Phase3PreparationError(
            "fixed Arrow projection must contain the calibration exclusion exactly once"
        )
    excluded_stratum = excluded_rows[0]["complexity_stratum"]
    eligible_counts[excluded_stratum] -= 1

    if population_counts != EXPECTED_POPULATION_COUNTS:
        raise Phase3PreparationError(
            "fixed Arrow complexity population mismatch: "
            f"expected {EXPECTED_POPULATION_COUNTS}, got {population_counts}"
        )
    if eligible_counts != EXPECTED_ELIGIBLE_POPULATION_COUNTS:
        raise Phase3PreparationError(
            "fixed Arrow eligible population mismatch: "
            f"expected {EXPECTED_ELIGIBLE_POPULATION_COUNTS}, got {eligible_counts}"
        )

    routing_manifest = convert_swe_evo_inventory(
        {
            **fixed_dataset_binding(),
            "instances": sorted(
                routing_rows,
                key=lambda row: row["instance_id"],
            ),
        },
        metadata_revision=metadata_revision,
    )
    body = {
        "source": fixed_dataset_binding(),
        "proxy": {
            "name": COMPLEXITY_PROXY_NAME,
            "source_field": "PRs",
            "extraction_rule": COMPLEXITY_PROXY_EXTRACTION_RULE,
            "gold_blind": True,
            "per_instance_count_retention": "discarded_after_stratification",
        },
        "stratum_boundaries": [dict(item) for item in COMPLEXITY_STRATUM_BOUNDARIES],
        "excluded_instance_ids": [EXCLUDED_CALIBRATION_INSTANCE_ID],
        "population": {
            "source_row_count": FIXED_DATASET_ROW_COUNT,
            "counts_by_stratum": population_counts,
            "eligible_row_count": FIXED_DATASET_ROW_COUNT - 1,
            "eligible_counts_by_stratum": eligible_counts,
        },
        "routing_manifest": routing_manifest,
        "routing_manifest_sha256": routing_manifest["manifest_sha256"],
    }
    projection = {
        "schema_version": COMPLEXITY_PROJECTION_SCHEMA_VERSION,
        **body,
    }
    projection["projection_sha256"] = canonical_json_sha256(projection)
    return validate_phase3_complexity_projection(projection)


build_trusted_arrow_projection = project_swe_evo_arrow_inventory


def validate_phase3_complexity_projection(projection):
    """Validate a projection and all source, population, and output bindings."""

    value = _json_object_snapshot(projection)
    if value is None:
        raise Phase3PreparationError("complexity projection must be a JSON object")
    schema = json.loads(
        schema_path("phase3_complexity_projection.schema.json").read_text(
            encoding="utf-8"
        )
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise Phase3PreparationError(
            "complexity projection schema validation failed at "
            f"{location}: {first.message}"
        )

    body = dict(value)
    supplied_digest = body.pop("projection_sha256")
    if supplied_digest != canonical_json_sha256(body):
        raise Phase3PreparationError(
            "projection_sha256 does not bind canonical content"
        )
    if value["source"] != fixed_dataset_binding():
        raise Phase3PreparationError("complexity projection source binding is not fixed")
    manifest = validate_routing_manifest(value["routing_manifest"])
    if value["routing_manifest_sha256"] != manifest["manifest_sha256"]:
        raise Phase3PreparationError(
            "complexity projection output digest does not bind the routing manifest"
        )

    counts = {"high": 0, "low": 0, "medium": 0}
    eligible = {"high": 0, "low": 0, "medium": 0}
    for row in manifest["manifest"]["metadata"]["instances"]:
        stratum = row["complexity_stratum"]
        if stratum not in counts:
            raise Phase3PreparationError(
                f"complexity projection contains unapproved stratum: {stratum}"
            )
        counts[stratum] += 1
        if row["instance_id"] != EXCLUDED_CALIBRATION_INSTANCE_ID:
            eligible[stratum] += 1
    if counts != value["population"]["counts_by_stratum"]:
        raise Phase3PreparationError(
            "complexity projection population does not match routing metadata"
        )
    if eligible != value["population"]["eligible_counts_by_stratum"]:
        raise Phase3PreparationError(
            "complexity projection eligible population does not match routing metadata"
        )
    return value


def complexity_projection_bytes(projection):
    """Return canonical bytes for a validated trusted projection."""

    return canonical_json_bytes(validate_phase3_complexity_projection(projection))


def replay_phase3_complexity_selection(projection):
    """Replay the approved preview through the existing deterministic selector."""

    value = validate_phase3_complexity_projection(projection)
    metadata = value["routing_manifest"]["manifest"]["metadata"]
    selection = select_complexity_stratified_instances(
        metadata,
        metadata_revision=metadata["metadata_revision"],
        filters={
            "repositories": [],
            "languages": [],
            "required_tags": [],
            "excluded_instance_ids": [EXCLUDED_CALIBRATION_INSTANCE_ID],
        },
        seed=PILOT_SELECTION_SEED,
        stratum_quotas=PILOT_STRATUM_QUOTAS,
    )
    if selection["eligible_counts_by_stratum"] != EXPECTED_ELIGIBLE_POPULATION_COUNTS:
        raise Phase3PreparationError(
            "complexity selection eligible populations do not match the approved projection"
        )
    if tuple(selection["ordered_instance_ids"]) != EXPECTED_SELECTION_PREVIEW:
        raise Phase3PreparationError(
            "deterministic complexity selection does not match the approved preview"
        )
    return selection


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


def build_phase3_instance_visible_input(
    *,
    instance_id,
    selection_authority,
    decisions,
    task_input,
    execution_profile,
):
    """Build one runtime-visible input from explicit, approved authorities.

    ``task_input`` contains only the instance-specific ``repository`` and
    ``task`` sections. ``execution_profile`` contains the common ``runtime``,
    ``model``, and ``execution`` sections. The model and reasoning profile are
    checked against the approved decision input rather than inferred here.
    """

    authority = validate_phase3_selection_authority(
        selection_authority,
        decisions=decisions,
    )
    decision = validate_phase3_pilot_decisions(decisions)
    _require_selected_instance(authority, instance_id)
    task_value = _require_exact_object(
        task_input,
        {"repository", "task"},
        "task_input",
    )
    profile = _require_exact_object(
        execution_profile,
        {"runtime", "model", "execution"},
        "execution_profile",
    )
    model = profile["model"]
    if not isinstance(model, dict):
        raise Phase3PreparationError("execution_profile.model must be an object")
    if model.get("model") != decision["execution"]["model"]:
        raise Phase3PreparationError(
            "execution profile model does not match the approved decision input"
        )
    if model.get("reasoning_profile") != decision["execution"][
        "reasoning_profile"
    ]:
        raise Phase3PreparationError(
            "execution profile reasoning_profile does not match the approved decision input"
        )
    visible = {
        "repository": task_value["repository"],
        "runtime": profile["runtime"],
        "task": task_value["task"],
        "model": model,
        "execution": profile["execution"],
    }
    return _validate_preregistration_definition(
        "shared_visible_inputs",
        visible,
    )


build_per_instance_visible_input = build_phase3_instance_visible_input


def build_phase3_instance_direct_taskpack(
    *,
    instance_id,
    selection_authority,
    decisions,
    visible_inputs,
    shared_budget,
):
    """Build a deterministic, frozen direct-mode taskpack for one instance."""

    authority = validate_phase3_selection_authority(
        selection_authority,
        decisions=decisions,
    )
    decision = validate_phase3_pilot_decisions(decisions)
    _require_selected_instance(authority, instance_id)
    visible = _validate_preregistration_definition(
        "shared_visible_inputs",
        visible_inputs,
    )
    budget = _validate_approved_budget(shared_budget, decision)
    body = {
        "taskpack_id": (
            "phase3b-direct-"
            + canonical_json_sha256(
                {
                    "instance_id": instance_id,
                    "selection_authority_sha256": authority[
                        "selection_authority_sha256"
                    ],
                }
            )[:20]
        ),
        "instance_id": instance_id,
        "status": "frozen",
        "execution_mode": "agentteam_direct",
        "live_authoring": False,
        "selection_authority_sha256": authority[
            "selection_authority_sha256"
        ],
        "visible_inputs": visible,
        "shared_budget": budget,
        "tasks": [
            {
                "task_id": "implementation",
                "objective": visible["task"]["goal"],
                "constraints": visible["task"]["constraints"],
                "non_goals": visible["task"]["non_goals"],
                "acceptance_commands": visible["task"][
                    "acceptance_commands"
                ],
            }
        ],
        "policy": {
            "gold_visibility": "evaluator_only",
            "cross_mode_artifact_access": "forbidden",
        },
    }
    taskpack = {
        "schema_version": DIRECT_TASKPACK_SCHEMA_VERSION,
        "taskpack": body,
        "taskpack_sha256": canonical_json_sha256(body),
    }
    return validate_phase3_instance_direct_taskpack(
        taskpack,
        instance_id=instance_id,
        selection_authority=authority,
        visible_inputs=visible,
        shared_budget=budget,
    )


build_per_instance_direct_taskpack = build_phase3_instance_direct_taskpack


def validate_phase3_instance_direct_taskpack(
    taskpack,
    *,
    instance_id=None,
    selection_authority=None,
    visible_inputs=None,
    shared_budget=None,
):
    """Validate a sealed per-instance direct taskpack and optional sources."""

    value = _require_exact_object(
        taskpack,
        {"schema_version", "taskpack", "taskpack_sha256"},
        "direct taskpack",
    )
    if value["schema_version"] != DIRECT_TASKPACK_SCHEMA_VERSION:
        raise Phase3PreparationError("unsupported direct taskpack schema_version")
    body = _require_exact_object(
        value["taskpack"],
        {
            "taskpack_id",
            "instance_id",
            "status",
            "execution_mode",
            "live_authoring",
            "selection_authority_sha256",
            "visible_inputs",
            "shared_budget",
            "tasks",
            "policy",
        },
        "direct taskpack body",
    )
    if value["taskpack_sha256"] != canonical_json_sha256(body):
        raise Phase3PreparationError(
            "taskpack_sha256 does not bind canonical direct taskpack content"
        )
    if (
        body["status"] != "frozen"
        or body["execution_mode"] != "agentteam_direct"
        or body["live_authoring"] is not False
    ):
        raise Phase3PreparationError("direct taskpack must be frozen before gold")
    if not isinstance(body["taskpack_id"], str) or _SAFE_ID.fullmatch(
        body["taskpack_id"]
    ) is None:
        raise Phase3PreparationError("direct taskpack_id must be a safe identifier")
    visible = _validate_preregistration_definition(
        "shared_visible_inputs",
        body["visible_inputs"],
    )
    _validate_preregistration_definition("budget", body["shared_budget"])
    expected_tasks = [
        {
            "task_id": "implementation",
            "objective": visible["task"]["goal"],
            "constraints": visible["task"]["constraints"],
            "non_goals": visible["task"]["non_goals"],
            "acceptance_commands": visible["task"]["acceptance_commands"],
        }
    ]
    if body["tasks"] != expected_tasks:
        raise Phase3PreparationError(
            "direct taskpack task does not match the runtime-visible task"
        )
    if body["policy"] != {
        "gold_visibility": "evaluator_only",
        "cross_mode_artifact_access": "forbidden",
    }:
        raise Phase3PreparationError("direct taskpack visibility policy changed")
    if instance_id is not None and body["instance_id"] != instance_id:
        raise Phase3PreparationError("direct taskpack instance_id mismatch")
    if selection_authority is not None:
        authority = validate_phase3_selection_authority(selection_authority)
        _require_selected_instance(authority, body["instance_id"])
        if body["selection_authority_sha256"] != authority[
            "selection_authority_sha256"
        ]:
            raise Phase3PreparationError(
                "direct taskpack does not bind the selection authority"
            )
    if visible_inputs is not None and canonical_json_bytes(
        visible
    ) != canonical_json_bytes(
        _validate_preregistration_definition(
            "shared_visible_inputs",
            visible_inputs,
        )
    ):
        raise Phase3PreparationError("direct taskpack visible input mismatch")
    if shared_budget is not None and canonical_json_bytes(
        body["shared_budget"]
    ) != canonical_json_bytes(
        _validate_preregistration_definition("budget", shared_budget)
    ):
        raise Phase3PreparationError("direct taskpack budget mismatch")
    return value


def build_phase3_instance_preregistration(
    *,
    instance_id,
    selection_authority,
    decisions,
    visible_inputs,
    direct_taskpack,
    shared_budget,
    research_authority_sha256,
    mode_order,
):
    """Build one immutable v1 preregistration for exactly one instance."""

    authority = validate_phase3_selection_authority(
        selection_authority,
        decisions=decisions,
    )
    decision = validate_phase3_pilot_decisions(decisions)
    _require_selected_instance(authority, instance_id)
    visible = _validate_preregistration_definition(
        "shared_visible_inputs",
        visible_inputs,
    )
    budget = _validate_approved_budget(shared_budget, decision)
    taskpack = validate_phase3_instance_direct_taskpack(
        direct_taskpack,
        instance_id=instance_id,
        selection_authority=authority,
        visible_inputs=visible,
        shared_budget=budget,
    )
    try:
        preregistration = build_benchmark_preregistration(
            research_authority_sha256=research_authority_sha256,
            selection_sha256=authority["selection"]["selection_sha256"],
            ordered_instance_ids=[instance_id],
            shared_visible_inputs=visible,
            shared_budget=budget,
            direct_taskpack_sha256_by_instance={
                instance_id: taskpack["taskpack_sha256"]
            },
            non_inferiority_margin=decision["thresholds"][
                "non_inferiority_margin"
            ],
            max_token_cost_ratio=decision["thresholds"][
                "max_token_cost_ratio"
            ],
            max_wall_time_cost_ratio=decision["thresholds"][
                "max_wall_time_cost_ratio"
            ],
            preselected_secondary_benefit_metric=decision["thresholds"][
                "preselected_secondary_benefit_metric"
            ],
            mode_order=mode_order,
        )
    except BenchmarkPreregistrationError as exc:
        raise Phase3PreparationError(str(exc)) from exc
    return validate_benchmark_preregistration(preregistration)


build_per_instance_preregistration = build_phase3_instance_preregistration


def materialize_phase3_instance_authorities(
    *,
    selection_authority,
    decisions,
    task_inputs_by_instance,
    execution_profile,
    shared_budget,
    research_authority_sha256,
    mode_order,
):
    """Materialize visible inputs, direct taskpacks, and preregistrations.

    The three maps cover the frozen selection exactly and preserve selection
    order in their insertion order. Distinct instances are forbidden from
    reusing one canonical visible-input authority.
    """

    authority = validate_phase3_selection_authority(
        selection_authority,
        decisions=decisions,
    )
    decision = validate_phase3_pilot_decisions(decisions)
    task_inputs = _json_object_snapshot(task_inputs_by_instance)
    if task_inputs is None:
        raise Phase3PreparationError("task_inputs_by_instance must be an object")
    ordered_ids = authority["selection"]["ordered_instance_ids"]
    if set(task_inputs) != set(ordered_ids):
        raise Phase3PreparationError(
            "per-instance task inputs must cover the selection exactly"
        )
    budget = _validate_approved_budget(shared_budget, decision)
    visible_by_instance = {}
    taskpacks_by_instance = {}
    preregistrations_by_instance = {}
    visible_digests = set()
    for instance_id in ordered_ids:
        visible = build_phase3_instance_visible_input(
            instance_id=instance_id,
            selection_authority=authority,
            decisions=decision,
            task_input=task_inputs[instance_id],
            execution_profile=execution_profile,
        )
        visible_digest = canonical_json_sha256(visible)
        if visible_digest in visible_digests:
            raise Phase3PreparationError(
                "different instances cannot reuse shared_visible_inputs authority"
            )
        visible_digests.add(visible_digest)
        taskpack = build_phase3_instance_direct_taskpack(
            instance_id=instance_id,
            selection_authority=authority,
            decisions=decision,
            visible_inputs=visible,
            shared_budget=budget,
        )
        preregistration = build_phase3_instance_preregistration(
            instance_id=instance_id,
            selection_authority=authority,
            decisions=decision,
            visible_inputs=visible,
            direct_taskpack=taskpack,
            shared_budget=budget,
            research_authority_sha256=research_authority_sha256,
            mode_order=mode_order,
        )
        visible_by_instance[instance_id] = visible
        taskpacks_by_instance[instance_id] = taskpack
        preregistrations_by_instance[instance_id] = preregistration
    materialization = {
        "schema_version": INSTANCE_MATERIALIZATION_SCHEMA_VERSION,
        "selection_authority_sha256": authority[
            "selection_authority_sha256"
        ],
        "ordered_instance_ids": list(ordered_ids),
        "visible_inputs_by_instance": visible_by_instance,
        "direct_taskpacks_by_instance": taskpacks_by_instance,
        "preregistrations_by_instance": preregistrations_by_instance,
    }
    materialization["materialization_sha256"] = canonical_json_sha256(
        materialization
    )
    return materialization


materialize_per_instance_preregistrations = materialize_phase3_instance_authorities


def fixed_readiness_binding():
    """Return the accepted Phase 3A readiness authority binding."""

    return json.loads(canonical_json_bytes(READINESS_BINDING).decode("utf-8"))


def validate_phase3_instance_materialization(
    materialization,
    *,
    selection_authority,
):
    """Validate all per-instance authorities before aggregate materialization."""

    authority = validate_phase3_selection_authority(selection_authority)
    value = _require_exact_object(
        materialization,
        {
            "schema_version",
            "selection_authority_sha256",
            "ordered_instance_ids",
            "visible_inputs_by_instance",
            "direct_taskpacks_by_instance",
            "preregistrations_by_instance",
            "materialization_sha256",
        },
        "instance materialization",
    )
    if value["schema_version"] != INSTANCE_MATERIALIZATION_SCHEMA_VERSION:
        raise Phase3PreparationError(
            "unsupported instance materialization schema_version"
        )
    digest_body = dict(value)
    digest_body.pop("materialization_sha256")
    if value["materialization_sha256"] != canonical_json_sha256(digest_body):
        raise Phase3PreparationError(
            "materialization_sha256 does not bind canonical content"
        )
    if value["selection_authority_sha256"] != authority[
        "selection_authority_sha256"
    ]:
        raise Phase3PreparationError(
            "instance materialization does not bind the selection authority"
        )
    ordered_ids = authority["selection"]["ordered_instance_ids"]
    if value["ordered_instance_ids"] != ordered_ids:
        raise Phase3PreparationError(
            "instance materialization order does not match the selection"
        )

    maps = (
        "visible_inputs_by_instance",
        "direct_taskpacks_by_instance",
        "preregistrations_by_instance",
    )
    for field in maps:
        if not isinstance(value[field], dict) or set(value[field]) != set(
            ordered_ids
        ):
            raise Phase3PreparationError(
                f"{field} must cover the selection exactly"
            )

    visible_digests = set()
    selection_sha256 = authority["selection"]["selection_sha256"]
    for instance_id in ordered_ids:
        visible = _validate_preregistration_definition(
            "shared_visible_inputs",
            value["visible_inputs_by_instance"][instance_id],
        )
        visible_digest = canonical_json_sha256(visible)
        if visible_digest in visible_digests:
            raise Phase3PreparationError(
                "different instances cannot reuse shared_visible_inputs authority"
            )
        visible_digests.add(visible_digest)
        direct = validate_phase3_instance_direct_taskpack(
            value["direct_taskpacks_by_instance"][instance_id],
            instance_id=instance_id,
            selection_authority=authority,
            visible_inputs=visible,
        )
        try:
            preregistration = validate_benchmark_preregistration(
                value["preregistrations_by_instance"][instance_id]
            )
        except BenchmarkPreregistrationError as exc:
            raise Phase3PreparationError(str(exc)) from exc
        authorization = preregistration["authorization"]
        if authorization["selection"] != {
            "selection_sha256": selection_sha256,
            "ordered_instance_ids": [instance_id],
        }:
            raise Phase3PreparationError(
                "per-instance preregistration selection binding mismatch"
            )
        if canonical_json_bytes(
            authorization["equal_input_bindings"]["shared_visible_inputs"]
        ) != canonical_json_bytes(visible):
            raise Phase3PreparationError(
                "per-instance preregistration visible input mismatch"
            )
        direct_binding = authorization["mode_controls"]["agentteam_direct"][
            "taskpack_sha256_by_instance"
        ]
        if direct_binding != {instance_id: direct["taskpack_sha256"]}:
            raise Phase3PreparationError(
                "per-instance preregistration direct taskpack mismatch"
            )
    return value


def build_phase3_aggregate_pilot_contract(
    *,
    selection_authority,
    instance_materialization,
    retry_policy,
    abort_conditions,
    readiness_binding=None,
    dataset_binding=None,
):
    """Build the sealed provider-free pilot aggregate from Phase 3B inputs.

    Readiness and dataset bindings are fixed authorities. Callers may pass
    them for explicit replay, but cannot replace or expand them.
    """

    authority = validate_phase3_selection_authority(selection_authority)
    materialization = validate_phase3_instance_materialization(
        instance_materialization,
        selection_authority=authority,
    )
    readiness = (
        fixed_readiness_binding()
        if readiness_binding is None
        else _json_object_snapshot(readiness_binding)
    )
    if readiness != fixed_readiness_binding():
        raise Phase3PreparationError(
            "readiness binding does not match accepted P3-READY authority"
        )
    dataset = (
        fixed_dataset_binding()
        if dataset_binding is None
        else _json_object_snapshot(dataset_binding)
    )
    if dataset != fixed_dataset_binding():
        raise Phase3PreparationError(
            "dataset binding does not match the fixed SWE-EVO inventory"
        )
    try:
        return build_phase3_pilot_contract(
            readiness_binding=readiness,
            dataset_binding=dataset,
            selection=authority["selection"],
            preregistrations_by_instance=materialization[
                "preregistrations_by_instance"
            ],
            retry_policy=retry_policy,
            abort_conditions=abort_conditions,
        )
    except Phase3PilotError as exc:
        raise Phase3PreparationError(str(exc)) from exc


build_aggregate_pilot_contract = build_phase3_aggregate_pilot_contract
materialize_phase3_pilot_contract = build_phase3_aggregate_pilot_contract


def build_phase3_provider_free_preflight_receipt(
    *,
    pilot_contract,
    selection_authority,
    instance_materialization,
    resource_preflight_receipt=None,
):
    """Prove aggregate consistency without creating a live-launch permit."""

    authority = validate_phase3_selection_authority(selection_authority)
    materialization = validate_phase3_instance_materialization(
        instance_materialization,
        selection_authority=authority,
    )
    try:
        pilot = validate_phase3_pilot_contract(
            pilot_contract,
            selection=authority["selection"],
            preregistrations_by_instance=materialization[
                "preregistrations_by_instance"
            ],
        )
    except Phase3PilotError as exc:
        raise Phase3PreparationError(str(exc)) from exc
    body = pilot["contract"]
    if body["readiness_binding"] != fixed_readiness_binding():
        raise Phase3PreparationError(
            "pilot contract does not bind accepted P3-READY authority"
        )
    if body["dataset_binding"] != fixed_dataset_binding():
        raise Phase3PreparationError(
            "pilot contract does not bind the fixed SWE-EVO inventory"
        )
    ceiling = body["aggregate_budget_ceiling"]
    computed_tokens = sum(
        item["maximum_total_tokens"] for item in body["instance_bindings"]
    )
    computed_wall = sum(
        item["maximum_wall_time_seconds"]
        for item in body["instance_bindings"]
    )
    resource_preflight = None
    if resource_preflight_receipt is not None:
        try:
            resource_preflight = validate_phase3_resource_preflight_receipt(
                resource_preflight_receipt
            )
        except ResourceEnvelopeError as exc:
            raise Phase3PreparationError(
                "resource preflight receipt is invalid"
            ) from exc
    verification_results = {
        "selection_binding": _preflight_check(
            {
                "selection_authority_sha256": authority[
                    "selection_authority_sha256"
                ],
                "selection_sha256": authority["selection"]["selection_sha256"],
                "ordered_instance_ids": authority["selection"][
                    "ordered_instance_ids"
                ],
            }
        ),
        "preregistration_coverage": _preflight_check(
            {
                "materialization_sha256": materialization[
                    "materialization_sha256"
                ],
                "authorization_sha256_by_instance": {
                    instance_id: preregistration["authorization_sha256"]
                    for instance_id, preregistration in materialization[
                        "preregistrations_by_instance"
                    ].items()
                },
            }
        ),
        "aggregate_contract": _preflight_check(pilot),
        "aggregate_budget": _preflight_check(
            {
                "computed_maximum_total_tokens": computed_tokens,
                "computed_maximum_wall_time_seconds": computed_wall,
                "contract_ceiling": ceiling,
            }
        ),
        "provider_absence": _preflight_check(
            {
                "live_provider_calls": 0,
                "scored_mode_executions": 0,
                "invocations_created": 0,
                "provider_status": "not_invoked",
            }
        ),
        "live_authorization_denial": _preflight_check(
            {
                "gate_id": "P3-LIVE",
                "contract_status": body["live_authorization"]["status"],
                "authorization_artifact_supplied": False,
                "permit_issued": False,
            }
        ),
    }
    if resource_preflight is not None:
        verification_results["resource_hierarchy"] = _preflight_check(
            {
                "resource_envelope_sha256": resource_preflight[
                    "binding_sha256"
                ],
                "resource_preflight_receipt_sha256": resource_preflight[
                    "receipt_sha256"
                ],
                "probe_count": len(resource_preflight["probe_records"]),
                "cleanup_complete": resource_preflight["cleanup"][
                    "cleanup_complete"
                ],
            }
        )
    verification = {
        "results": verification_results,
        "verification_sha256": canonical_json_sha256(verification_results),
    }
    receipt_body = {
        "status": "passed",
        "decision_id": PILOT_PREFLIGHT_DECISION_ID,
        "provider_free": True,
        "bindings": {
            "readiness_evidence_sha256": body["readiness_binding"][
                "evidence_sha256"
            ],
            "dataset_artifact_sha256": body["dataset_binding"][
                "artifact_sha256"
            ],
            "selection_authority_sha256": authority[
                "selection_authority_sha256"
            ],
            "selection_sha256": body["selection"]["selection_sha256"],
            "materialization_sha256": materialization[
                "materialization_sha256"
            ],
            "pilot_contract_sha256": pilot["contract_sha256"],
        },
        "instance_reconciliation": {
            "ordered_instance_ids": body["selection"]["ordered_instance_ids"],
            "selected_instance_count": len(body["selection"]["ordered_instance_ids"]),
            "preregistration_count": len(body["instance_bindings"]),
        },
        "aggregate_budget_reconciliation": {
            "maximum_total_tokens": ceiling["maximum_total_tokens"],
            "maximum_wall_time_seconds": ceiling["maximum_wall_time_seconds"],
            "max_inflight_model_invocations": ceiling[
                "max_inflight_model_invocations"
            ],
            "computed_maximum_total_tokens": computed_tokens,
            "computed_maximum_wall_time_seconds": computed_wall,
        },
        "usage_reconciliation": {
            "live_provider_calls": 0,
            "scored_mode_executions": 0,
            "invocations_created": 0,
            "provider_status": "not_invoked",
            "terminal_usage_records": 0,
            "terminal_usage_status": "not_applicable",
            "token_totals": None,
        },
        "live_authorization_reconciliation": {
            "gate_id": "P3-LIVE",
            "required": True,
            "contract_status": "not_authorized",
            "authorization_artifact_supplied": False,
            "permit_issued": False,
            "admission_status": "denied",
            "denial_reason": "mandatory_operator_review_and_epoch_authorization_required",
        },
        "verification": verification,
    }
    if resource_preflight is not None:
        receipt_body["bindings"].update(
            {
                "resource_envelope_sha256": resource_preflight[
                    "binding_sha256"
                ],
                "resource_preflight_receipt_sha256": resource_preflight[
                    "receipt_sha256"
                ],
            }
        )
    receipt = {
        "schema_version": (
            PILOT_PREFLIGHT_V2_SCHEMA_VERSION
            if resource_preflight is not None
            else PILOT_PREFLIGHT_SCHEMA_VERSION
        ),
        **receipt_body,
    }
    receipt["receipt_sha256"] = canonical_json_sha256(receipt)
    return validate_phase3_provider_free_preflight_receipt(
        receipt,
        pilot_contract=pilot,
        selection_authority=authority,
        instance_materialization=materialization,
        resource_preflight_receipt=resource_preflight,
    )


build_provider_free_preflight_receipt = build_phase3_provider_free_preflight_receipt


def validate_phase3_provider_free_preflight_receipt(
    receipt,
    *,
    pilot_contract=None,
    selection_authority=None,
    instance_materialization=None,
    resource_preflight_receipt=None,
):
    """Validate a provider-absence receipt and optional source authorities."""

    value = _json_object_snapshot(receipt)
    if value is None:
        raise Phase3PreparationError("pilot preflight receipt must be a JSON object")
    schema = json.loads(
        schema_path("phase3_pilot_preflight.schema.json").read_text(
            encoding="utf-8"
        )
    )
    if value.get("schema_version") == PILOT_PREFLIGHT_V2_SCHEMA_VERSION:
        schema["properties"]["schema_version"]["const"] = (
            PILOT_PREFLIGHT_V2_SCHEMA_VERSION
        )
        bindings = schema["properties"]["bindings"]
        bindings["required"].extend(
            [
                "resource_envelope_sha256",
                "resource_preflight_receipt_sha256",
            ]
        )
        bindings["properties"].update(
            {
                "resource_envelope_sha256": {"$ref": "#/$defs/sha256"},
                "resource_preflight_receipt_sha256": {
                    "$ref": "#/$defs/sha256"
                },
            }
        )
        results = schema["properties"]["verification"]["properties"][
            "results"
        ]
        results["required"].append("resource_hierarchy")
        results["properties"]["resource_hierarchy"] = {
            "$ref": "#/$defs/check"
        }
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        raise Phase3PreparationError(
            f"pilot preflight schema validation failed at {location or '<root>'}: "
            f"{errors[0].message}"
        )
    digest_body = dict(value)
    digest_body.pop("receipt_sha256")
    if value["receipt_sha256"] != canonical_json_sha256(digest_body):
        raise Phase3PreparationError(
            "receipt_sha256 does not bind canonical preflight content"
        )
    results = value["verification"]["results"]
    if value["verification"]["verification_sha256"] != canonical_json_sha256(
        results
    ):
        raise Phase3PreparationError(
            "verification_sha256 does not bind preflight results"
        )
    if value["bindings"]["readiness_evidence_sha256"] != READINESS_BINDING[
        "evidence_sha256"
    ]:
        raise Phase3PreparationError(
            "preflight receipt readiness evidence binding changed"
        )
    if value["bindings"]["dataset_artifact_sha256"] != (
        FIXED_DATASET_ARTIFACT_SHA256
    ):
        raise Phase3PreparationError(
            "preflight receipt dataset artifact binding changed"
        )
    instances = value["instance_reconciliation"]
    if instances["selected_instance_count"] != len(
        instances["ordered_instance_ids"]
    ) or instances["preregistration_count"] != instances[
        "selected_instance_count"
    ]:
        raise Phase3PreparationError(
            "preflight instance reconciliation is inconsistent"
        )
    budget = value["aggregate_budget_reconciliation"]
    if budget["maximum_total_tokens"] != budget[
        "computed_maximum_total_tokens"
    ] or budget["maximum_wall_time_seconds"] != budget[
        "computed_maximum_wall_time_seconds"
    ]:
        raise Phase3PreparationError(
            "preflight aggregate budget reconciliation is inconsistent"
        )
    if pilot_contract is not None:
        try:
            pilot = validate_phase3_pilot_contract(pilot_contract)
        except Phase3PilotError as exc:
            raise Phase3PreparationError(str(exc)) from exc
        if value["bindings"]["pilot_contract_sha256"] != pilot[
            "contract_sha256"
        ]:
            raise Phase3PreparationError(
                "preflight receipt does not bind the pilot contract"
            )
        pilot_body = pilot["contract"]
        if value["instance_reconciliation"]["ordered_instance_ids"] != (
            pilot_body["selection"]["ordered_instance_ids"]
        ):
            raise Phase3PreparationError(
                "preflight receipt instance order does not match the pilot contract"
            )
        ceiling = pilot_body["aggregate_budget_ceiling"]
        recorded_budget = value["aggregate_budget_reconciliation"]
        if (
            recorded_budget["maximum_total_tokens"]
            != ceiling["maximum_total_tokens"]
            or recorded_budget["maximum_wall_time_seconds"]
            != ceiling["maximum_wall_time_seconds"]
            or recorded_budget["max_inflight_model_invocations"]
            != ceiling["max_inflight_model_invocations"]
        ):
            raise Phase3PreparationError(
                "preflight receipt budget does not match the pilot contract"
            )
    if selection_authority is not None:
        authority = validate_phase3_selection_authority(selection_authority)
        if value["bindings"]["selection_authority_sha256"] != authority[
            "selection_authority_sha256"
        ]:
            raise Phase3PreparationError(
                "preflight receipt does not bind the selection authority"
            )
        if value["bindings"]["selection_sha256"] != authority["selection"][
            "selection_sha256"
        ] or value["instance_reconciliation"]["ordered_instance_ids"] != (
            authority["selection"]["ordered_instance_ids"]
        ):
            raise Phase3PreparationError(
                "preflight receipt does not match the frozen selection"
            )
    if instance_materialization is not None:
        if selection_authority is None:
            raise Phase3PreparationError(
                "selection authority is required with instance materialization"
            )
        materialization = validate_phase3_instance_materialization(
            instance_materialization,
            selection_authority=selection_authority,
        )
        if value["bindings"]["materialization_sha256"] != materialization[
            "materialization_sha256"
        ]:
            raise Phase3PreparationError(
                "preflight receipt does not bind instance materialization"
            )
    if value["schema_version"] == PILOT_PREFLIGHT_V2_SCHEMA_VERSION:
        expected_binding_sha256 = canonical_resource_envelope_sha256(
            approved_phase3_resource_envelope_binding()
        )
        if value["bindings"]["resource_envelope_sha256"] != (
            expected_binding_sha256
        ):
            raise Phase3PreparationError(
                "preflight receipt resource envelope binding changed"
            )
        if resource_preflight_receipt is not None:
            try:
                resource_preflight = validate_phase3_resource_preflight_receipt(
                    resource_preflight_receipt
                )
            except ResourceEnvelopeError as exc:
                raise Phase3PreparationError(
                    "resource preflight receipt is invalid"
                ) from exc
            if (
                value["bindings"]["resource_preflight_receipt_sha256"]
                != resource_preflight["receipt_sha256"]
                or value["bindings"]["resource_envelope_sha256"]
                != resource_preflight["binding_sha256"]
            ):
                raise Phase3PreparationError(
                    "aggregate preflight does not bind resource preflight"
                )
    return value


validate_provider_free_preflight_receipt = validate_phase3_provider_free_preflight_receipt


def provider_free_preflight_receipt_bytes(receipt):
    """Return canonical bytes for a validated provider-free receipt."""

    return canonical_json_bytes(
        validate_phase3_provider_free_preflight_receipt(receipt)
    )


def _preflight_check(evidence):
    return {
        "status": "passed",
        "evidence_sha256": canonical_json_sha256(evidence),
    }


def _require_selected_instance(authority, instance_id):
    if not isinstance(instance_id, str) or not instance_id:
        raise Phase3PreparationError("instance_id must be a non-empty string")
    if instance_id not in authority["selection"]["ordered_instance_ids"]:
        raise Phase3PreparationError("instance_id is not in the frozen selection")


def _require_exact_object(value, fields, label):
    snapshot = _json_object_snapshot(value)
    if snapshot is None:
        raise Phase3PreparationError(f"{label} must be a JSON object")
    if set(snapshot) != set(fields):
        raise Phase3PreparationError(f"{label} fields do not match the fixed allowlist")
    return snapshot


def _validate_preregistration_definition(definition, value):
    snapshot = _json_object_snapshot(value)
    if snapshot is None:
        raise Phase3PreparationError(f"{definition} must be a JSON object")
    schema = json.loads(
        schema_path("benchmark_preregistration.schema.json").read_text(
            encoding="utf-8"
        )
    )
    definition_schema = {
        "$ref": f"#/$defs/{definition}",
        "$defs": schema["$defs"],
    }
    errors = sorted(
        Draft202012Validator(definition_schema).iter_errors(snapshot),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        raise Phase3PreparationError(
            f"{definition} validation failed at {location or '<root>'}: "
            f"{errors[0].message}"
        )
    return snapshot


def _validate_approved_budget(shared_budget, decisions):
    budget = _validate_preregistration_definition("budget", shared_budget)
    approved = decisions["execution"]["per_instance_budget"]
    if budget["max_total_tokens"] != approved["max_total_tokens"]:
        raise Phase3PreparationError(
            "shared budget max_total_tokens does not match the approved decision"
        )
    if budget["max_wall_time_seconds"] != approved[
        "max_wall_time_seconds"
    ]:
        raise Phase3PreparationError(
            "shared budget max_wall_time_seconds does not match the approved decision"
        )
    return budget


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


def _file_sha256(path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Phase3PreparationError(
            f"fixed Arrow artifact cannot be read: {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _read_swe_evo_arrow_rows(path):
    try:
        import pyarrow as pa
        import pyarrow.ipc as ipc
    except (ImportError, ModuleNotFoundError) as exc:
        raise Phase3PreparationError(
            "pyarrow is required for trusted Arrow projection; "
            "install it in the evaluator environment before preparation"
        ) from exc

    try:
        with path.open("rb") as source:
            try:
                reader = ipc.open_stream(source)
                table = reader.read_all()
            except pa.ArrowInvalid:
                source.seek(0)
                reader = ipc.open_file(source)
                table = reader.read_all()
    except Exception as exc:
        raise Phase3PreparationError(
            f"fixed Arrow artifact could not be decoded: {exc}"
        ) from exc

    column_names = set(table.column_names)
    repository_fields = [
        field for field in ("repo", "repository") if field in column_names
    ]
    if "instance_id" not in column_names or "PRs" not in column_names:
        raise Phase3PreparationError(
            "fixed Arrow schema must contain instance_id and PRs"
        )
    if len(repository_fields) != 1:
        raise Phase3PreparationError(
            "fixed Arrow schema must contain exactly one repository identifier field"
        )

    # Drop evaluator-only columns before materializing Arrow values in Python.
    projected = table.select(
        ["instance_id", repository_fields[0], "PRs"]
    )
    return projected.to_pylist()


def _project_swe_evo_complexity_rows(rows):
    if not isinstance(rows, list):
        raise Phase3PreparationError("decoded Arrow rows must be an array")
    if len(rows) != FIXED_DATASET_ROW_COUNT:
        raise Phase3PreparationError(
            "fixed Arrow row count mismatch: "
            f"expected {FIXED_DATASET_ROW_COUNT}, got {len(rows)}"
        )

    projected = []
    seen_ids = set()
    populations = {"high": 0, "low": 0, "medium": 0}
    for index, row in enumerate(rows):
        label = f"Arrow row {index}"
        if not isinstance(row, dict):
            raise Phase3PreparationError(f"{label} must be an object")
        if "instance_id" not in row or "PRs" not in row:
            raise Phase3PreparationError(
                f"{label} must contain instance_id and PRs"
            )
        instance_id = row["instance_id"]
        if not isinstance(instance_id, str) or _SAFE_ID.fullmatch(instance_id) is None:
            raise Phase3PreparationError(
                f"{label}.instance_id must be a safe identifier"
            )
        if instance_id in seen_ids:
            raise Phase3PreparationError(
                f"fixed Arrow artifact contains duplicate instance_id: {instance_id}"
            )
        seen_ids.add(instance_id)

        repository_fields = [
            field for field in ("repo", "repository") if field in row
        ]
        if len(repository_fields) != 1:
            raise Phase3PreparationError(
                f"{label} must contain exactly one repository identifier field"
            )
        repository = row[repository_fields[0]]
        if (
            not isinstance(repository, str)
            or not repository
            or repository != repository.strip()
        ):
            raise Phase3PreparationError(
                f"{label} repository identifier must be a non-empty trimmed string"
            )

        prs = row["PRs"]
        if not isinstance(prs, list):
            raise Phase3PreparationError(f"{label}.PRs must be an Arrow list")
        count = len(prs)
        stratum = _complexity_stratum_for_pr_count(count)
        populations[stratum] += 1
        projected.append(
            {
                "instance_id": instance_id,
                "repository": repository,
                "complexity_stratum": stratum,
                "language": "python",
                "tags": [],
            }
        )
    return projected, populations


def _complexity_stratum_for_pr_count(count):
    if count <= 2:
        return "low"
    if count <= 6:
        return "medium"
    return "high"


def _json_object_snapshot(value):
    if not isinstance(value, dict):
        return None
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (ExperimentContractError, json.JSONDecodeError):
        return None

"""Provider-free Phase 3B inventory binding and routing-manifest conversion."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from .benchmark_adapter import BenchmarkAdapterError, validate_swe_evo_metadata
from .experiment_contract import canonical_json_sha256, schema_path

FIXED_SOURCE_COMMIT = "9b83d5af943ba7a17567336f5b18239f73960219"
FIXED_DATASET_ARTIFACT_SHA256 = "74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520"
FIXED_DATASET_SPLIT = "test"
FIXED_DATASET_ROW_COUNT = 48
FIXED_DATASET_ARTIFACT_PATH = "hf_out/hf_dataset/test/data-00000-of-00001.arrow"
FIXED_SOURCE_REPOSITORY = "https://github.com/SWE-EVO/SWE-EVO.git"
ROUTING_MANIFEST_SCHEMA_VERSION = "phase3_routing_manifest.v1"
ROUTING_FIELDS = (
    "instance_id",
    "repository",
    "complexity_stratum",
    "language",
    "tags",
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

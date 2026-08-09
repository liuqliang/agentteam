"""Gold-blind SWE-EVO metadata validation and deterministic selection.

The adapter is intentionally a pure input-boundary module.  It accepts only
allowlisted, non-gold metadata, applies explicit filters, and derives ordering
from SHA-256 rather than process-global pseudo-random state.
"""

from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator

from .experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    schema_path,
)


SWE_EVO_METADATA_SCHEMA_VERSION = "swe_evo_metadata.v1"
SELECTION_SCHEMA_VERSION = "benchmark_instance_selection.v1"
BENCHMARK_ID = "swe_evo"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FORBIDDEN_FIELD_PARTS = ("gold", "outcome", "result", "score", "success")
_FORBIDDEN_FIELDS = {
    "patch",
    "prior_run",
    "prior_runs",
    "reference_answer",
    "reference_patch",
}
_METADATA_FIELDS = {
    "schema_version",
    "benchmark",
    "metadata_revision",
    "instances",
}
_INSTANCE_FIELDS = {
    "instance_id",
    "repository",
    "complexity_stratum",
    "language",
    "tags",
}
_FILTER_FIELDS = {
    "repositories",
    "languages",
    "required_tags",
    "excluded_instance_ids",
}


class BenchmarkAdapterError(ExperimentContractError):
    """Raised when benchmark metadata or selection authority is unsafe."""


def validate_swe_evo_metadata(metadata, *, expected_revision=None):
    """Return a detached snapshot of allowlisted SWE-EVO routing metadata.

    Task descriptions, patches, evaluator data, scores, and prior outcomes are
    deliberately outside this contract.  Unknown fields fail closed instead
    of becoming an accidental side channel.
    """

    value = _snapshot_object(metadata, "SWE-EVO metadata")
    _reject_forbidden_fields(value)
    _require_exact_fields(value, _METADATA_FIELDS, "SWE-EVO metadata")

    if value["schema_version"] != SWE_EVO_METADATA_SCHEMA_VERSION:
        raise BenchmarkAdapterError("unsupported SWE-EVO metadata schema_version")
    if value["benchmark"] != BENCHMARK_ID:
        raise BenchmarkAdapterError("metadata benchmark must be 'swe_evo'")
    revision = _require_safe_id(value["metadata_revision"], "metadata_revision")
    if expected_revision is not None and revision != expected_revision:
        raise BenchmarkAdapterError(
            "metadata_revision does not match the fixed expected revision"
        )

    instances = value["instances"]
    if not isinstance(instances, list) or not instances:
        raise BenchmarkAdapterError("metadata.instances must be a non-empty array")
    seen_ids = set()
    for index, instance in enumerate(instances):
        label = f"metadata.instances[{index}]"
        if not isinstance(instance, dict):
            raise BenchmarkAdapterError(f"{label} must be an object")
        _require_exact_fields(instance, _INSTANCE_FIELDS, label)
        instance_id = _require_safe_id(instance["instance_id"], f"{label}.instance_id")
        if instance_id in seen_ids:
            raise BenchmarkAdapterError(f"duplicate instance_id: {instance_id}")
        seen_ids.add(instance_id)
        _require_non_empty_string(instance["repository"], f"{label}.repository")
        _require_safe_id(
            instance["complexity_stratum"], f"{label}.complexity_stratum"
        )
        _require_safe_id(instance["language"], f"{label}.language")
        _validate_safe_id_array(instance["tags"], f"{label}.tags")
    return value


def select_complexity_stratified_instances(
    metadata,
    *,
    metadata_revision,
    filters,
    seed,
    stratum_quotas,
):
    """Build a digest-bound, deterministically ordered selection artifact."""

    metadata = validate_swe_evo_metadata(
        metadata,
        expected_revision=metadata_revision,
    )
    normalized_filters = _normalize_filters(filters)
    normalized_quotas = _normalize_stratum_quotas(stratum_quotas)
    seed = _normalize_seed(seed)

    eligible = [
        instance
        for instance in metadata["instances"]
        if _matches_filters(instance, normalized_filters)
    ]
    eligible_by_stratum = {
        stratum: [
            instance
            for instance in eligible
            if instance["complexity_stratum"] == stratum
        ]
        for stratum in normalized_quotas
    }
    eligible_counts = {
        stratum: len(instances)
        for stratum, instances in eligible_by_stratum.items()
    }
    for stratum, quota in normalized_quotas.items():
        if eligible_counts[stratum] < quota:
            raise BenchmarkAdapterError(
                f"complexity stratum {stratum!r} has {eligible_counts[stratum]} "
                f"eligible instances but quota requires {quota}"
            )

    ordered_instances = []
    for stratum in sorted(normalized_quotas):
        ranked = sorted(
            eligible_by_stratum[stratum],
            key=lambda instance: (
                _selection_rank(seed, stratum, instance["instance_id"]),
                instance["instance_id"],
            ),
        )
        ordered_instances.extend(
            {
                "instance_id": instance["instance_id"],
                "complexity_stratum": stratum,
            }
            for instance in ranked[: normalized_quotas[stratum]]
        )

    selection = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "metadata_revision": metadata_revision,
        "metadata_sha256": canonical_json_sha256(metadata),
        "filters": normalized_filters,
        "seed": seed,
        "stratum_quotas": normalized_quotas,
        "eligible_counts_by_stratum": eligible_counts,
        "ordered_instance_ids": [
            item["instance_id"] for item in ordered_instances
        ],
        "ordered_instances": ordered_instances,
    }
    selection["selection_sha256"] = selection_digest(selection)
    validate_benchmark_instance_selection(selection)
    return selection


def build_benchmark_instance_selection(*args, **kwargs):
    """Compatibility entry point with a name matching the emitted artifact."""

    return select_complexity_stratified_instances(*args, **kwargs)


def selection_digest(selection):
    """Return the canonical digest of every selection field except itself."""

    value = _snapshot_object(selection, "benchmark instance selection")
    value.pop("selection_sha256", None)
    return canonical_json_sha256(value)


def validate_benchmark_instance_selection(selection, *, metadata=None):
    """Validate schema, digest, selection invariants, and optional replay."""

    value = _snapshot_object(selection, "benchmark instance selection")
    schema = json.loads(
        schema_path("benchmark_instance_selection.schema.json").read_text(
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
        raise BenchmarkAdapterError(
            f"benchmark instance selection schema validation failed at "
            f"{location}: {first.message}"
        )
    if value["selection_sha256"] != selection_digest(value):
        raise BenchmarkAdapterError("selection_sha256 does not match canonical content")

    ordered_ids = [item["instance_id"] for item in value["ordered_instances"]]
    if value["ordered_instance_ids"] != ordered_ids:
        raise BenchmarkAdapterError(
            "ordered_instance_ids must match ordered_instances exactly"
        )
    quota_strata = set(value["stratum_quotas"])
    if set(value["eligible_counts_by_stratum"]) != quota_strata:
        raise BenchmarkAdapterError(
            "eligible_counts_by_stratum must cover exactly the quota strata"
        )
    selected_counts = {stratum: 0 for stratum in quota_strata}
    for item in value["ordered_instances"]:
        stratum = item["complexity_stratum"]
        if stratum not in selected_counts:
            raise BenchmarkAdapterError("selected instance uses an unbound stratum")
        selected_counts[stratum] += 1
    if selected_counts != value["stratum_quotas"]:
        raise BenchmarkAdapterError("selected counts do not match stratum_quotas")
    for stratum, count in value["eligible_counts_by_stratum"].items():
        if count < value["stratum_quotas"][stratum]:
            raise BenchmarkAdapterError("eligible count is smaller than its quota")

    if metadata is not None:
        replay = select_complexity_stratified_instances(
            metadata,
            metadata_revision=value["metadata_revision"],
            filters=value["filters"],
            seed=value["seed"],
            stratum_quotas=value["stratum_quotas"],
        )
        if replay != value:
            raise BenchmarkAdapterError(
                "selection does not match deterministic replay from metadata"
            )
    return value


def _normalize_filters(filters):
    value = _snapshot_object(filters, "selection filters")
    _reject_forbidden_fields(value)
    _require_exact_fields(value, _FILTER_FIELDS, "selection filters")
    normalized = {}
    for field in sorted(_FILTER_FIELDS):
        items = value[field]
        if not isinstance(items, list):
            raise BenchmarkAdapterError(f"selection filters.{field} must be an array")
        if field == "repositories":
            for index, item in enumerate(items):
                _require_non_empty_string(item, f"selection filters.{field}[{index}]")
        else:
            for index, item in enumerate(items):
                _require_safe_id(item, f"selection filters.{field}[{index}]")
        if len(items) != len(set(items)):
            raise BenchmarkAdapterError(
                f"selection filters.{field} must contain unique values"
            )
        normalized[field] = sorted(items)
    return normalized


def _normalize_stratum_quotas(stratum_quotas):
    value = _snapshot_object(stratum_quotas, "stratum quotas")
    if not value:
        raise BenchmarkAdapterError("stratum quotas must not be empty")
    normalized = {}
    for stratum, quota in value.items():
        _require_safe_id(stratum, "stratum quota key")
        if not isinstance(quota, int) or isinstance(quota, bool) or quota < 1:
            raise BenchmarkAdapterError("every stratum quota must be a positive integer")
        normalized[stratum] = quota
    return {key: normalized[key] for key in sorted(normalized)}


def _normalize_seed(seed):
    if (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or seed < 0
        or seed > 9223372036854775807
    ):
        raise BenchmarkAdapterError("seed must be a non-negative signed 64-bit integer")
    return seed


def _matches_filters(instance, filters):
    if filters["repositories"] and instance["repository"] not in filters["repositories"]:
        return False
    if filters["languages"] and instance["language"] not in filters["languages"]:
        return False
    if instance["instance_id"] in filters["excluded_instance_ids"]:
        return False
    return set(filters["required_tags"]).issubset(instance["tags"])


def _selection_rank(seed, stratum, instance_id):
    return canonical_json_sha256(
        {
            "seed": seed,
            "complexity_stratum": stratum,
            "instance_id": instance_id,
        }
    )


def _snapshot_object(value, label):
    if not isinstance(value, dict):
        raise BenchmarkAdapterError(f"{label} must be a JSON object")
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (ExperimentContractError, json.JSONDecodeError) as exc:
        raise BenchmarkAdapterError(f"{label} must contain canonical JSON values") from exc


def _reject_forbidden_fields(value, path="<root>"):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_FIELDS or any(
                part in normalized for part in _FORBIDDEN_FIELD_PARTS
            ):
                raise BenchmarkAdapterError(
                    f"forbidden gold or prior-outcome field at {path}.{key}"
                )
            _reject_forbidden_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_fields(item, f"{path}[{index}]")


def _require_exact_fields(value, fields, label):
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        raise BenchmarkAdapterError(
            f"{label} fields do not match allowlist; missing={missing}, unknown={unknown}"
        )


def _require_safe_id(value, label):
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise BenchmarkAdapterError(f"{label} must be a safe identifier")
    return value


def _require_non_empty_string(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise BenchmarkAdapterError(f"{label} must be a non-empty trimmed string")
    return value


def _validate_safe_id_array(value, label):
    if not isinstance(value, list):
        raise BenchmarkAdapterError(f"{label} must be an array")
    for index, item in enumerate(value):
        _require_safe_id(item, f"{label}[{index}]")
    if len(value) != len(set(value)):
        raise BenchmarkAdapterError(f"{label} must contain unique values")

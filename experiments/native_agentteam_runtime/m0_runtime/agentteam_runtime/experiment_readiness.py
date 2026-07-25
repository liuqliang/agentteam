import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


READINESS_SCHEMA_VERSION = "p0_experiment_readiness.v1"
EXPERIMENT_MANIFEST_SCHEMA_VERSION = "agentteam_experiment_manifest.v1"
P0_CAPABILITY_IDS = (
    "invocation_level_real_usage",
    "three_mode_experiment_harness",
    "immutable_experiment_manifest",
    "clean_reset_and_blind_gold_isolation",
    "actual_budget_enforcement",
    "operator_action_ledger",
    "machine_readable_result_bundle",
)


class ExperimentReadinessError(RuntimeError):
    pass


def packaged_readiness_record_path():
    return Path(__file__).resolve().parent / "data" / "p0_experiment_readiness.v1.json"


def readiness_schema_path():
    return (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "p0_experiment_readiness.schema.json"
    )


def experiment_manifest_schema_path():
    return (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "experiment_manifest.schema.json"
    )


def build_p0_readiness_summary(record_path=None):
    path = Path(record_path or packaged_readiness_record_path()).resolve()
    record = _read_json_object(path, "P0 readiness record")
    _validate_schema(
        record,
        readiness_schema_path(),
        "P0 readiness record",
    )
    capabilities = record["capabilities"]
    capability_ids = [item["capability_id"] for item in capabilities]
    if len(set(capability_ids)) != len(capability_ids):
        raise ExperimentReadinessError("P0 readiness record has duplicate capability IDs")
    expected = set(P0_CAPABILITY_IDS)
    actual = set(capability_ids)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ExperimentReadinessError(
            "P0 readiness capability inventory mismatch"
            f"; missing={missing}; unknown={unknown}"
        )
    all_passed = all(item["status"] == "passed" for item in capabilities)
    expected_overall = "passed" if all_passed else "blocked"
    if record["overall_status"] != expected_overall:
        raise ExperimentReadinessError(
            "P0 readiness overall status disagrees with capability states"
        )
    if record["pilot_authorized"] is not all_passed:
        raise ExperimentReadinessError(
            "P0 readiness pilot authorization disagrees with capability states"
        )
    counts = {
        status: sum(item["status"] == status for item in capabilities)
        for status in ("passed", "partial", "missing")
    }
    blockers = [
        {
            "capability_id": item["capability_id"],
            "status": item["status"],
            "reason": item["reason"],
        }
        for item in capabilities
        if item["status"] != "passed"
    ]
    return {
        "readiness_status": record["overall_status"],
        "pilot_authorized": record["pilot_authorized"],
        "schema_version": record["schema_version"],
        "record_id": record["record_id"],
        "record_path": str(path),
        "record_sha256": canonical_json_sha256(record),
        "updated_at": record["updated_at"],
        "next_required_phase": record["next_required_phase"],
        "capability_count": len(capabilities),
        "capability_counts": counts,
        "capabilities": capabilities,
        "blockers": blockers,
    }


def validate_experiment_manifest(manifest_path):
    path = Path(manifest_path).expanduser().resolve()
    manifest = _read_json_object(path, "experiment manifest")
    _validate_schema(
        manifest,
        experiment_manifest_schema_path(),
        "experiment manifest",
    )
    return {
        "manifest_status": "accepted",
        "manifest_path": str(path),
        "manifest_sha256": canonical_json_sha256(manifest),
        "schema_version": manifest["schema_version"],
        "experiment_id": manifest["experiment_id"],
        "instance_id": manifest["instance_id"],
        "mode": manifest["mode"],
        "repository_commit": manifest["repository"]["commit"],
        "readiness_record_sha256": manifest["readiness_binding"]["record_sha256"],
        "manifest": manifest,
    }


def check_pilot_authorization(manifest_path):
    return _check_pilot_authorization_with_record(
        manifest_path,
        packaged_readiness_record_path(),
    )


def _check_pilot_authorization_with_record(manifest_path, record_path):
    readiness = build_p0_readiness_summary(record_path)
    manifest = validate_experiment_manifest(manifest_path)
    blockers = list(readiness["blockers"])
    if (
        manifest["readiness_record_sha256"]
        != readiness["record_sha256"]
    ):
        blockers.append(
            {
                "capability_id": "readiness_record_binding",
                "status": "blocked",
                "reason": (
                    "experiment manifest readiness digest does not match "
                    "the runtime-packaged record"
                ),
            }
        )
    pilot_authorized = bool(readiness["pilot_authorized"] and not blockers)
    return {
        "pilot_authorized": pilot_authorized,
        "readiness": readiness,
        "manifest": {
            key: value
            for key, value in manifest.items()
            if key != "manifest"
        },
        "blockers": blockers,
        "provider_calls": 0,
        "target_mutations": 0,
    }


def canonical_json_sha256(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json_object(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ExperimentReadinessError(f"{label} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentReadinessError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ExperimentReadinessError(f"{label} must be a JSON object")
    return value


def _validate_schema(value, schema_path, label):
    schema = _read_json_object(schema_path, f"{label} schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise ExperimentReadinessError(
            f"{label} schema validation failed at {location}: {first.message}"
        )

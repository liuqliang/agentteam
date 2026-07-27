import hashlib
import json
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


READINESS_SCHEMA_VERSION = "p0_experiment_readiness.v1"
EXPERIMENT_MANIFEST_SCHEMA_VERSION = "agentteam_experiment_manifest.v1"
PHASE1_COMPLETION_PROMOTION_SCHEMA_VERSION = "phase1_completion_promotion.v1"
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


def phase1_completion_promotion_path():
    return (
        Path(__file__).resolve().parents[2]
        / "implementation_artifacts"
        / "acceptance"
        / "phase1-completion-promotion.v1.json"
    )


def phase1_completion_promotion_schema_path():
    return (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "phase1_completion_promotion.schema.json"
    )


def validate_phase1_completion_promotion(receipt_path=None):
    receipt_path = Path(
        receipt_path or phase1_completion_promotion_path()
    ).resolve()
    receipt = _read_json_object(receipt_path, "Phase 1 completion promotion")
    _validate_schema(
        receipt,
        phase1_completion_promotion_schema_path(),
        "Phase 1 completion promotion",
    )
    repository_root = Path(__file__).resolve().parents[2]
    finalization_path = _resolve_bound_artifact(
        repository_root,
        receipt["finalization_artifact"],
        "Phase 1 finalization",
    )
    approval_path = _resolve_bound_artifact(
        repository_root,
        receipt["operator_approval"],
        "Phase 1 operator approval",
    )
    epoch_path = _resolve_bound_artifact(
        repository_root,
        receipt["gate_epoch_artifact"],
        "Phase 1 gate epoch",
    )
    finalization = _read_json_object(finalization_path, "Phase 1 finalization")
    approval = _read_json_object(approval_path, "Phase 1 operator approval")
    epoch = _read_json_object(epoch_path, "Phase 1 gate epoch")
    _validate_schema(
        finalization,
        repository_root / "schemas" / "phase1_usage_finalization.schema.json",
        "Phase 1 finalization",
    )
    _validate_schema(
        approval,
        repository_root / "schemas" / "post_backlog_gate_approval.schema.json",
        "Phase 1 operator approval",
    )
    _validate_schema(
        epoch,
        repository_root / "schemas" / "post_backlog_gate_epoch.schema.json",
        "Phase 1 gate epoch",
    )
    if finalization["controller_validation_status"] != "passed":
        raise ExperimentReadinessError("Phase 1 finalization is not passed")
    if approval["decision"] != "approved":
        raise ExperimentReadinessError("Phase 1 operator approval is not approved")
    if approval["gate_id"] != "P1-06E":
        raise ExperimentReadinessError("Phase 1 approval does not bind P1-06E")
    if approval["epoch_number"] != finalization["gate_epoch"]:
        raise ExperimentReadinessError("Phase 1 approval epoch does not match finalization")
    if epoch["epoch_number"] != finalization["gate_epoch"]:
        raise ExperimentReadinessError("Phase 1 gate epoch number is inconsistent")
    if (
        receipt["gate_epoch_artifact"]["canonical_sha256"]
        != finalization["evidence_digests"]["gate_epoch_sha256"]
        or receipt["gate_epoch_artifact"]["canonical_sha256"]
        != approval["epoch_sha256"]
        or canonical_json_sha256(epoch)
        != receipt["gate_epoch_artifact"]["canonical_sha256"]
    ):
        raise ExperimentReadinessError("Phase 1 gate epoch digest is inconsistent")
    if epoch["validated_code_sha"] != finalization["validated_code_sha"]:
        raise ExperimentReadinessError("Phase 1 gate epoch validated code is inconsistent")
    if (
        approval["final_report_sha"] != finalization["final_report_sha"]
        or receipt["source_integration_commit"] != finalization["final_report_sha"]
    ):
        raise ExperimentReadinessError(
            "Phase 1 final report commit binding is inconsistent"
        )
    if approval["evidence_sha256"] != receipt["finalization_artifact"]["sha256"]:
        raise ExperimentReadinessError(
            "Phase 1 approval evidence digest does not bind finalization"
        )
    _verify_phase1_git_promotion(receipt)
    return {
        "promotion_status": receipt["promotion_status"],
        "source_integration_commit": receipt["source_integration_commit"],
        "gate_epoch": finalization["gate_epoch"],
        "finalization_sha256": receipt["finalization_artifact"]["sha256"],
        "approval_sha256": receipt["operator_approval"]["sha256"],
        "gate_epoch_sha256": receipt["gate_epoch_artifact"]["canonical_sha256"],
        "receipt_sha256": canonical_json_sha256(receipt),
    }


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
    invocation_capability = next(
        item
        for item in capabilities
        if item["capability_id"] == "invocation_level_real_usage"
    )
    phase1_completion = None
    if invocation_capability["status"] == "passed":
        phase1_completion = validate_phase1_completion_promotion()
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
        "phase1_completion": phase1_completion,
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


def _resolve_bound_artifact(repository_root, binding, label):
    relative_path = Path(binding["path"])
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ExperimentReadinessError(f"{label} path is unsafe")
    path = (Path(repository_root) / relative_path).resolve()
    try:
        path.relative_to(Path(repository_root).resolve())
    except ValueError as exc:
        raise ExperimentReadinessError(f"{label} path escapes repository root") from exc
    if path.is_symlink() or not path.is_file():
        raise ExperimentReadinessError(f"{label} is missing or unsafe: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != binding["sha256"]:
        raise ExperimentReadinessError(f"{label} digest does not match promotion")
    return path


def _verify_phase1_git_promotion(receipt):
    checkout = _phase1_source_checkout()
    object_format = _git_text(checkout, "rev-parse", "--show-object-format")
    if object_format != receipt["git_object_format"]:
        raise ExperimentReadinessError("Phase 1 promotion Git object format mismatch")
    commit = receipt["source_integration_commit"]
    _git_text(checkout, "cat-file", "-e", f"{commit}^{{commit}}")
    tree = _git_text(checkout, "rev-parse", f"{commit}^{{tree}}")
    if tree != receipt["source_tree"]:
        raise ExperimentReadinessError("Phase 1 promotion source tree mismatch")
    remote_head = receipt["remote_default_head"]
    _git_text(checkout, "cat-file", "-e", f"{remote_head}^{{commit}}")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "merge-base",
            "--is-ancestor",
            commit,
            remote_head,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ExperimentReadinessError(
            "Phase 1 final report commit is not reachable from the recorded "
            "remote default head"
        )


def _phase1_source_checkout():
    release_or_checkout_root = Path(__file__).resolve().parents[4]
    candidates = [release_or_checkout_root]
    manifest_path = release_or_checkout_root / "manifest.json"
    if manifest_path.is_file():
        manifest = _read_json_object(manifest_path, "runtime release manifest")
        source_root = manifest.get("source_root")
        if isinstance(source_root, str) and source_root:
            candidates.append(Path(source_root).expanduser().resolve())
    for candidate in candidates:
        completed = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return Path(completed.stdout.strip()).resolve()
    raise ExperimentReadinessError(
        "Phase 1 completion promotion requires its bound Git source checkout"
    )


def _git_text(checkout, *arguments):
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ExperimentReadinessError(
            completed.stderr.strip() or "Phase 1 Git promotion check failed"
        )
    return completed.stdout.strip()

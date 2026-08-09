"""Provider-free Phase 3 benchmark readiness receipts."""

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import (
    EXPERIMENT_MODES,
    canonical_json_sha256,
    publish_immutable_json,
)


class Phase3ReadinessError(RuntimeError):
    pass


_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "phase3_readiness_receipt.schema.json"
)
_EXPECTED_RUNTIME_ROOTS = {
    mode: f"modes/{mode}" for mode in EXPERIMENT_MODES
}


def build_phase3_readiness_receipt(
    *,
    fixture,
    bindings,
    mode_reconciliation,
    isolation_reconciliation,
    usage_reconciliation,
    verification_results,
    status="passed",
    decision_id="DEC-P3-readiness-execution",
):
    """Build and validate one canonically bound readiness receipt."""
    receipt = {
        "schema_version": "phase3_readiness_receipt.v1",
        "status": status,
        "decision_id": decision_id,
        "fixture": copy.deepcopy(fixture),
        "bindings": copy.deepcopy(bindings),
        "mode_reconciliation": copy.deepcopy(mode_reconciliation),
        "isolation_reconciliation": copy.deepcopy(
            isolation_reconciliation
        ),
        "usage_reconciliation": copy.deepcopy(usage_reconciliation),
        "verification": {
            "results": copy.deepcopy(verification_results),
            "verification_sha256": canonical_json_sha256(
                verification_results
            ),
        },
    }
    receipt["receipt_sha256"] = phase3_readiness_receipt_sha256(receipt)
    return validate_phase3_readiness_receipt(receipt)


def validate_phase3_readiness_receipt(receipt, *, schema_path=None):
    """Validate schema and cross-field relations not expressible in JSON Schema."""
    if not isinstance(receipt, dict):
        raise Phase3ReadinessError(
            "Phase 3 readiness receipt must be an object"
        )
    schema = _read_schema(schema_path or _SCHEMA_PATH)
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(receipt),
        key=lambda item: list(item.path),
    )
    if errors:
        first = errors[0]
        location = ".".join(
            str(part) for part in first.absolute_path
        ) or "<root>"
        raise Phase3ReadinessError(
            "Phase 3 readiness receipt schema error at "
            f"{location}: {first.message}"
        )
    if receipt["receipt_sha256"] != phase3_readiness_receipt_sha256(
        receipt
    ):
        raise Phase3ReadinessError(
            "receipt_sha256 does not bind canonical receipt content"
        )
    verification = receipt["verification"]
    if verification["verification_sha256"] != canonical_json_sha256(
        verification["results"]
    ):
        raise Phase3ReadinessError(
            "verification_sha256 does not bind verification results"
        )
    actual_roots = {
        mode: receipt["mode_reconciliation"][mode]["runtime_root"]
        for mode in EXPERIMENT_MODES
    }
    if actual_roots != _EXPECTED_RUNTIME_ROOTS:
        raise Phase3ReadinessError(
            "mode runtime roots do not match their namespaces"
        )
    visible_inputs = {
        receipt["mode_reconciliation"][mode]["visible_input_sha256"]
        for mode in EXPERIMENT_MODES
    }
    budgets = {
        receipt["mode_reconciliation"][mode]["budget_sha256"]
        for mode in EXPERIMENT_MODES
    }
    if len(visible_inputs) != 1 or len(budgets) != 1:
        raise Phase3ReadinessError(
            "mode input and budget bindings are not equal"
        )
    return copy.deepcopy(receipt)


def publish_phase3_readiness_receipt(path, receipt):
    """Validate and immutably publish a readiness receipt."""
    validated = validate_phase3_readiness_receipt(receipt)
    return publish_immutable_json(
        path,
        validated,
        label="Phase 3 readiness receipt",
    )


def phase3_readiness_receipt_sha256(receipt):
    """Return the canonical digest bound by ``receipt_sha256``."""
    content = copy.deepcopy(receipt)
    content.pop("receipt_sha256", None)
    return canonical_json_sha256(content)


def _read_schema(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Phase3ReadinessError(
            "Phase 3 readiness receipt schema is unavailable"
        )
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Phase3ReadinessError(
            "Phase 3 readiness receipt schema is invalid"
        ) from exc
    Draft202012Validator.check_schema(schema)
    return schema

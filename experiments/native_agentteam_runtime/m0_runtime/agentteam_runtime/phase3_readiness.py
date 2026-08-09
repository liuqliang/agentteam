"""Provider-free Phase 3 benchmark readiness receipts."""

import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .benchmark_adapter import (
    BenchmarkAdapterError,
    build_benchmark_instance_selection,
    validate_benchmark_instance_selection,
)
from .benchmark_preregistration import (
    BenchmarkPreregistrationError,
    authorize_mode_runtime_path,
    build_benchmark_preregistration,
    validate_benchmark_preregistration,
)
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


def produce_phase3_readiness_fixture(
    evidence_run,
    *,
    repository_binding,
    decision_contract_sha256,
    inherited_decision_id,
    research_authority_sha256,
    verification_evidence,
):
    """Exercise the provider-free readiness path and retain its receipt."""
    evidence_run = Path(evidence_run).resolve()
    fixture_root = evidence_run / "acceptance" / "phase3-readiness-fixture"
    metadata = _fixture_metadata()
    selection = _fixture_selection(metadata)
    replayed_selection = _fixture_selection(metadata)
    if selection != replayed_selection:
        raise Phase3ReadinessError(
            "Phase 3 fixture selection replay is not deterministic"
        )
    validate_benchmark_instance_selection(selection, metadata=metadata)
    changed_selection = copy.deepcopy(selection)
    changed_selection["ordered_instance_ids"].reverse()
    try:
        validate_benchmark_instance_selection(changed_selection)
    except BenchmarkAdapterError:
        pass
    else:
        raise Phase3ReadinessError(
            "Phase 3 fixture accepted a changed selection order"
        )

    preregistration = _fixture_preregistration(
        selection,
        repository_binding=repository_binding,
        research_authority_sha256=research_authority_sha256,
    )
    validate_benchmark_preregistration(preregistration)
    changed_preregistration = copy.deepcopy(preregistration)
    changed_preregistration["authorization"]["budgets"][
        "agentteam_full"
    ]["max_total_tokens"] += 1
    try:
        validate_benchmark_preregistration(changed_preregistration)
    except BenchmarkPreregistrationError:
        pass
    else:
        raise Phase3ReadinessError(
            "Phase 3 fixture accepted unequal mode budgets"
        )

    authorization = preregistration["authorization"]
    visible_digest = canonical_json_sha256(
        authorization["equal_input_bindings"]["shared_visible_inputs"]
    )
    runtime_root = fixture_root / "runtime"
    mode_bindings = {}
    sibling_denials = 0
    for index, mode in enumerate(EXPERIMENT_MODES):
        own_path = runtime_root / "modes" / mode / "binding.json"
        binding = {
            "visible_input_sha256": visible_digest,
            "budget_sha256": canonical_json_sha256(
                authorization["budgets"][mode]
            ),
        }
        publish_immutable_json(
            own_path,
            binding,
            label=f"Phase 3 {mode} fixture binding",
        )
        authorize_mode_runtime_path(
            preregistration,
            runtime_root=runtime_root,
            mode=mode,
            candidate_path=own_path,
        )
        sibling = EXPERIMENT_MODES[
            (index + 1) % len(EXPERIMENT_MODES)
        ]
        try:
            authorize_mode_runtime_path(
                preregistration,
                runtime_root=runtime_root,
                mode=mode,
                candidate_path=(
                    runtime_root
                    / "modes"
                    / sibling
                    / "binding.json"
                ),
            )
        except BenchmarkPreregistrationError:
            sibling_denials += 1
        else:
            raise Phase3ReadinessError(
                "Phase 3 fixture allowed cross-mode artifact access"
            )
        mode_bindings[mode] = {
            **binding,
            "runtime_root": authorization["isolation"][
                "mode_runtime_roots"
            ][mode],
        }

    provider_artifacts = [
        path.relative_to(fixture_root).as_posix()
        for path in fixture_root.rglob("*")
        if path.is_file()
        and (
            path.name in {"started.json", "usage.json", "terminal.json"}
            or "model-invocation" in path.name
            or "provider-session" in path.name
        )
    ]
    if provider_artifacts:
        raise Phase3ReadinessError(
            "Phase 3 fixture unexpectedly created provider artifacts"
        )
    results = {
        "selection_replay": _fixture_check(
            selection["selection_sha256"]
        ),
        "selection_mutation_detection": _fixture_check(
            {"rejected": "changed_ordered_instance_ids"}
        ),
        "preregistration_replay": _fixture_check(
            preregistration["authorization_sha256"]
        ),
        "preregistration_mutation_detection": _fixture_check(
            {"rejected": "unequal_agentteam_full_budget"}
        ),
        "equal_input_binding": _fixture_check(
            {
                mode: mode_bindings[mode]["visible_input_sha256"]
                for mode in EXPERIMENT_MODES
            }
        ),
        "equal_budget_binding": _fixture_check(
            {
                mode: mode_bindings[mode]["budget_sha256"]
                for mode in EXPERIMENT_MODES
            }
        ),
        "mode_isolation": _fixture_check(
            {
                "runtime_roots": mode_bindings,
                "sibling_denials": sibling_denials,
            }
        ),
        "decision_binding_replay": _fixture_check(
            {
                "decision_contract_sha256": decision_contract_sha256,
                "inherited_decision_id": inherited_decision_id,
            }
        ),
        "provider_absence": _fixture_check(
            {
                "invocation_artifacts": provider_artifacts,
                "provider_calls": 0,
                "verification": verification_evidence,
            }
        ),
    }
    receipt = build_phase3_readiness_receipt(
        fixture={
            "kind": "local_fixture",
            "benchmark": "swe_evo",
            "metadata_revision": metadata["metadata_revision"],
            "metadata_sha256": canonical_json_sha256(metadata),
        },
        bindings={
            "selection_sha256": selection["selection_sha256"],
            "ordered_instance_ids": selection["ordered_instance_ids"],
            "preregistration_authorization_sha256": preregistration[
                "authorization_sha256"
            ],
            "decision_contract_sha256": decision_contract_sha256,
            "inherited_decision_id": inherited_decision_id,
            "decision_replay_status": "idempotent",
        },
        mode_reconciliation=mode_bindings,
        isolation_reconciliation={
            "gold_visibility": "evaluator_only",
            "cross_mode_artifact_access": "denied",
            "independent_runtime_roots": True,
            "sibling_access_denials": sibling_denials,
        },
        usage_reconciliation={
            "live_provider_calls": 0,
            "scored_mode_executions": 0,
            "invocations_created": 0,
            "provider_status": "not_invoked",
            "terminal_usage_records": 0,
            "terminal_usage_status": "not_applicable",
            "token_totals": None,
        },
        verification_results=results,
    )
    publication = publish_phase3_readiness_receipt(
        evidence_run / "acceptance" / "phase3-readiness.json",
        receipt,
    )
    return {**publication, "receipt": receipt}


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


def _fixture_metadata():
    return {
        "schema_version": "swe_evo_metadata.v1",
        "benchmark": "swe_evo",
        "metadata_revision": "phase3-readiness-fixture-r1",
        "instances": [
            _fixture_instance("fixture-low-1", "low"),
            _fixture_instance("fixture-low-2", "low"),
            _fixture_instance("fixture-high-1", "high"),
            _fixture_instance("fixture-high-2", "high"),
        ],
    }


def _fixture_instance(instance_id, stratum):
    return {
        "instance_id": instance_id,
        "repository": "fixture/alpha",
        "complexity_stratum": stratum,
        "language": "python",
        "tags": ["pilot"],
    }


def _fixture_selection(metadata):
    return build_benchmark_instance_selection(
        metadata,
        metadata_revision="phase3-readiness-fixture-r1",
        filters={
            "repositories": ["fixture/alpha"],
            "languages": ["python"],
            "required_tags": ["pilot"],
            "excluded_instance_ids": [],
        },
        seed=20260809,
        stratum_quotas={"low": 1, "high": 1},
    )


def _fixture_preregistration(
    selection,
    *,
    repository_binding,
    research_authority_sha256,
):
    visible_inputs = {
        "repository": dict(repository_binding),
        "runtime": {
            "agentteam_release_commit": repository_binding["commit"],
            "codex_cli_version": "not-invoked",
            "environment_version": "phase3-readiness-fixture-v1",
        },
        "task": {
            "goal": "Verify frozen benchmark authority without execution.",
            "constraints": [
                "Use local fixtures only.",
                "Do not read evaluator gold.",
                "Do not invoke a model provider.",
            ],
            "non_goals": ["Produce a benchmark score."],
            "acceptance_commands": [
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests."
                    "test_phase3_benchmark_readiness",
                ]
            ],
        },
        "model": {
            "model": "gpt-5.6-sol",
            "reasoning_profile": "high",
            "service_configuration_sha256": canonical_json_sha256(
                {"provider_status": "not_invoked"}
            ),
        },
        "execution": {
            "tools_sha256": _fixture_digest("tools"),
            "network_policy": "disabled",
            "sandbox_policy_sha256": _fixture_digest("sandbox"),
            "permission_policy_sha256": _fixture_digest("permission"),
            "host_class": "local-fixture",
            "cpu_limit": 1,
            "memory_limit_bytes": 1024 * 1024 * 1024,
            "external_services_sha256": _fixture_digest(
                "external-services"
            ),
            "benchmark_visible_tests_sha256": _fixture_digest(
                "visible-tests"
            ),
            "termination_policy_sha256": _fixture_digest("termination"),
            "shared_dependency_cache_sha256": _fixture_digest(
                "dependency-cache"
            ),
        },
    }
    budget = {
        "max_total_tokens": 100000,
        "max_wall_time_seconds": 3600,
        "max_operator_interactions": 2,
        "allowed_operator_input_types": [
            "expected_operator_action",
            "decision_escalation",
        ],
    }
    return build_benchmark_preregistration(
        research_authority_sha256=research_authority_sha256,
        selection_sha256=selection["selection_sha256"],
        ordered_instance_ids=selection["ordered_instance_ids"],
        shared_visible_inputs=visible_inputs,
        shared_budget=budget,
        direct_taskpack_sha256_by_instance={
            instance_id: canonical_json_sha256(
                {
                    "kind": "frozen_direct_taskpack",
                    "instance_id": instance_id,
                }
            )
            for instance_id in selection["ordered_instance_ids"]
        },
        non_inferiority_margin=0.05,
        max_token_cost_ratio=1.25,
        max_wall_time_cost_ratio=1.20,
        preselected_secondary_benefit_metric=(
            "corrective_operator_interventions"
        ),
        mode_order=[
            ["single_codex", "agentteam_direct", "agentteam_full"],
            ["agentteam_direct", "agentteam_full", "single_codex"],
            ["agentteam_full", "single_codex", "agentteam_direct"],
        ],
    )


def _fixture_check(evidence):
    return {
        "status": "passed",
        "evidence_sha256": canonical_json_sha256(evidence),
    }


def _fixture_digest(label):
    return hashlib.sha256(label.encode("ascii")).hexdigest()

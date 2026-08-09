"""Immutable Phase 3 benchmark preregistration authority.

The preregistration is deliberately prepared before provider admission.  It
binds the selected instances and every comparison input which could otherwise
be changed after an outcome became visible.  Publication is create-if-absent:
one authority root can contain exactly one canonical preregistration.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import (
    EXPERIMENT_MODES,
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    publish_immutable_json,
    schema_path,
)


PREREGISTRATION_SCHEMA_VERSION = "benchmark_preregistration.v1"
PREREGISTRATION_DECISION_ID = "DEC-P3-preregistration-execution"
RESEARCH_AUTHORITY_PATH = (
    "experiments/native_agentteam_runtime/research/"
    "agentteam_research_positioning.md"
)
BENCHMARK_NAME = "SWE-EVO"
PREREGISTRATION_FILENAME = "benchmark-preregistration.v1.json"

PRIMARY_METRIC = "final_mechanical_acceptance"
SECONDARY_METRICS = (
    "benchmark_native_partial_score",
    "swe_style_partial_score",
    "verified_milestones_completed",
    "accepted_attempts",
    "rejected_attempts",
    "regressions",
    "expected_operator_actions",
    "corrective_operator_interventions",
    "decision_escalations",
    "wall_time_seconds",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "uncached_work_tokens",
    "artifact_bytes",
    "repeated_reading_cost",
    "retry_cost",
    "recovery_cost",
)
CONTINUATION_BENEFIT_METRICS = {
    "corrective_operator_interventions",
    "regressions",
    "recovery_cost",
}
COMMON_FORBIDDEN_INPUTS = (
    "gold_patch",
    "evaluator_gold",
    "prior_outcomes",
    "sibling_mode_artifacts",
    "sibling_mode_taskpack",
    "sibling_mode_transcript",
    "sibling_mode_report",
    "sibling_mode_patch",
    "sibling_mode_cache_artifact",
)
SINGLE_CODEX_FORBIDDEN_INPUTS = COMMON_FORBIDDEN_INPUTS + (
    "agentteam_taskpack",
    "repo_map_handoff",
    "planner_output",
)


class BenchmarkPreregistrationError(ExperimentContractError):
    """Raised when Phase 3 preregistration authority is incomplete or changed."""


def build_benchmark_preregistration(
    *,
    research_authority_sha256,
    selection_sha256,
    ordered_instance_ids,
    shared_visible_inputs,
    shared_budget,
    direct_taskpack_sha256_by_instance,
    non_inferiority_margin,
    max_token_cost_ratio,
    max_wall_time_cost_ratio,
    preselected_secondary_benefit_metric,
    mode_order,
):
    """Build and validate the complete preregistration envelope.

    The caller supplies the values which calibration is expected to freeze.
    Research-authority constants (metrics, scoring weights, repetition
    triggers, coverage threshold, and isolation policy) are not caller
    configurable.
    """

    modes = list(EXPERIMENT_MODES)
    visible_inputs = _json_snapshot(
        shared_visible_inputs, "shared_visible_inputs"
    )
    budget = _json_snapshot(shared_budget, "shared_budget")
    direct_taskpacks = _json_snapshot(
        direct_taskpack_sha256_by_instance,
        "direct_taskpack_sha256_by_instance",
    )
    visible_digest = canonical_json_sha256(visible_inputs)
    authorization = {
        "decision_id": PREREGISTRATION_DECISION_ID,
        "research_authority": {
            "path": RESEARCH_AUTHORITY_PATH,
            "sha256": research_authority_sha256,
        },
        "benchmark": BENCHMARK_NAME,
        "selection": {
            "selection_sha256": selection_sha256,
            "ordered_instance_ids": list(ordered_instance_ids),
        },
        "modes": modes,
        "equal_input_bindings": {
            "shared_visible_inputs": visible_inputs,
            "per_mode_visible_input_sha256": {
                mode: visible_digest for mode in modes
            },
        },
        "budgets": {mode: copy.deepcopy(budget) for mode in modes},
        "mode_controls": {
            "single_codex": {
                "agentteam_artifact_visibility": "forbidden",
                "counted_stages": ["model_execution"],
            },
            "agentteam_direct": {
                "taskpack_sha256_by_instance": direct_taskpacks,
                "taskpack_frozen_before_gold": True,
                "reuse_same_taskpack_across_repetitions": True,
                "live_authoring": False,
                "counted_stages": [
                    "routing",
                    "worker",
                    "verification",
                    "integration",
                    "report",
                ],
            },
            "agentteam_full": {
                "live_authoring": True,
                "counted_stages": [
                    "authoring",
                    "planning",
                    "repo_mapping",
                    "worker",
                    "review",
                    "follow_up",
                    "verification",
                    "integration",
                    "report",
                ],
            },
        },
        "metrics": {
            "primary": PRIMARY_METRIC,
            "secondary": list(SECONDARY_METRICS),
            "partial_score": {
                "benchmark_native": True,
                "swe_style": {
                    "f2p_weight_percent": 70,
                    "p2p_weight_percent": 20,
                    "patch_application_scope_basic_verification_weight_percent": 10,
                },
            },
            "final_score_authority": "benchmark_evaluator_only",
            "reasoning_token_policy": (
                "report_only_when_provider_declares_non_overlapping"
            ),
            "uncached_work_formula": "max(input-cached_input,0)+output",
        },
        "thresholds": {
            "provider_usage_coverage_percent": 100,
            "third_repetition_variance_percent": 30,
            "non_inferiority_margin": non_inferiority_margin,
            "acceptable_cost": {
                "max_token_ratio": max_token_cost_ratio,
                "max_wall_time_ratio": max_wall_time_cost_ratio,
            },
            "continuation": {
                "primary_improvement_or_noninferiority_with_benefit": True,
                "preselected_secondary_benefit_metric": (
                    preselected_secondary_benefit_metric
                ),
            },
        },
        "repetition_policy": {
            "initial_repetitions": 2,
            "max_repetitions": 3,
            "run_third_on_outcome_disagreement": True,
            "run_third_on_token_or_wall_variance_percent": 30,
            "retain_all_terminal_outcomes": True,
            "paired_per_task_reporting": True,
            "mode_order": [list(order) for order in mode_order],
        },
        "isolation": {
            "gold_visibility": "evaluator_only",
            "fresh_model_session_per_run": True,
            "isolated_work_root_per_run": True,
            "cross_mode_artifact_access": "forbidden",
            "shared_cache_policy": "declared_and_equal_only",
            "mode_runtime_roots": {
                mode: f"modes/{mode}" for mode in modes
            },
            "forbidden_visible_inputs": {
                "single_codex": list(SINGLE_CODEX_FORBIDDEN_INPUTS),
                "agentteam_direct": list(COMMON_FORBIDDEN_INPUTS),
                "agentteam_full": list(COMMON_FORBIDDEN_INPUTS),
            },
        },
    }
    preregistration = {
        "schema_version": PREREGISTRATION_SCHEMA_VERSION,
        "authorization": authorization,
        "authorization_sha256": canonical_json_sha256(authorization),
    }
    validate_benchmark_preregistration(preregistration)
    return preregistration


def validate_benchmark_preregistration(preregistration):
    """Validate schema, research invariants, and the authorization digest."""

    if not isinstance(preregistration, dict):
        raise BenchmarkPreregistrationError(
            "benchmark preregistration must be a JSON object"
        )
    schema = _read_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(preregistration), key=lambda item: list(item.path)
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise BenchmarkPreregistrationError(
            "benchmark preregistration schema validation failed at "
            f"{location}: {first.message}"
        )

    authorization = preregistration["authorization"]
    expected_digest = canonical_json_sha256(authorization)
    if preregistration["authorization_sha256"] != expected_digest:
        raise BenchmarkPreregistrationError(
            "authorization_sha256 does not bind the canonical authorization"
        )
    if tuple(authorization["modes"]) != EXPERIMENT_MODES:
        raise BenchmarkPreregistrationError(
            "modes must be the complete ordered Phase 2 mode set"
        )
    instance_ids = authorization["selection"]["ordered_instance_ids"]
    if len(instance_ids) != len(set(instance_ids)):
        raise BenchmarkPreregistrationError(
            "ordered selected instance ids must be unique"
        )
    direct_taskpacks = authorization["mode_controls"]["agentteam_direct"][
        "taskpack_sha256_by_instance"
    ]
    if set(direct_taskpacks) != set(instance_ids):
        raise BenchmarkPreregistrationError(
            "direct taskpack bindings must cover every selected instance exactly"
        )

    equal_inputs = authorization["equal_input_bindings"]
    expected_visible_digest = canonical_json_sha256(
        equal_inputs["shared_visible_inputs"]
    )
    expected_visible_bindings = {
        mode: expected_visible_digest for mode in EXPERIMENT_MODES
    }
    if equal_inputs["per_mode_visible_input_sha256"] != expected_visible_bindings:
        raise BenchmarkPreregistrationError(
            "every mode must bind the same canonical visible inputs"
        )

    budgets = authorization["budgets"]
    baseline_budget = canonical_json_bytes(budgets[EXPERIMENT_MODES[0]])
    if any(
        canonical_json_bytes(budgets[mode]) != baseline_budget
        for mode in EXPERIMENT_MODES[1:]
    ):
        raise BenchmarkPreregistrationError(
            "every mode must have an identical total budget"
        )

    metrics = authorization["metrics"]
    if metrics["primary"] != PRIMARY_METRIC or tuple(
        metrics["secondary"]
    ) != SECONDARY_METRICS:
        raise BenchmarkPreregistrationError(
            "metrics differ from the approved research authority"
        )
    benefit_metric = authorization["thresholds"]["continuation"][
        "preselected_secondary_benefit_metric"
    ]
    if benefit_metric not in CONTINUATION_BENEFIT_METRICS:
        raise BenchmarkPreregistrationError(
            "continuation benefit metric was not preselected from the authority"
        )

    repetition = authorization["repetition_policy"]
    if (
        repetition["run_third_on_token_or_wall_variance_percent"]
        != authorization["thresholds"]["third_repetition_variance_percent"]
    ):
        raise BenchmarkPreregistrationError(
            "third repetition threshold differs from the frozen threshold"
        )
    _validate_counterbalanced_mode_order(repetition["mode_order"])
    _validate_isolation(authorization["isolation"])
    return preregistration


def preregistration_authorization_sha256(preregistration):
    """Return the verified digest which authorizes later benchmark execution."""

    validate_benchmark_preregistration(preregistration)
    return preregistration["authorization_sha256"]


def publish_benchmark_preregistration(authority_root, preregistration):
    """Publish one immutable canonical preregistration under ``authority_root``."""

    preregistration = _json_snapshot(preregistration, "preregistration")
    validate_benchmark_preregistration(preregistration)
    publication = publish_immutable_json(
        Path(authority_root) / PREREGISTRATION_FILENAME,
        preregistration,
        label="benchmark preregistration",
    )
    return {
        **publication,
        "authorization_sha256": preregistration["authorization_sha256"],
    }


def authorize_mode_runtime_path(
    preregistration,
    *,
    runtime_root,
    mode,
    candidate_path,
):
    """Resolve a path only when it remains inside the requesting mode's root.

    Launchers should call this at their artifact boundary.  ``Path.resolve``
    follows existing symlinks, so a symlink from one mode root into a sibling
    root is denied as cross-mode access too.
    """

    validate_benchmark_preregistration(preregistration)
    if mode not in EXPERIMENT_MODES:
        raise BenchmarkPreregistrationError(f"unsupported experiment mode: {mode!r}")
    root = Path(runtime_root).resolve()
    candidate = Path(candidate_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved_candidate = candidate.resolve()
    relative_root = preregistration["authorization"]["isolation"][
        "mode_runtime_roots"
    ][mode]
    own_root = (root / relative_root).resolve()
    try:
        resolved_candidate.relative_to(own_root)
    except ValueError as exc:
        raise BenchmarkPreregistrationError(
            f"mode {mode} cannot access artifacts outside {relative_root}"
        ) from exc
    return resolved_candidate


def _validate_counterbalanced_mode_order(mode_order):
    if len(mode_order) != len(EXPERIMENT_MODES):
        raise BenchmarkPreregistrationError(
            "mode_order must preregister all three possible repetition orders"
        )
    required = set(EXPERIMENT_MODES)
    for order in mode_order:
        if len(order) != len(EXPERIMENT_MODES) or set(order) != required:
            raise BenchmarkPreregistrationError(
                "each repetition mode order must be a complete permutation"
            )
    for position in range(len(EXPERIMENT_MODES)):
        if {order[position] for order in mode_order} != required:
            raise BenchmarkPreregistrationError(
                "mode_order must counterbalance every mode across positions"
            )


def _validate_isolation(isolation):
    expected_roots = {mode: f"modes/{mode}" for mode in EXPERIMENT_MODES}
    if isolation["mode_runtime_roots"] != expected_roots:
        raise BenchmarkPreregistrationError(
            "mode runtime roots must be distinct preregistered mode namespaces"
        )
    for relative in isolation["mode_runtime_roots"].values():
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise BenchmarkPreregistrationError("mode runtime root is unsafe")
    expected_forbidden = {
        "single_codex": list(SINGLE_CODEX_FORBIDDEN_INPUTS),
        "agentteam_direct": list(COMMON_FORBIDDEN_INPUTS),
        "agentteam_full": list(COMMON_FORBIDDEN_INPUTS),
    }
    if isolation["forbidden_visible_inputs"] != expected_forbidden:
        raise BenchmarkPreregistrationError(
            "gold, prior outcome, or cross-mode visibility policy changed"
        )


def _read_schema():
    path = schema_path("benchmark_preregistration.schema.json")
    if path.is_symlink() or not path.is_file():
        raise BenchmarkPreregistrationError(
            f"benchmark preregistration schema is missing or unsafe: {path}"
        )
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkPreregistrationError(
            "benchmark preregistration schema is unreadable"
        ) from exc
    if not isinstance(schema, dict):
        raise BenchmarkPreregistrationError(
            "benchmark preregistration schema must be an object"
        )
    return schema


def _json_snapshot(value, label):
    if not isinstance(value, dict):
        raise BenchmarkPreregistrationError(f"{label} must be a JSON object")
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise BenchmarkPreregistrationError(f"{label} cannot be snapshotted") from exc

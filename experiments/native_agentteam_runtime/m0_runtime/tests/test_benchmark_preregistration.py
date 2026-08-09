from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.benchmark_preregistration import (
    BenchmarkPreregistrationError,
    SECONDARY_METRICS,
    authorize_mode_runtime_path,
    build_benchmark_preregistration,
    preregistration_authorization_sha256,
    publish_benchmark_preregistration,
    validate_benchmark_preregistration,
)
from agentteam_runtime.experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    canonical_json_sha256,
)


MODES = ("single_codex", "agentteam_direct", "agentteam_full")


def _shared_visible_inputs():
    return {
        "repository": {
            "source": "local:swe-evo-fixture",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "git_object_format": "sha1",
        },
        "runtime": {
            "agentteam_release_commit": "3" * 40,
            "codex_cli_version": "codex-cli-test-v1",
            "environment_version": "fixture-env-v1",
        },
        "task": {
            "goal": "Implement the selected repository task.",
            "constraints": ["Do not read evaluator gold."],
            "non_goals": ["Do not change benchmark scope."],
            "acceptance_commands": [["python3", "-m", "unittest"]],
        },
        "model": {
            "model": "gpt-test",
            "reasoning_profile": "high",
            "service_configuration_sha256": "4" * 64,
        },
        "execution": {
            "tools_sha256": "5" * 64,
            "network_policy": "disabled",
            "sandbox_policy_sha256": "6" * 64,
            "permission_policy_sha256": "7" * 64,
            "host_class": "fixture-host",
            "cpu_limit": 4,
            "memory_limit_bytes": 8 * 1024 * 1024 * 1024,
            "external_services_sha256": "8" * 64,
            "benchmark_visible_tests_sha256": "9" * 64,
            "termination_policy_sha256": "a" * 64,
            "shared_dependency_cache_sha256": "b" * 64,
        },
    }


def _shared_budget():
    return {
        "max_total_tokens": 100000,
        "max_wall_time_seconds": 3600,
        "max_operator_interactions": 2,
        "allowed_operator_input_types": [
            "expected_operator_action",
            "decision_escalation",
        ],
    }


def _build(**overrides):
    arguments = {
        "research_authority_sha256": "c" * 64,
        "selection_sha256": "d" * 64,
        "ordered_instance_ids": ["swe-evo-001", "swe-evo-002"],
        "shared_visible_inputs": _shared_visible_inputs(),
        "shared_budget": _shared_budget(),
        "direct_taskpack_sha256_by_instance": {
            "swe-evo-001": "e" * 64,
            "swe-evo-002": "f" * 64,
        },
        "non_inferiority_margin": 0.05,
        "max_token_cost_ratio": 1.25,
        "max_wall_time_cost_ratio": 1.20,
        "preselected_secondary_benefit_metric": (
            "corrective_operator_interventions"
        ),
        "mode_order": [
            ["single_codex", "agentteam_direct", "agentteam_full"],
            ["agentteam_direct", "agentteam_full", "single_codex"],
            ["agentteam_full", "single_codex", "agentteam_direct"],
        ],
    }
    arguments.update(overrides)
    return build_benchmark_preregistration(**arguments)


def _refresh_digest(preregistration):
    preregistration["authorization_sha256"] = canonical_json_sha256(
        preregistration["authorization"]
    )


class BenchmarkPreregistrationTests(unittest.TestCase):
    def test_build_binds_complete_research_authority_and_is_stable(self):
        first = _build()
        second = _build()

        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertEqual(
            preregistration_authorization_sha256(first),
            canonical_json_sha256(first["authorization"]),
        )
        authorization = first["authorization"]
        self.assertEqual(authorization["modes"], list(MODES))
        self.assertEqual(
            authorization["metrics"]["secondary"], list(SECONDARY_METRICS)
        )
        self.assertEqual(
            authorization["thresholds"]["provider_usage_coverage_percent"], 100
        )
        self.assertEqual(
            authorization["repetition_policy"]["initial_repetitions"], 2
        )
        self.assertEqual(
            authorization["repetition_policy"]["max_repetitions"], 3
        )
        self.assertTrue(
            authorization["mode_controls"]["agentteam_direct"][
                "taskpack_frozen_before_gold"
            ]
        )
        self.assertTrue(
            authorization["mode_controls"]["agentteam_direct"][
                "reuse_same_taskpack_across_repetitions"
            ]
        )
        self.assertEqual(
            authorization["repetition_policy"][
                "run_third_on_token_or_wall_variance_percent"
            ],
            30,
        )

    def test_selection_order_and_digest_are_both_authorized(self):
        first = _build()
        reordered = _build(
            ordered_instance_ids=["swe-evo-002", "swe-evo-001"]
        )
        changed_selection = _build(selection_sha256="e" * 64)

        self.assertNotEqual(
            first["authorization_sha256"], reordered["authorization_sha256"]
        )
        self.assertNotEqual(
            first["authorization_sha256"],
            changed_selection["authorization_sha256"],
        )

    def test_any_post_build_mutation_invalidates_authorization_digest(self):
        mutations = [
            lambda value: value["authorization"]["selection"][
                "ordered_instance_ids"
            ].reverse(),
            lambda value: value["authorization"]["equal_input_bindings"][
                "shared_visible_inputs"
            ]["task"]["constraints"].append("A changed constraint."),
            lambda value: value["authorization"]["budgets"]["agentteam_full"].update(
                {"max_total_tokens": 100001}
            ),
            lambda value: value["authorization"]["thresholds"].update(
                {"non_inferiority_margin": 0.10}
            ),
            lambda value: value["authorization"]["repetition_policy"][
                "mode_order"
            ].reverse(),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(_build())
                mutate(changed)
                with self.assertRaisesRegex(
                    BenchmarkPreregistrationError, "authorization_sha256"
                ):
                    validate_benchmark_preregistration(changed)

    def test_recomputed_digest_cannot_hide_unequal_visible_input_binding(self):
        changed = copy.deepcopy(_build())
        changed["authorization"]["equal_input_bindings"][
            "per_mode_visible_input_sha256"
        ]["agentteam_full"] = "f" * 64
        _refresh_digest(changed)

        with self.assertRaisesRegex(
            BenchmarkPreregistrationError, "same canonical visible inputs"
        ):
            validate_benchmark_preregistration(changed)

    def test_recomputed_digest_cannot_hide_unequal_mode_budget(self):
        changed = copy.deepcopy(_build())
        changed["authorization"]["budgets"]["agentteam_full"][
            "max_total_tokens"
        ] += 1
        _refresh_digest(changed)

        with self.assertRaisesRegex(
            BenchmarkPreregistrationError, "identical total budget"
        ):
            validate_benchmark_preregistration(changed)

    def test_schema_rejects_unregistered_or_gold_visible_fields(self):
        changed = copy.deepcopy(_build())
        changed["authorization"]["selection"]["gold_patch"] = "secret"
        _refresh_digest(changed)

        with self.assertRaisesRegex(
            BenchmarkPreregistrationError, "schema validation failed"
        ):
            validate_benchmark_preregistration(changed)

        for mode in MODES:
            forbidden = changed["authorization"]["isolation"][
                "forbidden_visible_inputs"
            ][mode]
            self.assertIn("gold_patch", forbidden)
            self.assertIn("prior_outcomes", forbidden)
            self.assertIn("sibling_mode_artifacts", forbidden)

    def test_counterbalanced_repetition_order_is_required(self):
        with self.assertRaisesRegex(
            BenchmarkPreregistrationError, "counterbalance"
        ):
            _build(
                mode_order=[
                    ["single_codex", "agentteam_direct", "agentteam_full"],
                    ["single_codex", "agentteam_full", "agentteam_direct"],
                    ["agentteam_full", "agentteam_direct", "single_codex"],
                ]
            )

    def test_direct_taskpack_binding_covers_every_selected_instance(self):
        with self.assertRaisesRegex(
            BenchmarkPreregistrationError, "cover every selected instance"
        ):
            _build(
                direct_taskpack_sha256_by_instance={"swe-evo-001": "e" * 64}
            )

    def test_thresholds_must_be_frozen_before_build(self):
        for field, value in (
            ("non_inferiority_margin", None),
            ("max_token_cost_ratio", None),
            ("max_wall_time_cost_ratio", None),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    BenchmarkPreregistrationError, "schema validation failed"
                ):
                    _build(**{field: value})

    def test_publication_is_canonical_idempotent_and_conflict_safe(self):
        preregistration = _build()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = publish_benchmark_preregistration(root, preregistration)
            replay = publish_benchmark_preregistration(root, preregistration)

            self.assertTrue(first["created"])
            self.assertFalse(replay["created"])
            self.assertEqual(first["authorization_sha256"], replay["authorization_sha256"])
            published_path = Path(first["path"])
            self.assertEqual(
                published_path.read_bytes(), canonical_json_bytes(preregistration) + b"\n"
            )
            conflicting = _build(selection_sha256="e" * 64)
            with self.assertRaisesRegex(
                ExperimentContractError, "already exists with different"
            ):
                publish_benchmark_preregistration(root, conflicting)

    def test_mode_runtime_path_allows_only_requesting_mode_namespace(self):
        preregistration = _build()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            own = root / "modes" / "single_codex" / "result.json"
            sibling = root / "modes" / "agentteam_direct" / "result.json"
            own.parent.mkdir(parents=True)
            sibling.parent.mkdir(parents=True)
            own.write_text("{}", encoding="utf-8")
            sibling.write_text("{}", encoding="utf-8")

            self.assertEqual(
                authorize_mode_runtime_path(
                    preregistration,
                    runtime_root=root,
                    mode="single_codex",
                    candidate_path=own,
                ),
                own.resolve(),
            )
            with self.assertRaisesRegex(
                BenchmarkPreregistrationError, "cannot access artifacts"
            ):
                authorize_mode_runtime_path(
                    preregistration,
                    runtime_root=root,
                    mode="single_codex",
                    candidate_path=sibling,
                )

    def test_mode_runtime_path_rejects_symlink_into_sibling_mode(self):
        preregistration = _build()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            own_root = root / "modes" / "single_codex"
            sibling_root = root / "modes" / "agentteam_direct"
            own_root.mkdir(parents=True)
            sibling_root.mkdir(parents=True)
            secret = sibling_root / "prior-outcome.json"
            secret.write_text(json.dumps({"passed": True}), encoding="utf-8")
            link = own_root / "sibling"
            link.symlink_to(sibling_root, target_is_directory=True)

            with self.assertRaisesRegex(
                BenchmarkPreregistrationError, "cannot access artifacts"
            ):
                authorize_mode_runtime_path(
                    preregistration,
                    runtime_root=root,
                    mode="single_codex",
                    candidate_path=link / secret.name,
                )

    def test_builder_snapshots_caller_owned_inputs(self):
        visible = _shared_visible_inputs()
        budget = _shared_budget()
        preregistration = _build(
            shared_visible_inputs=visible,
            shared_budget=budget,
        )
        visible["task"]["goal"] = "mutated later"
        budget["max_total_tokens"] = 1

        validate_benchmark_preregistration(preregistration)
        self.assertNotEqual(
            preregistration["authorization"]["equal_input_bindings"][
                "shared_visible_inputs"
            ]["task"]["goal"],
            visible["task"]["goal"],
        )
        for mode in MODES:
            self.assertEqual(
                preregistration["authorization"]["budgets"][mode][
                    "max_total_tokens"
                ],
                100000,
            )


if __name__ == "__main__":
    unittest.main()

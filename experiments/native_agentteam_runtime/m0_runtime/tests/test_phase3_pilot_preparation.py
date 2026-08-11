import copy
import unittest

from agentteam_runtime.phase3_pilot_preparation import (
    FIXED_DATASET_ARTIFACT_SHA256,
    FIXED_DATASET_ROW_COUNT,
    FIXED_SOURCE_COMMIT,
    Phase3PreparationError,
    build_phase3_instance_direct_taskpack,
    build_phase3_instance_preregistration,
    build_phase3_instance_visible_input,
    convert_swe_evo_inventory,
    decision_input_report,
    fixed_dataset_binding,
    materialize_phase3_instance_authorities,
    prepare_phase3_pilot_selection,
    replay_phase3_pilot_selection,
    routing_manifest_bytes,
    selection_authority_bytes,
    validate_routing_manifest,
    validate_phase3_pilot_decisions,
    validate_phase3_instance_direct_taskpack,
    validate_phase3_selection_authority,
)
from agentteam_runtime.experiment_contract import canonical_json_sha256


def _inventory():
    return {
        **fixed_dataset_binding(),
        "instances": [
            {
                "instance_id": f"swe-evo-{index:03d}",
                "repository": "org/repo",
                "complexity_stratum": "lower",
                "language": "python",
                "tags": ["pilot"],
            }
            for index in range(FIXED_DATASET_ROW_COUNT)
        ],
    }


def _decisions():
    return {
        "schema_version": "phase3_pilot_decisions.v1",
        "authority": {
            "decision_id": "DEC-P3B-pilot-parameters",
            "revision": 1,
            "status": "approved",
            "decided_by": "operator-semantic-authority",
            "decided_at": "2026-08-11T00:00:00Z",
        },
        "complexity": {
            "proxy": {
                "name": "reviewed-visible-stratum",
                "source": "fixed routing manifest",
                "extraction_rule": "Use the reviewed complexity_stratum field.",
                "source_fields": ["complexity_stratum"],
            },
            "gold_blind": True,
            "stratum_boundaries": [
                {
                    "stratum": "lower",
                    "boundary_rule": "reviewed lower-complexity boundary",
                }
            ],
        },
        "selection": {
            "metadata_revision": "swe-evo-fixed-r1",
            "filters": {
                "repositories": [],
                "languages": ["python"],
                "required_tags": ["pilot"],
                "excluded_instance_ids": [],
            },
            "seed": 20260811,
            "stratum_quotas": {"lower": 3},
            "sample_size": 3,
        },
        "execution": {
            "model": "operator-selected-codex-model",
            "reasoning_profile": "high",
            "per_instance_budget": {
                "max_total_tokens": 1000,
                "max_wall_time_seconds": 120,
            },
        },
        "thresholds": {
            "non_inferiority_margin": 0.05,
            "max_token_cost_ratio": 1.25,
            "max_wall_time_cost_ratio": 1.25,
            "preselected_secondary_benefit_metric": (
                "corrective_operator_interventions"
            ),
        },
    }


def _execution_profile():
    return {
        "runtime": {
            "agentteam_release_commit": "1" * 40,
            "codex_cli_version": "codex-cli-phase3b-test",
            "environment_version": "phase3b-test-v1",
        },
        "model": {
            "model": "operator-selected-codex-model",
            "reasoning_profile": "high",
            "service_configuration_sha256": "2" * 64,
        },
        "execution": {
            "tools_sha256": "3" * 64,
            "network_policy": "disabled",
            "sandbox_policy_sha256": "4" * 64,
            "permission_policy_sha256": "5" * 64,
            "host_class": "phase3b-test-host",
            "cpu_limit": 4,
            "memory_limit_bytes": 8 * 1024 * 1024 * 1024,
            "external_services_sha256": "6" * 64,
            "benchmark_visible_tests_sha256": "7" * 64,
            "termination_policy_sha256": "8" * 64,
            "shared_dependency_cache_sha256": "9" * 64,
        },
    }


def _shared_budget():
    return {
        "max_total_tokens": 1000,
        "max_wall_time_seconds": 120,
        "max_operator_interactions": 1,
        "allowed_operator_input_types": ["decision_escalation"],
    }


def _mode_order():
    return [
        ["single_codex", "agentteam_direct", "agentteam_full"],
        ["agentteam_direct", "agentteam_full", "single_codex"],
        ["agentteam_full", "single_codex", "agentteam_direct"],
    ]


def _task_inputs(selection_authority):
    return {
        instance_id: {
            "repository": {
                "source": f"local:{instance_id}",
                "commit": f"{index + 1:x}" * 40,
                "tree": f"{index + 5:x}" * 40,
                "git_object_format": "sha1",
            },
            "task": {
                "goal": f"Implement selected task {instance_id}.",
                "constraints": ["Do not read evaluator gold."],
                "non_goals": ["Do not change benchmark scope."],
                "acceptance_commands": [
                    ["python3", "-m", "unittest", f"tests.{instance_id}"]
                ],
            },
        }
        for index, instance_id in enumerate(
            selection_authority["selection"]["ordered_instance_ids"]
        )
    }


def _selection_authority():
    return prepare_phase3_pilot_selection(
        convert_swe_evo_inventory(_inventory()),
        _decisions(),
    )


class Phase3PilotPreparationTests(unittest.TestCase):
    def test_fixed_binding_and_byte_stable_manifest(self):
        first = convert_swe_evo_inventory(_inventory())
        second = convert_swe_evo_inventory(copy.deepcopy(_inventory()))
        self.assertEqual(first, second)
        self.assertEqual(routing_manifest_bytes(first), routing_manifest_bytes(second))
        self.assertEqual(first["manifest"]["dataset"]["source_commit"], FIXED_SOURCE_COMMIT)
        self.assertEqual(first["manifest"]["dataset"]["artifact_sha256"], FIXED_DATASET_ARTIFACT_SHA256)

    def test_evaluator_fields_rejected(self):
        for field in ("patch", "test_patch", "score", "prior_result"):
            inventory = _inventory()
            inventory["instances"][0][field] = "restricted"
            with self.subTest(field=field), self.assertRaisesRegex(
                Phase3PreparationError,
                "non-routing",
            ):
                convert_swe_evo_inventory(inventory)

    def test_fixed_binding_mismatch_rejected(self):
        inventory = _inventory()
        inventory["source_commit"] = "0" * 40
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "fixed inventory binding mismatch",
        ):
            convert_swe_evo_inventory(inventory)

    def test_fixed_row_count_mismatch_rejected(self):
        inventory = _inventory()
        inventory["instances"].pop()
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "fixed inventory row count mismatch",
        ):
            convert_swe_evo_inventory(inventory)

    def test_unknown_top_level_fields_rejected(self):
        inventory = _inventory()
        inventory["score"] = 0
        with self.assertRaisesRegex(Phase3PreparationError, "non-routing"):
            convert_swe_evo_inventory(inventory)

    def test_manifest_mutation_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        manifest["manifest"]["gold_visibility"] = "runtime_visible"
        with self.assertRaises(Phase3PreparationError):
            validate_routing_manifest(manifest)

    def test_resigned_dataset_binding_mutation_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        manifest["manifest"]["dataset"]["source_repository"] = "https://example.invalid/repo.git"
        manifest["manifest_sha256"] = canonical_json_sha256(manifest["manifest"])
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "dataset binding|schema validation",
        ):
            validate_routing_manifest(manifest)

    def test_resigned_allowlist_mutation_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        manifest["manifest"]["allowlisted_fields"].append("score")
        manifest["manifest_sha256"] = canonical_json_sha256(manifest["manifest"])
        with self.assertRaises(Phase3PreparationError):
            validate_routing_manifest(manifest)

    def test_missing_decision_reports_exact_paths_and_does_not_select(self):
        decisions = _decisions()
        del decisions["selection"]["seed"]
        del decisions["execution"]["model"]

        report = prepare_phase3_pilot_selection(
            convert_swe_evo_inventory(_inventory()),
            decisions,
        )

        self.assertEqual(report["status"], "decision_input_required")
        self.assertEqual(report["selection_freeze"], "blocked")
        self.assertEqual(
            report["missing_decisions"],
            ["selection.seed", "execution.model"],
        )
        self.assertNotIn("selection", report)

    def test_missing_decisions_are_not_defaulted(self):
        report = decision_input_report({})
        self.assertEqual(report["status"], "decision_input_required")
        self.assertIn("selection.seed", report["missing_decisions"])
        self.assertIn("selection.stratum_quotas", report["missing_decisions"])
        self.assertIn("execution.model", report["missing_decisions"])
        self.assertIn(
            "thresholds.non_inferiority_margin",
            report["missing_decisions"],
        )

    def test_invalid_or_unapproved_decisions_block_selection(self):
        cases = []
        unapproved = _decisions()
        unapproved["authority"]["status"] = "draft"
        cases.append(unapproved)
        inconsistent_sample = _decisions()
        inconsistent_sample["selection"]["sample_size"] = 4
        cases.append(inconsistent_sample)
        not_gold_blind = _decisions()
        not_gold_blind["complexity"]["gold_blind"] = False
        cases.append(not_gold_blind)
        unknown_gold_field = _decisions()
        unknown_gold_field["selection"]["prior_score"] = 1
        cases.append(unknown_gold_field)

        manifest = convert_swe_evo_inventory(_inventory())
        for decisions in cases:
            with self.subTest(decisions=decisions):
                report = prepare_phase3_pilot_selection(manifest, decisions)
                self.assertEqual(report["status"], "decision_input_required")
                self.assertNotIn("selection", report)
                self.assertTrue(report["invalid_decisions"])

    def test_complete_decisions_validate_without_mutation(self):
        decisions = _decisions()
        before = copy.deepcopy(decisions)
        validated = validate_phase3_pilot_decisions(decisions)
        self.assertEqual(validated, before)
        self.assertEqual(decisions, before)

    def test_selection_is_byte_stable_and_replayable(self):
        manifest = convert_swe_evo_inventory(_inventory())
        decisions = _decisions()
        first = prepare_phase3_pilot_selection(manifest, decisions)
        second = prepare_phase3_pilot_selection(
            copy.deepcopy(manifest),
            copy.deepcopy(decisions),
        )

        self.assertEqual(first["status"], "selection_frozen")
        self.assertEqual(first, second)
        self.assertEqual(
            selection_authority_bytes(first),
            selection_authority_bytes(second),
        )
        self.assertEqual(
            replay_phase3_pilot_selection(manifest, decisions, first),
            first,
        )

    def test_every_authorized_input_is_bound_by_authority_digest(self):
        manifest = convert_swe_evo_inventory(_inventory())
        baseline = prepare_phase3_pilot_selection(manifest, _decisions())
        mutations = []

        complexity = _decisions()
        complexity["complexity"]["proxy"]["extraction_rule"] += " Reviewed."
        mutations.append(complexity)
        seed = _decisions()
        seed["selection"]["seed"] += 1
        mutations.append(seed)
        model = _decisions()
        model["execution"]["model"] = "another-operator-selected-model"
        mutations.append(model)
        budget = _decisions()
        budget["execution"]["per_instance_budget"]["max_total_tokens"] += 1
        mutations.append(budget)
        threshold = _decisions()
        threshold["thresholds"]["max_token_cost_ratio"] = 1.5
        mutations.append(threshold)

        for decisions in mutations:
            with self.subTest(decisions=decisions):
                changed = prepare_phase3_pilot_selection(manifest, decisions)
                self.assertEqual(changed["status"], "selection_frozen")
                self.assertNotEqual(
                    baseline["selection_authority_sha256"],
                    changed["selection_authority_sha256"],
                )

    def test_manifest_or_authority_drift_is_rejected(self):
        manifest = convert_swe_evo_inventory(_inventory())
        decisions = _decisions()
        authority = prepare_phase3_pilot_selection(manifest, decisions)
        authority["decision_input_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "selection_authority_sha256",
        ):
            validate_phase3_selection_authority(
                authority,
                routing_manifest=manifest,
                decisions=decisions,
            )

    def test_routing_revision_mismatch_is_structured_and_blocks(self):
        decisions = _decisions()
        decisions["selection"]["metadata_revision"] = "other-revision"
        report = prepare_phase3_pilot_selection(
            convert_swe_evo_inventory(_inventory()),
            decisions,
        )
        self.assertEqual(report["status"], "decision_input_required")
        self.assertEqual(report["selection_freeze"], "blocked")
        self.assertTrue(
            any(
                item["path"] == "selection.metadata_revision"
                for item in report["invalid_decisions"]
            )
        )

    def test_per_instance_materialization_is_complete_equal_and_byte_stable(self):
        selection_authority = _selection_authority()
        arguments = {
            "selection_authority": selection_authority,
            "decisions": _decisions(),
            "task_inputs_by_instance": _task_inputs(selection_authority),
            "execution_profile": _execution_profile(),
            "shared_budget": _shared_budget(),
            "research_authority_sha256": "a" * 64,
            "mode_order": _mode_order(),
        }

        first = materialize_phase3_instance_authorities(**arguments)
        second = materialize_phase3_instance_authorities(
            **copy.deepcopy(arguments)
        )

        self.assertEqual(first, second)
        ordered_ids = selection_authority["selection"]["ordered_instance_ids"]
        self.assertEqual(first["ordered_instance_ids"], ordered_ids)
        self.assertEqual(
            list(first["visible_inputs_by_instance"]), ordered_ids
        )
        self.assertEqual(
            list(first["direct_taskpacks_by_instance"]), ordered_ids
        )
        self.assertEqual(
            list(first["preregistrations_by_instance"]), ordered_ids
        )
        visible_digests = set()
        for instance_id in ordered_ids:
            visible = first["visible_inputs_by_instance"][instance_id]
            direct = first["direct_taskpacks_by_instance"][instance_id]
            preregistration = first["preregistrations_by_instance"][instance_id]
            authorization = preregistration["authorization"]
            visible_digest = canonical_json_sha256(visible)
            visible_digests.add(visible_digest)
            self.assertEqual(
                authorization["selection"]["ordered_instance_ids"],
                [instance_id],
            )
            self.assertEqual(
                set(
                    authorization["mode_controls"]["agentteam_direct"][
                        "taskpack_sha256_by_instance"
                    ]
                ),
                {instance_id},
            )
            self.assertEqual(
                authorization["mode_controls"]["agentteam_direct"][
                    "taskpack_sha256_by_instance"
                ][instance_id],
                direct["taskpack_sha256"],
            )
            self.assertEqual(
                set(
                    authorization["equal_input_bindings"][
                        "per_mode_visible_input_sha256"
                    ].values()
                ),
                {visible_digest},
            )
            self.assertEqual(
                len(
                    {
                        canonical_json_sha256(budget)
                        for budget in authorization["budgets"].values()
                    }
                ),
                1,
            )
        self.assertEqual(len(visible_digests), len(ordered_ids))

    def test_individual_builders_bind_approved_selection_and_budget(self):
        selection_authority = _selection_authority()
        instance_id = selection_authority["selection"]["ordered_instance_ids"][0]
        task_input = _task_inputs(selection_authority)[instance_id]
        visible = build_phase3_instance_visible_input(
            instance_id=instance_id,
            selection_authority=selection_authority,
            decisions=_decisions(),
            task_input=task_input,
            execution_profile=_execution_profile(),
        )
        direct = build_phase3_instance_direct_taskpack(
            instance_id=instance_id,
            selection_authority=selection_authority,
            decisions=_decisions(),
            visible_inputs=visible,
            shared_budget=_shared_budget(),
        )
        preregistration = build_phase3_instance_preregistration(
            instance_id=instance_id,
            selection_authority=selection_authority,
            decisions=_decisions(),
            visible_inputs=visible,
            direct_taskpack=direct,
            shared_budget=_shared_budget(),
            research_authority_sha256="a" * 64,
            mode_order=_mode_order(),
        )

        self.assertEqual(
            direct["taskpack"]["visible_inputs"],
            visible,
        )
        self.assertEqual(
            preregistration["authorization"]["budgets"]["single_codex"],
            _shared_budget(),
        )

    def test_materialization_rejects_missing_or_reused_instance_input(self):
        selection_authority = _selection_authority()
        task_inputs = _task_inputs(selection_authority)
        task_inputs.pop(next(iter(task_inputs)))
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "cover the selection exactly",
        ):
            materialize_phase3_instance_authorities(
                selection_authority=selection_authority,
                decisions=_decisions(),
                task_inputs_by_instance=task_inputs,
                execution_profile=_execution_profile(),
                shared_budget=_shared_budget(),
                research_authority_sha256="a" * 64,
                mode_order=_mode_order(),
            )

        task_inputs = _task_inputs(selection_authority)
        first, second = list(task_inputs)[:2]
        task_inputs[second] = copy.deepcopy(task_inputs[first])
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "cannot reuse shared_visible_inputs",
        ):
            materialize_phase3_instance_authorities(
                selection_authority=selection_authority,
                decisions=_decisions(),
                task_inputs_by_instance=task_inputs,
                execution_profile=_execution_profile(),
                shared_budget=_shared_budget(),
                research_authority_sha256="a" * 64,
                mode_order=_mode_order(),
            )

    def test_missing_task_authority_evaluator_fields_and_budget_drift_fail_closed(self):
        selection_authority = _selection_authority()
        instance_id = selection_authority["selection"]["ordered_instance_ids"][0]
        base = {
            "instance_id": instance_id,
            "selection_authority": selection_authority,
            "decisions": _decisions(),
            "execution_profile": _execution_profile(),
        }
        cases = []
        missing_commit = _task_inputs(selection_authority)[instance_id]
        del missing_commit["repository"]["commit"]
        cases.append(missing_commit)
        missing_goal = _task_inputs(selection_authority)[instance_id]
        del missing_goal["task"]["goal"]
        cases.append(missing_goal)
        missing_command = _task_inputs(selection_authority)[instance_id]
        missing_command["task"]["acceptance_commands"] = []
        cases.append(missing_command)
        evaluator_material = _task_inputs(selection_authority)[instance_id]
        evaluator_material["task"]["gold_patch"] = "restricted"
        cases.append(evaluator_material)
        for task_input in cases:
            with self.subTest(task_input=task_input), self.assertRaises(
                Phase3PreparationError
            ):
                build_phase3_instance_visible_input(
                    task_input=task_input,
                    **base,
                )

        visible = build_phase3_instance_visible_input(
            task_input=_task_inputs(selection_authority)[instance_id],
            **base,
        )
        changed_budget = _shared_budget()
        changed_budget["max_total_tokens"] += 1
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "does not match the approved decision",
        ):
            build_phase3_instance_direct_taskpack(
                instance_id=instance_id,
                selection_authority=selection_authority,
                decisions=_decisions(),
                visible_inputs=visible,
                shared_budget=changed_budget,
            )

        direct = build_phase3_instance_direct_taskpack(
            instance_id=instance_id,
            selection_authority=selection_authority,
            decisions=_decisions(),
            visible_inputs=visible,
            shared_budget=_shared_budget(),
        )
        direct["taskpack"]["policy"]["gold_visibility"] = "runtime_visible"
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "taskpack_sha256|visibility policy",
        ):
            validate_phase3_instance_direct_taskpack(direct)


if __name__ == "__main__":
    unittest.main()

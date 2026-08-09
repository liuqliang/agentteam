from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from agentteam_runtime.benchmark_adapter import (
    BenchmarkAdapterError,
    build_benchmark_instance_selection,
    validate_benchmark_instance_selection,
)
from agentteam_runtime import taskpack as taskpack_module
from agentteam_runtime.benchmark_preregistration import (
    BenchmarkPreregistrationError,
    authorize_mode_runtime_path,
    build_benchmark_preregistration,
    publish_benchmark_preregistration,
    validate_benchmark_preregistration,
)
from agentteam_runtime.decision_runtime import (
    load_run_decision_binding,
    publish_run_decision_binding,
    require_active_inherited_decision,
    validate_taskpack_decision_contract,
)
from agentteam_runtime.experiment_contract import (
    EXPERIMENT_MODES,
    canonical_json_bytes,
    canonical_json_sha256,
)
from agentteam_runtime.experiment_gates import (
    Phase2GateError,
    resolve_gate_spec,
    run_gate_controller,
    validate_gate_relation,
)
from agentteam_runtime.phase3_readiness import (
    Phase3ReadinessError,
    build_phase3_readiness_receipt,
    phase3_readiness_receipt_sha256,
    publish_phase3_readiness_receipt,
    validate_phase3_readiness_receipt,
)


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = ROOT.parents[1]
SCHEMA_PATH = ROOT / "schemas" / "phase3_readiness_receipt.schema.json"
BLUEPRINT_PATH = (
    ROOT
    / "implementation_artifacts"
    / "plans"
    / "2026-08-09-phase3a-execution-contract-v3.blueprint.json"
)
RESEARCH_AUTHORITY_PATH = (
    ROOT / "research" / "agentteam_research_positioning.md"
)
MODES = tuple(EXPERIMENT_MODES)


def _instance(instance_id, stratum):
    return {
        "instance_id": instance_id,
        "repository": "fixture/alpha",
        "complexity_stratum": stratum,
        "language": "python",
        "tags": ["pilot"],
    }


def _metadata():
    return {
        "schema_version": "swe_evo_metadata.v1",
        "benchmark": "swe_evo",
        "metadata_revision": "phase3-readiness-fixture-r1",
        "instances": [
            _instance("fixture-low-1", "low"),
            _instance("fixture-low-2", "low"),
            _instance("fixture-high-1", "high"),
            _instance("fixture-high-2", "high"),
        ],
    }


def _selection(metadata=None):
    return build_benchmark_instance_selection(
        metadata or _metadata(),
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


def _shared_visible_inputs():
    return {
        "repository": {
            "source": "local:phase3-readiness-fixture",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "git_object_format": "sha1",
        },
        "runtime": {
            "agentteam_release_commit": "3" * 40,
            "codex_cli_version": "not-invoked",
            "environment_version": "phase3-readiness-fixture-v1",
        },
        "task": {
            "goal": "Verify the frozen benchmark authority without execution.",
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
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_phase3_benchmark_readiness",
                ]
            ],
        },
        "model": {
            "model": "gpt-5.6-sol",
            "reasoning_profile": "high",
            "service_configuration_sha256": "4" * 64,
        },
        "execution": {
            "tools_sha256": "5" * 64,
            "network_policy": "disabled",
            "sandbox_policy_sha256": "6" * 64,
            "permission_policy_sha256": "7" * 64,
            "host_class": "local-fixture",
            "cpu_limit": 1,
            "memory_limit_bytes": 1024 * 1024 * 1024,
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


def _preregistration(selection):
    return build_benchmark_preregistration(
        research_authority_sha256=hashlib.sha256(
            RESEARCH_AUTHORITY_PATH.read_bytes()
        ).hexdigest(),
        selection_sha256=selection["selection_sha256"],
        ordered_instance_ids=selection["ordered_instance_ids"],
        shared_visible_inputs=_shared_visible_inputs(),
        shared_budget=_shared_budget(),
        direct_taskpack_sha256_by_instance={
            instance_id: canonical_json_sha256(
                {"kind": "frozen_direct_taskpack", "instance_id": instance_id}
            )
            for instance_id in selection["ordered_instance_ids"]
        },
        non_inferiority_margin=0.05,
        max_token_cost_ratio=1.25,
        max_wall_time_cost_ratio=1.20,
        preselected_secondary_benefit_metric="corrective_operator_interventions",
        mode_order=[
            ["single_codex", "agentteam_direct", "agentteam_full"],
            ["agentteam_direct", "agentteam_full", "single_codex"],
            ["agentteam_full", "single_codex", "agentteam_direct"],
        ],
    )


def _check(evidence):
    return {
        "status": "passed",
        "evidence_sha256": canonical_json_sha256(evidence),
    }


def _minimal_receipt():
    selection = _selection()
    preregistration = _preregistration(selection)
    visible_digest = next(
        iter(
            preregistration["authorization"]["equal_input_bindings"][
                "per_mode_visible_input_sha256"
            ].values()
        )
    )
    mode_bindings = {
        mode: {
            "visible_input_sha256": visible_digest,
            "budget_sha256": canonical_json_sha256(
                preregistration["authorization"]["budgets"][mode]
            ),
            "runtime_root": f"modes/{mode}",
        }
        for mode in MODES
    }
    results = {
        name: _check(name)
        for name in (
            "selection_replay",
            "selection_mutation_detection",
            "preregistration_replay",
            "preregistration_mutation_detection",
            "equal_input_binding",
            "equal_budget_binding",
            "mode_isolation",
            "decision_binding_replay",
            "provider_absence",
        )
    }
    return build_phase3_readiness_receipt(
        fixture={
            "kind": "local_fixture",
            "benchmark": "swe_evo",
            "metadata_revision": "phase3-readiness-fixture-r1",
            "metadata_sha256": canonical_json_sha256(_metadata()),
        },
        bindings={
            "selection_sha256": selection["selection_sha256"],
            "ordered_instance_ids": selection["ordered_instance_ids"],
            "preregistration_authorization_sha256": preregistration[
                "authorization_sha256"
            ],
            "decision_contract_sha256": "c" * 64,
            "inherited_decision_id": "DEC-P3-readiness-execution",
            "decision_replay_status": "idempotent",
        },
        mode_reconciliation=mode_bindings,
        isolation_reconciliation={
            "gold_visibility": "evaluator_only",
            "cross_mode_artifact_access": "denied",
            "independent_runtime_roots": True,
            "sibling_access_denials": 3,
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


def _p3_gate_declaration():
    return {
        "gate_id": "P3-READY",
        "executor": "deterministic_controller",
        "controller_entrypoint": "phase3_readiness_controller_v1",
        "relation_validator": "phase3_readiness_relation_v1",
        "evidence_schema": (
            "experiments/native_agentteam_runtime/schemas/"
            "phase3_readiness_receipt.schema.json"
        ),
        "operator_authorization_required": False,
    }


def _read_receipt_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class Phase3BenchmarkReadinessTests(unittest.TestCase):
    def test_receipt_schema_is_valid_draft_2020_12(self):
        Draft202012Validator.check_schema(_read_receipt_schema())

    def test_provider_free_authority_path_publishes_idempotent_receipt(self):
        metadata = _metadata()
        selection = _selection(metadata)
        replayed_selection = _selection(metadata)
        self.assertEqual(
            canonical_json_bytes(selection),
            canonical_json_bytes(replayed_selection),
        )
        validate_benchmark_instance_selection(selection, metadata=metadata)

        changed_selection = copy.deepcopy(selection)
        changed_selection["ordered_instance_ids"].reverse()
        with self.assertRaises(BenchmarkAdapterError):
            validate_benchmark_instance_selection(changed_selection)

        preregistration = _preregistration(selection)
        authorization = preregistration["authorization"]
        changed_preregistration = copy.deepcopy(preregistration)
        changed_preregistration["authorization"]["budgets"]["agentteam_full"][
            "max_total_tokens"
        ] += 1
        with self.assertRaises(BenchmarkPreregistrationError):
            validate_benchmark_preregistration(changed_preregistration)

        blueprint = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))
        task_ids = [item["task_id"] for item in blueprint["tasks"]]
        decision_contract = validate_taskpack_decision_contract(
            blueprint["decision_contract"],
            task_ids=task_ids,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority_root = root / "authority"
            first_preregistration = publish_benchmark_preregistration(
                authority_root, preregistration
            )
            replayed_preregistration = publish_benchmark_preregistration(
                authority_root, preregistration
            )
            self.assertTrue(first_preregistration["created"])
            self.assertFalse(replayed_preregistration["created"])
            self.assertEqual(first_preregistration, {
                **replayed_preregistration,
                "created": True,
            })

            runtime_root = root / "runtime"
            sibling_denials = 0
            mode_bindings = {}
            visible_digest = canonical_json_sha256(
                authorization["equal_input_bindings"]["shared_visible_inputs"]
            )
            for index, mode in enumerate(MODES):
                own_path = runtime_root / "modes" / mode / "binding.json"
                own_path.parent.mkdir(parents=True, exist_ok=True)
                own_path.write_bytes(
                    canonical_json_bytes(
                        {
                            "visible_input_sha256": visible_digest,
                            "budget_sha256": canonical_json_sha256(
                                authorization["budgets"][mode]
                            ),
                        }
                    )
                    + b"\n"
                )
                self.assertEqual(
                    authorize_mode_runtime_path(
                        preregistration,
                        runtime_root=runtime_root,
                        mode=mode,
                        candidate_path=own_path,
                    ),
                    own_path.resolve(),
                )
                sibling = MODES[(index + 1) % len(MODES)]
                sibling_path = runtime_root / "modes" / sibling / "binding.json"
                with self.assertRaises(BenchmarkPreregistrationError):
                    authorize_mode_runtime_path(
                        preregistration,
                        runtime_root=runtime_root,
                        mode=mode,
                        candidate_path=sibling_path,
                    )
                sibling_denials += 1
                mode_bindings[mode] = {
                    "visible_input_sha256": visible_digest,
                    "budget_sha256": canonical_json_sha256(
                        authorization["budgets"][mode]
                    ),
                    "runtime_root": authorization["isolation"][
                        "mode_runtime_roots"
                    ][mode],
                }

            work_root = root / "decision-work"
            frozen_dir = work_root / "frozen" / "phase3-readiness"
            run_dir = work_root / "runs" / "phase3-readiness"
            frozen_dir.mkdir(parents=True)
            (frozen_dir / "manifest.json").write_bytes(
                canonical_json_bytes({"digest_sha256": "d" * 64}) + b"\n"
            )
            taskpack = {
                "taskpack_id": blueprint["taskpack"]["taskpack_id"],
                "decision_contract": decision_contract,
            }
            binding = publish_run_decision_binding(
                work_root,
                frozen_dir,
                run_dir,
                taskpack,
                task_ids=task_ids,
            )
            replayed_binding = publish_run_decision_binding(
                work_root,
                frozen_dir,
                run_dir,
                taskpack,
                task_ids=task_ids,
            )
            self.assertEqual(binding, replayed_binding)
            self.assertEqual(binding, load_run_decision_binding(run_dir))
            inherited = require_active_inherited_decision(binding, "P3-03")
            self.assertEqual(inherited, "DEC-P3-readiness-execution")

            provider_artifacts = [
                path
                for path in root.rglob("*")
                if path.is_file()
                and (
                    path.name in {"started.json", "usage.json", "terminal.json"}
                    or "model-invocation" in path.name
                    or "provider-session" in path.name
                )
            ]
            self.assertEqual(provider_artifacts, [])

            results = {
                "selection_replay": _check(selection["selection_sha256"]),
                "selection_mutation_detection": _check(
                    {"rejected": "changed_ordered_instance_ids"}
                ),
                "preregistration_replay": _check(
                    first_preregistration["sha256"]
                ),
                "preregistration_mutation_detection": _check(
                    {"rejected": "unequal_agentteam_full_budget"}
                ),
                "equal_input_binding": _check(
                    {mode: mode_bindings[mode]["visible_input_sha256"] for mode in MODES}
                ),
                "equal_budget_binding": _check(
                    {mode: mode_bindings[mode]["budget_sha256"] for mode in MODES}
                ),
                "mode_isolation": _check(
                    {"runtime_roots": mode_bindings, "sibling_denials": sibling_denials}
                ),
                "decision_binding_replay": _check(binding),
                "provider_absence": _check(
                    {"invocation_artifacts": [], "provider_calls": 0}
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
                    "decision_contract_sha256": canonical_json_sha256(
                        decision_contract
                    ),
                    "inherited_decision_id": inherited,
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
            self.assertEqual(
                validate_phase3_readiness_receipt(receipt), receipt
            )

            receipt_path = root / "acceptance" / "phase3-readiness.json"
            first_receipt = publish_phase3_readiness_receipt(
                receipt_path, receipt
            )
            replayed_receipt = publish_phase3_readiness_receipt(
                receipt_path, receipt
            )
            self.assertTrue(first_receipt["created"])
            self.assertFalse(replayed_receipt["created"])
            self.assertEqual(
                first_receipt["sha256"], canonical_json_sha256(receipt)
            )
            self.assertEqual(
                json.loads(receipt_path.read_text(encoding="utf-8")), receipt
            )

    def test_receipt_rejects_zero_usage_claim_and_digest_mutation(self):
        selection = _selection()
        preregistration = _preregistration(selection)
        visible_digest = next(
            iter(
                preregistration["authorization"]["equal_input_bindings"][
                    "per_mode_visible_input_sha256"
                ].values()
            )
        )
        mode_bindings = {
            mode: {
                "visible_input_sha256": visible_digest,
                "budget_sha256": canonical_json_sha256(
                    preregistration["authorization"]["budgets"][mode]
                ),
                "runtime_root": f"modes/{mode}",
            }
            for mode in MODES
        }
        results = {
            name: _check(name)
            for name in (
                "selection_replay",
                "selection_mutation_detection",
                "preregistration_replay",
                "preregistration_mutation_detection",
                "equal_input_binding",
                "equal_budget_binding",
                "mode_isolation",
                "decision_binding_replay",
                "provider_absence",
            )
        }
        receipt = build_phase3_readiness_receipt(
            fixture={
                "kind": "local_fixture",
                "benchmark": "swe_evo",
                "metadata_revision": "phase3-readiness-fixture-r1",
                "metadata_sha256": canonical_json_sha256(_metadata()),
            },
            bindings={
                "selection_sha256": selection["selection_sha256"],
                "ordered_instance_ids": selection["ordered_instance_ids"],
                "preregistration_authorization_sha256": preregistration[
                    "authorization_sha256"
                ],
                "decision_contract_sha256": "c" * 64,
                "inherited_decision_id": "DEC-P3-readiness-execution",
                "decision_replay_status": "idempotent",
            },
            mode_reconciliation=mode_bindings,
            isolation_reconciliation={
                "gold_visibility": "evaluator_only",
                "cross_mode_artifact_access": "denied",
                "independent_runtime_roots": True,
                "sibling_access_denials": 3,
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
        validate_phase3_readiness_receipt(receipt)

        false_zero_claim = copy.deepcopy(receipt)
        false_zero_claim["usage_reconciliation"]["token_totals"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        false_zero_claim["receipt_sha256"] = (
            phase3_readiness_receipt_sha256(false_zero_claim)
        )
        with self.assertRaisesRegex(Phase3ReadinessError, "token_totals"):
            validate_phase3_readiness_receipt(false_zero_claim)

        changed = copy.deepcopy(receipt)
        changed["bindings"]["ordered_instance_ids"].reverse()
        with self.assertRaisesRegex(Phase3ReadinessError, "receipt_sha256"):
            validate_phase3_readiness_receipt(changed)

        unequal_modes = copy.deepcopy(receipt)
        unequal_modes["mode_reconciliation"]["agentteam_full"][
            "visible_input_sha256"
        ] = "f" * 64
        unequal_modes["receipt_sha256"] = (
            phase3_readiness_receipt_sha256(unequal_modes)
        )
        with self.assertRaisesRegex(
            Phase3ReadinessError,
            "input and budget bindings are not equal",
        ):
            validate_phase3_readiness_receipt(unequal_modes)

    def test_p3_ready_gate_registry_is_exact_and_action_free(self):
        declaration = _p3_gate_declaration()
        spec = resolve_gate_spec(declaration)
        self.assertEqual(spec.gate_id, "P3-READY")
        self.assertFalse(spec.action_required)

        drifted = copy.deepcopy(declaration)
        drifted["relation_validator"] = "unregistered_relation_v1"
        with self.assertRaisesRegex(
            Phase2GateError,
            "registry binding mismatch",
        ):
            resolve_gate_spec(drifted)

        with_action = copy.deepcopy(declaration)
        with_action["controller_action_input"] = {}
        with self.assertRaisesRegex(
            Phase2GateError,
            "does not accept controller action input",
        ):
            resolve_gate_spec(with_action)

    def test_phase3_model_worker_blueprint_accepts_registered_gate(self):
        blueprint = json.loads(BLUEPRINT_PATH.read_text(encoding="utf-8"))
        gate = blueprint["post_backlog_gates"][0]
        gate.update(
            {
                "controller_entrypoint": (
                    "phase3_readiness_controller_v1"
                ),
                "relation_validator": "phase3_readiness_relation_v1",
                "operator_authorization_required": False,
            }
        )
        taskpack_module._validate_taskpack_blueprint_schema(blueprint)
        taskpack_module._validate_taskpack_blueprint(
            blueprint,
            project_root=REPOSITORY_ROOT,
            blueprint_relative_path=BLUEPRINT_PATH.relative_to(
                REPOSITORY_ROOT
            ).as_posix(),
        )

    def test_p3_ready_controller_validates_and_seals_relation(self):
        receipt = _minimal_receipt()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "phase3-readiness.json"
            publish_phase3_readiness_receipt(artifact_path, receipt)
            context = {
                "repository_root": str(REPOSITORY_ROOT),
                "epoch_number": 1,
                "epoch_sha256": "e" * 64,
            }
            spec = resolve_gate_spec(_p3_gate_declaration())
            relation = validate_gate_relation(
                spec,
                artifact_path,
                context,
            )
            self.assertEqual(relation["relation_status"], "passed")
            self.assertEqual(relation["provider_calls"], 0)
            self.assertEqual(relation["target_mutations"], 0)
            self.assertEqual(relation["ordered_instance_count"], 2)

            result = run_gate_controller(
                spec,
                artifact_path,
                context,
                result_path=root / "controller-result.json",
            )
            self.assertEqual(
                result["schema_version"],
                "gate_controller_result.v1",
            )
            self.assertEqual(result["controller_status"], "passed")
            self.assertEqual(result["provider_calls"], 0)


if __name__ == "__main__":
    unittest.main()

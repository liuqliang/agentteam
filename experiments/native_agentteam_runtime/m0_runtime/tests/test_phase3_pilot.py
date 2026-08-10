from __future__ import annotations

import copy
import unittest

from agentteam_runtime.benchmark_adapter import build_benchmark_instance_selection
from agentteam_runtime.benchmark_preregistration import (
    build_benchmark_preregistration,
)
from agentteam_runtime.experiment_contract import canonical_json_sha256
from agentteam_runtime.phase3_pilot import (
    LIVE_AUTHORIZATION_SCHEMA_VERSION,
    Phase3PilotError,
    admit_phase3_live_launch,
    build_phase3_pilot_contract,
    validate_phase3_live_authorization,
    validate_phase3_pilot_contract,
)


MODES = ["single_codex", "agentteam_direct", "agentteam_full"]
MODE_ORDER = [
    ["single_codex", "agentteam_direct", "agentteam_full"],
    ["agentteam_direct", "agentteam_full", "single_codex"],
    ["agentteam_full", "single_codex", "agentteam_direct"],
]


def _selection():
    metadata = {
        "schema_version": "swe_evo_metadata.v1",
        "benchmark": "swe_evo",
        "metadata_revision": "fixture-r1",
        "instances": [
            {
                "instance_id": "swe-evo-001",
                "repository": "example/one",
                "complexity_stratum": "lower",
                "language": "python",
                "tags": ["fixture"],
            },
            {
                "instance_id": "swe-evo-002",
                "repository": "example/two",
                "complexity_stratum": "upper",
                "language": "python",
                "tags": ["fixture"],
            },
        ],
    }
    return build_benchmark_instance_selection(
        metadata,
        metadata_revision="fixture-r1",
        filters={
            "repositories": [],
            "languages": ["python"],
            "required_tags": ["fixture"],
            "excluded_instance_ids": [],
        },
        seed=20260810,
        stratum_quotas={"lower": 1, "upper": 1},
    )


def _visible_inputs(instance_id, *, model="gpt-test"):
    suffix = "1" if instance_id.endswith("001") else "2"
    return {
        "repository": {
            "source": f"local:{instance_id}",
            "commit": suffix * 40,
            "tree": ("3" if suffix == "1" else "4") * 40,
            "git_object_format": "sha1",
        },
        "runtime": {
            "agentteam_release_commit": "5" * 40,
            "codex_cli_version": "codex-cli-test-v1",
            "environment_version": "phase3b-fixture-v1",
        },
        "task": {
            "goal": f"Implement benchmark instance {instance_id}.",
            "constraints": ["Do not read evaluator gold."],
            "non_goals": ["Do not change benchmark scope."],
            "acceptance_commands": [["python3", "-m", "unittest"]],
        },
        "model": {
            "model": model,
            "reasoning_profile": "high",
            "service_configuration_sha256": "6" * 64,
        },
        "execution": {
            "tools_sha256": "7" * 64,
            "network_policy": "disabled",
            "sandbox_policy_sha256": "8" * 64,
            "permission_policy_sha256": "9" * 64,
            "host_class": "fixture-host",
            "cpu_limit": 4,
            "memory_limit_bytes": 8 * 1024 * 1024 * 1024,
            "external_services_sha256": "a" * 64,
            "benchmark_visible_tests_sha256": "b" * 64,
            "termination_policy_sha256": "c" * 64,
            "shared_dependency_cache_sha256": "d" * 64,
        },
    }


def _budget():
    return {
        "max_total_tokens": 100000,
        "max_wall_time_seconds": 3600,
        "max_operator_interactions": 1,
        "allowed_operator_input_types": ["decision_escalation"],
    }


def _preregistrations(selection, *, changed_model_instance=None):
    result = {}
    for index, instance_id in enumerate(selection["ordered_instance_ids"]):
        model = "other-model" if instance_id == changed_model_instance else "gpt-test"
        result[instance_id] = build_benchmark_preregistration(
            research_authority_sha256="e" * 64,
            selection_sha256=selection["selection_sha256"],
            ordered_instance_ids=[instance_id],
            shared_visible_inputs=_visible_inputs(instance_id, model=model),
            shared_budget=_budget(),
            direct_taskpack_sha256_by_instance={
                instance_id: ("f" if index == 0 else "0") * 64
            },
            non_inferiority_margin=0.05,
            max_token_cost_ratio=1.25,
            max_wall_time_cost_ratio=1.20,
            preselected_secondary_benefit_metric=(
                "corrective_operator_interventions"
            ),
            mode_order=MODE_ORDER,
        )
    return result


def _readiness_binding():
    return {
        "gate_id": "P3-READY",
        "controller_id": "phase3_readiness_controller_v1",
        "relation_id": "phase3_readiness_relation_v1",
        "evidence_sha256": "1" * 64,
        "receipt_content_sha256": "2" * 64,
        "integration_head": "3" * 40,
    }


def _dataset_binding():
    return {
        "benchmark": "swe_evo",
        "source_repository": "https://github.com/SWE-EVO/SWE-EVO.git",
        "source_commit": "4" * 40,
        "artifact_path": "hf_out/hf_dataset/test/data-00000-of-00001.arrow",
        "artifact_sha256": "5" * 64,
        "split": "test",
        "row_count": 48,
    }


def _build(*, preregistrations=None):
    selection = _selection()
    preregistrations = preregistrations or _preregistrations(selection)
    contract = build_phase3_pilot_contract(
        readiness_binding=_readiness_binding(),
        dataset_binding=_dataset_binding(),
        selection=selection,
        preregistrations_by_instance=preregistrations,
        retry_policy={
            "provider_retry_limit": 1,
            "retryable_failures": ["provider_transport_error"],
            "budget_accounting": (
                "all_reported_usage_and_wall_time_count_toward_instance_budget"
            ),
        },
        abort_conditions={
            "usage_coverage_below_percent": 100,
            "cross_mode_isolation_violation": True,
            "gold_visibility_violation": True,
            "contract_digest_mismatch": True,
            "budget_ceiling_reached": True,
        },
    )
    return selection, preregistrations, contract


def _live_authorization(contract, *, decision="approved"):
    body = contract["contract"]
    return {
        "schema_version": LIVE_AUTHORIZATION_SCHEMA_VERSION,
        "decision": decision,
        "operator_identity": "fixture-operator",
        "authorized_at": "2026-08-10T12:00:00Z",
        "gate_id": "P3-LIVE",
        "epoch_number": 1,
        "epoch_sha256": "6" * 64,
        "pilot_contract_sha256": contract["contract_sha256"],
        "readiness_evidence_sha256": body["readiness_binding"][
            "evidence_sha256"
        ],
        "selection_sha256": body["selection"]["selection_sha256"],
        "agentteam_release_commit": body["execution_profile"]["runtime"][
            "agentteam_release_commit"
        ],
        "model": body["execution_profile"]["model"]["model"],
        "reasoning_profile": body["execution_profile"]["model"][
            "reasoning_profile"
        ],
        "max_total_tokens": body["aggregate_budget_ceiling"][
            "maximum_total_tokens"
        ],
        "max_wall_time_seconds": body["aggregate_budget_ceiling"][
            "maximum_wall_time_seconds"
        ],
        "max_inflight_model_invocations": 1,
        "modes": MODES,
    }


class Phase3PilotTests(unittest.TestCase):
    def test_build_binds_distinct_instance_inputs_and_aggregate_ceiling(self):
        selection, preregistrations, contract = _build()

        self.assertFalse(contract["contract"]["provider_calls_authorized"])
        self.assertEqual(
            contract["contract"]["selection"]["ordered_instance_ids"],
            selection["ordered_instance_ids"],
        )
        self.assertEqual(
            contract["contract"]["aggregate_budget_ceiling"][
                "maximum_total_tokens"
            ],
            1_800_000,
        )
        self.assertEqual(
            contract["contract_sha256"],
            canonical_json_sha256(contract["contract"]),
        )
        validate_phase3_pilot_contract(
            contract,
            selection=selection,
            preregistrations_by_instance=preregistrations,
        )

    def test_one_preregistration_cannot_cover_multiple_instances(self):
        selection = _selection()
        preregistrations = _preregistrations(selection)
        first_id = selection["ordered_instance_ids"][0]
        preregistrations[first_id]["authorization"]["selection"][
            "ordered_instance_ids"
        ] = selection["ordered_instance_ids"]
        preregistrations[first_id]["authorization"]["mode_controls"][
            "agentteam_direct"
        ]["taskpack_sha256_by_instance"][selection["ordered_instance_ids"][1]] = (
            "a" * 64
        )
        preregistrations[first_id]["authorization_sha256"] = canonical_json_sha256(
            preregistrations[first_id]["authorization"]
        )

        with self.assertRaisesRegex(Phase3PilotError, "exactly its own instance"):
            _build(preregistrations=preregistrations)

    def test_execution_profile_drift_between_instances_is_rejected(self):
        selection = _selection()
        preregistrations = _preregistrations(
            selection,
            changed_model_instance=selection["ordered_instance_ids"][1],
        )

        with self.assertRaisesRegex(Phase3PilotError, "same runtime, model"):
            _build(preregistrations=preregistrations)

    def test_contract_mutation_invalidates_digest(self):
        _, _, contract = _build()
        changed = copy.deepcopy(contract)
        changed["contract"]["instance_bindings"][0][
            "maximum_total_tokens"
        ] += 1

        with self.assertRaisesRegex(Phase3PilotError, "contract_sha256"):
            validate_phase3_pilot_contract(changed)

    def test_live_launch_requires_exact_epoch_bound_authorization(self):
        selection, preregistrations, contract = _build()
        authorization = _live_authorization(contract)

        permit = admit_phase3_live_launch(
            contract,
            authorization,
            selection=selection,
            preregistrations_by_instance=preregistrations,
            expected_epoch_number=1,
            expected_epoch_sha256="6" * 64,
        )
        self.assertEqual(permit["gate_id"], "P3-LIVE")
        self.assertEqual(
            permit["maximum_total_tokens"],
            contract["contract"]["aggregate_budget_ceiling"][
                "maximum_total_tokens"
            ],
        )

    def test_rejected_stale_or_expanded_authorization_is_denied(self):
        selection, preregistrations, contract = _build()
        cases = []
        rejected = _live_authorization(contract, decision="rejected")
        cases.append((rejected, 1, "6" * 64, "not approved"))
        stale = _live_authorization(contract)
        cases.append((stale, 2, "6" * 64, "epoch number is stale"))
        expanded = _live_authorization(contract)
        expanded["max_total_tokens"] += 1
        cases.append((expanded, 1, "6" * 64, "does not match"))

        for authorization, epoch, epoch_sha, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(Phase3PilotError, message):
                    validate_phase3_live_authorization(
                        authorization,
                        pilot_contract=contract,
                        selection=selection,
                        preregistrations_by_instance=preregistrations,
                        expected_epoch_number=epoch,
                        expected_epoch_sha256=epoch_sha,
                    )

    def test_self_consistent_forged_ceiling_is_rejected_from_sources(self):
        selection, preregistrations, contract = _build()
        changed = copy.deepcopy(contract)
        changed["contract"]["instance_bindings"][0][
            "maximum_total_tokens"
        ] += 1
        changed["contract"]["aggregate_budget_ceiling"][
            "maximum_total_tokens"
        ] += 1
        changed["contract_sha256"] = canonical_json_sha256(changed["contract"])

        with self.assertRaisesRegex(
            Phase3PilotError,
            "does not match per-instance preregistrations",
        ):
            validate_phase3_pilot_contract(
                changed,
                selection=selection,
                preregistrations_by_instance=preregistrations,
            )


if __name__ == "__main__":
    unittest.main()

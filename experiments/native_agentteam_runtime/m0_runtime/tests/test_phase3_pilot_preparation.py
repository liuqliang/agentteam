import copy
import json
import sys
import tempfile
import types
import unittest
from unittest import mock

import agentteam_runtime.phase3_pilot_preparation as preparation

from agentteam_runtime.phase3_pilot_preparation import (
    FIXED_DATASET_ARTIFACT_SHA256,
    FIXED_DATASET_ROW_COUNT,
    FIXED_DATASET_SPLIT,
    FIXED_SOURCE_COMMIT,
    EXPECTED_ELIGIBLE_POPULATION_COUNTS,
    EXPECTED_POPULATION_COUNTS,
    EXPECTED_SELECTION_PREVIEW,
    Phase3PreparationError,
    build_phase3_aggregate_pilot_contract,
    build_phase3_instance_direct_taskpack,
    build_phase3_instance_preregistration,
    build_phase3_instance_visible_input,
    build_phase3_provider_free_preflight_receipt,
    convert_swe_evo_inventory,
    complexity_projection_bytes,
    decision_input_report,
    fixed_dataset_binding,
    materialize_phase3_instance_authorities,
    prepare_phase3_pilot_selection,
    project_swe_evo_arrow_inventory,
    provider_free_preflight_receipt_bytes,
    replay_phase3_pilot_selection,
    replay_phase3_complexity_selection,
    routing_manifest_bytes,
    selection_authority_bytes,
    validate_routing_manifest,
    validate_phase3_complexity_projection,
    validate_phase3_pilot_decisions,
    validate_phase3_instance_direct_taskpack,
    validate_phase3_instance_materialization,
    validate_phase3_provider_free_preflight_receipt,
    validate_phase3_selection_authority,
)
from agentteam_runtime.experiment_contract import canonical_json_sha256
from agentteam_runtime.resource_envelope import (
    approved_phase3_resource_envelope_binding,
    build_resource_evidence,
    canonical_resource_envelope_sha256,
)
from agentteam_runtime.phase3_pilot import (
    LIVE_AUTHORIZATION_SCHEMA_VERSION,
    Phase3PilotError,
    admit_phase3_live_launch,
)


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


def _resource_preflight_receipt():
    binding = approved_phase3_resource_envelope_binding()
    binding_sha256 = canonical_resource_envelope_sha256(binding)
    probe_specs = (
        ("single_codex", "workload"),
        ("single_codex", "evaluator"),
        ("agentteam_direct", "control_plane"),
        ("agentteam_direct", "workload"),
        ("agentteam_direct", "evaluator"),
        ("agentteam_full", "control_plane"),
        ("agentteam_full", "workload"),
        ("agentteam_full", "evaluator"),
    )
    receipt = {
        "schema_version": "phase3_resource_preflight.v1",
        "status": "passed",
        "provider_free": True,
        "pilot_id": "PILOT-PREPARATION-V2",
        "binding_sha256": binding_sha256,
        "probe_records": [
            {
                "probe_id": f"{mode}:{scope}",
                "mode": mode,
                "scope": scope,
                "returncode": 0,
                "evidence": build_resource_evidence(
                    binding=binding,
                    scope=scope,
                    identity={"control_group": f"/{mode}/{scope}"},
                    counters={},
                ),
            }
            for mode, scope in probe_specs
        ],
        "aggregate_evidence": {
            "project": build_resource_evidence(
                binding=binding,
                scope="project",
                identity={"control_group": "/project"},
                counters={},
            ),
            "modes": {
                mode: build_resource_evidence(
                    binding=binding,
                    scope="mode",
                    identity={"control_group": f"/project/{mode}"},
                    counters={},
                )
                for mode in (
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                )
            },
        },
        "cleanup": {
            "cleanup_attempted": True,
            "cleanup_complete": True,
            "mode_cleanup": {
                mode: {"cleanup_complete": True}
                for mode in (
                    "single_codex",
                    "agentteam_direct",
                    "agentteam_full",
                )
            },
        },
        "provider_reconciliation": {
            "provider_calls": 0,
            "model_invocations": 0,
            "scored_mode_executions": 0,
        },
    }
    body = copy.deepcopy(receipt)
    receipt["receipt_sha256"] = canonical_json_sha256(body)
    return receipt


def _trusted_arrow_rows():
    """Return a gold-bearing fake Arrow decode with the frozen distributions."""

    selected = {
        "low": (
            "dask__dask_2023.3.2_2023.4.0",
            "dask/dask",
            0,
        ),
        "medium": (
            "iterative__dvc_2.19.0_2.20.0",
            "iterative/dvc",
            3,
        ),
        "high": (
            "psf__requests_v2.4.0_v2.4.1",
            "psf/requests",
            15,
        ),
    }
    filler_counts = {"low": 19, "medium": 11, "high": 14}

    def rank(stratum, instance_id):
        return canonical_json_sha256(
            {
                "seed": 20260812,
                "complexity_stratum": stratum,
                "instance_id": instance_id,
            }
        )

    rows = []
    for stratum in ("low", "medium", "high"):
        selected_id, repository, pr_count = selected[stratum]
        rows.append(_gold_bearing_arrow_row(selected_id, repository, pr_count))
        target_rank = rank(stratum, selected_id)
        added = 0
        candidate = 0
        while added < filler_counts[stratum]:
            instance_id = f"fixture-{stratum}-{candidate:05d}"
            candidate += 1
            if rank(stratum, instance_id) <= target_rank:
                continue
            rows.append(
                _gold_bearing_arrow_row(
                    instance_id,
                    f"fixture/{stratum}",
                    pr_count,
                )
            )
            added += 1
    rows.append(
        _gold_bearing_arrow_row(
            "psf__requests_v2.27.0_v2.27.1",
            "psf/requests",
            2,
        )
    )
    return list(reversed(rows))


def _gold_bearing_arrow_row(instance_id, repository, pr_count):
    return {
        "instance_id": instance_id,
        "repo": repository,
        "PRs": [
            {"body": f"raw-pr-sentinel-{instance_id}-{index}"}
            for index in range(pr_count)
        ],
        "patch": f"restricted-patch-{instance_id}",
        "test_patch": f"restricted-test-{instance_id}",
        "score": 1,
        "prior_result": "restricted-outcome",
    }


def _trusted_projection(rows=None):
    rows = _trusted_arrow_rows() if rows is None else rows
    with tempfile.NamedTemporaryFile() as source, mock.patch.object(
        preparation,
        "_file_sha256",
        return_value=FIXED_DATASET_ARTIFACT_SHA256,
    ), mock.patch.object(
        preparation,
        "_read_swe_evo_arrow_rows",
        return_value=rows,
    ):
        return project_swe_evo_arrow_inventory(
            source.name,
            source_commit=FIXED_SOURCE_COMMIT,
            split=FIXED_DATASET_SPLIT,
        )


def _complexity_decisions():
    decisions = _decisions()
    decisions["complexity"] = {
        "proxy": {
            "name": "dataset_pr_record_count",
            "source": "trusted fixed Arrow projection",
            "extraction_rule": 'len(instance["PRs"]) then discard the count',
            "source_fields": ["complexity_stratum"],
        },
        "gold_blind": True,
        "stratum_boundaries": [
            {"stratum": "low", "boundary_rule": "0-2 PR records"},
            {"stratum": "medium", "boundary_rule": "3-6 PR records"},
            {"stratum": "high", "boundary_rule": "7 or more PR records"},
        ],
    }
    decisions["selection"] = {
        "metadata_revision": "swe-evo-pr-count-r1",
        "filters": {
            "repositories": [],
            "languages": [],
            "required_tags": [],
            "excluded_instance_ids": ["psf__requests_v2.27.0_v2.27.1"],
        },
        "seed": 20260812,
        "stratum_quotas": {"low": 1, "medium": 1, "high": 1},
        "sample_size": 3,
    }
    return decisions


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


def _materialization():
    selection_authority = _selection_authority()
    materialization = materialize_phase3_instance_authorities(
        selection_authority=selection_authority,
        decisions=_decisions(),
        task_inputs_by_instance=_task_inputs(selection_authority),
        execution_profile=_execution_profile(),
        shared_budget=_shared_budget(),
        research_authority_sha256="a" * 64,
        mode_order=_mode_order(),
    )
    return selection_authority, materialization


def _retry_policy():
    return {
        "provider_retry_limit": 1,
        "retryable_failures": [
            "provider_transport_error",
            "provider_rate_limit",
        ],
        "budget_accounting": (
            "all_reported_usage_and_wall_time_count_toward_instance_budget"
        ),
    }


def _abort_conditions():
    return {
        "usage_coverage_below_percent": 100,
        "cross_mode_isolation_violation": True,
        "gold_visibility_violation": True,
        "contract_digest_mismatch": True,
        "budget_ceiling_reached": True,
    }


def _aggregate():
    selection_authority, materialization = _materialization()
    contract = build_phase3_aggregate_pilot_contract(
        selection_authority=selection_authority,
        instance_materialization=materialization,
        retry_policy=_retry_policy(),
        abort_conditions=_abort_conditions(),
    )
    return selection_authority, materialization, contract


def _live_authorization(contract, *, decision="approved"):
    body = contract["contract"]
    return {
        "schema_version": LIVE_AUTHORIZATION_SCHEMA_VERSION,
        "decision": decision,
        "operator_identity": "phase3b-test-operator",
        "authorized_at": "2026-08-11T01:00:00Z",
        "gate_id": "P3-LIVE",
        "epoch_number": 2,
        "epoch_sha256": "b" * 64,
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
        "modes": ["single_codex", "agentteam_direct", "agentteam_full"],
    }


class Phase3PilotPreparationTests(unittest.TestCase):
    def _assert_trusted_arrow_projection_is_bound_gold_blind_and_byte_stable(self):
        first = _trusted_projection()
        second = _trusted_projection(copy.deepcopy(_trusted_arrow_rows()))

        self.assertEqual(first, second)
        self.assertEqual(
            complexity_projection_bytes(first),
            complexity_projection_bytes(second),
        )
        self.assertEqual(
            first["population"]["counts_by_stratum"],
            EXPECTED_POPULATION_COUNTS,
        )
        self.assertEqual(
            first["population"]["eligible_counts_by_stratum"],
            EXPECTED_ELIGIBLE_POPULATION_COUNTS,
        )
        self.assertEqual(first["source"], fixed_dataset_binding())
        self.assertEqual(
            first["routing_manifest_sha256"],
            first["routing_manifest"]["manifest_sha256"],
        )

        serialized = json.dumps(first, sort_keys=True)
        for forbidden_value in (
            "raw-pr-sentinel",
            "restricted-patch",
            "restricted-test",
            "restricted-outcome",
        ):
            self.assertNotIn(forbidden_value, serialized)
        for row in first["routing_manifest"]["manifest"]["metadata"][
            "instances"
        ]:
            self.assertEqual(
                set(row),
                {
                    "instance_id",
                    "repository",
                    "complexity_stratum",
                    "language",
                    "tags",
                },
            )
            self.assertNotIn("dataset_pr_record_count", row)
            self.assertNotIn("PRs", row)

    def _assert_projection_replays_exact_approved_selection(self):
        projection = _trusted_projection()
        first = replay_phase3_complexity_selection(projection)
        second = replay_phase3_complexity_selection(copy.deepcopy(projection))

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(first["ordered_instance_ids"]),
            EXPECTED_SELECTION_PREVIEW,
        )
        self.assertEqual(
            first["eligible_counts_by_stratum"],
            EXPECTED_ELIGIBLE_POPULATION_COUNTS,
        )

    def _assert_complexity_control_metadata_does_not_enter_worker_visible_input(self):
        projection = _trusted_projection()
        decisions = _complexity_decisions()
        authority = prepare_phase3_pilot_selection(
            projection["routing_manifest"],
            decisions,
        )
        self.assertEqual(authority["status"], "selection_frozen")
        instance_id = authority["selection"]["ordered_instance_ids"][0]
        visible = build_phase3_instance_visible_input(
            instance_id=instance_id,
            selection_authority=authority,
            decisions=decisions,
            task_input=_task_inputs(authority)[instance_id],
            execution_profile=_execution_profile(),
        )
        serialized = json.dumps(visible, sort_keys=True)
        for forbidden in (
            "complexity_stratum",
            "dataset_pr_record_count",
            "PRs",
            "raw-pr-sentinel",
            "restricted-patch",
            "restricted-outcome",
        ):
            self.assertNotIn(forbidden, serialized)

    def _assert_trusted_arrow_source_drift_and_missing_pyarrow_fail_closed(self):
        with tempfile.NamedTemporaryFile() as source:
            source.write(b"not-the-fixed-arrow-artifact")
            source.flush()
            with self.assertRaisesRegex(
                Phase3PreparationError,
                "artifact SHA-256",
            ):
                project_swe_evo_arrow_inventory(
                    source.name,
                    source_commit=FIXED_SOURCE_COMMIT,
                    split=FIXED_DATASET_SPLIT,
                )

            with mock.patch.object(
                preparation,
                "_file_sha256",
                return_value=FIXED_DATASET_ARTIFACT_SHA256,
            ), mock.patch.dict(
                sys.modules,
                {"pyarrow": None, "pyarrow.ipc": None},
            ), self.assertRaisesRegex(
                Phase3PreparationError,
                "pyarrow is required",
            ):
                project_swe_evo_arrow_inventory(
                    source.name,
                    source_commit=FIXED_SOURCE_COMMIT,
                    split=FIXED_DATASET_SPLIT,
                )

        with self.assertRaisesRegex(Phase3PreparationError, "source_commit"):
            project_swe_evo_arrow_inventory(
                "unused.arrow",
                source_commit="0" * 40,
                split=FIXED_DATASET_SPLIT,
            )
        with self.assertRaisesRegex(Phase3PreparationError, "split"):
            project_swe_evo_arrow_inventory(
                "unused.arrow",
                source_commit=FIXED_SOURCE_COMMIT,
                split="train",
            )

    def _assert_arrow_reader_projects_before_python_materialization(self):
        class FakeProjectedTable:
            def to_pylist(self):
                return [{"instance_id": "fixture", "repo": "fixture/repo", "PRs": []}]

        class FakeTable:
            column_names = [
                "instance_id",
                "repo",
                "PRs",
                "patch",
                "test_patch",
                "score",
                "prior_result",
            ]

            def __init__(self):
                self.selected_columns = None

            def select(self, columns):
                self.selected_columns = list(columns)
                return FakeProjectedTable()

        class FakeReader:
            def __init__(self, table):
                self.table = table

            def read_all(self):
                return self.table

        table = FakeTable()
        fake_ipc = types.ModuleType("pyarrow.ipc")
        fake_ipc.open_stream = lambda _source: FakeReader(table)
        fake_pyarrow = types.ModuleType("pyarrow")
        fake_pyarrow.ArrowInvalid = RuntimeError
        fake_pyarrow.ipc = fake_ipc
        with tempfile.NamedTemporaryFile() as source, mock.patch.dict(
            sys.modules,
            {"pyarrow": fake_pyarrow, "pyarrow.ipc": fake_ipc},
        ):
            rows = preparation._read_swe_evo_arrow_rows(
                preparation.Path(source.name)
            )

        self.assertEqual(table.selected_columns, ["instance_id", "repo", "PRs"])
        self.assertEqual(
            rows,
            [{"instance_id": "fixture", "repo": "fixture/repo", "PRs": []}],
        )

    def _assert_trusted_arrow_projection_rejects_row_and_population_drift(self):
        missing_prs = _trusted_arrow_rows()
        missing_prs[0].pop("PRs")
        with self.assertRaisesRegex(Phase3PreparationError, "instance_id and PRs"):
            _trusted_projection(missing_prs)

        invalid_prs = _trusted_arrow_rows()
        invalid_prs[0]["PRs"] = None
        with self.assertRaisesRegex(Phase3PreparationError, "must be an Arrow list"):
            _trusted_projection(invalid_prs)

        population_drift = _trusted_arrow_rows()
        next(row for row in population_drift if len(row["PRs"]) >= 7)["PRs"] = []
        with self.assertRaisesRegex(Phase3PreparationError, "population mismatch"):
            _trusted_projection(population_drift)

    def _assert_projection_mutation_and_leakage_are_rejected(self):
        projection = _trusted_projection()
        projection["routing_manifest"]["manifest"]["metadata"]["instances"][0][
            "dataset_pr_record_count"
        ] = 7
        projection["routing_manifest"]["manifest_sha256"] = canonical_json_sha256(
            projection["routing_manifest"]["manifest"]
        )
        projection["routing_manifest_sha256"] = projection["routing_manifest"][
            "manifest_sha256"
        ]
        body = copy.deepcopy(projection)
        body.pop("projection_sha256")
        projection["projection_sha256"] = canonical_json_sha256(body)
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "schema validation",
        ):
            validate_phase3_complexity_projection(projection)

    def test_fixed_binding_and_byte_stable_manifest(self):
        self._assert_trusted_arrow_projection_is_bound_gold_blind_and_byte_stable()
        self._assert_projection_replays_exact_approved_selection()
        self._assert_complexity_control_metadata_does_not_enter_worker_visible_input()
        self._assert_trusted_arrow_source_drift_and_missing_pyarrow_fail_closed()
        self._assert_arrow_reader_projects_before_python_materialization()
        self._assert_trusted_arrow_projection_rejects_row_and_population_drift()
        self._assert_projection_mutation_and_leakage_are_rejected()

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

    def test_aggregate_contract_sums_all_maximum_scheduled_repetitions(self):
        selection_authority, materialization, contract = _aggregate()

        validate_phase3_instance_materialization(
            materialization,
            selection_authority=selection_authority,
        )
        self.assertFalse(contract["contract"]["provider_calls_authorized"])
        self.assertEqual(
            contract["contract"]["live_authorization"]["status"],
            "not_authorized",
        )
        self.assertEqual(
            contract["contract"]["aggregate_budget_ceiling"],
            {
                "maximum_total_tokens": 27_000,
                "maximum_wall_time_seconds": 3_240,
                "max_inflight_model_invocations": 1,
            },
        )

    def test_preregistration_change_changes_aggregate_contract_digest(self):
        selection_authority, baseline_materialization, baseline = _aggregate()
        task_inputs = _task_inputs(selection_authority)
        first_id = selection_authority["selection"]["ordered_instance_ids"][0]
        task_inputs[first_id]["task"]["goal"] += " Preserve reviewed behavior."
        changed_materialization = materialize_phase3_instance_authorities(
            selection_authority=selection_authority,
            decisions=_decisions(),
            task_inputs_by_instance=task_inputs,
            execution_profile=_execution_profile(),
            shared_budget=_shared_budget(),
            research_authority_sha256="a" * 64,
            mode_order=_mode_order(),
        )
        changed = build_phase3_aggregate_pilot_contract(
            selection_authority=selection_authority,
            instance_materialization=changed_materialization,
            retry_policy=_retry_policy(),
            abort_conditions=_abort_conditions(),
        )

        self.assertNotEqual(
            baseline_materialization["materialization_sha256"],
            changed_materialization["materialization_sha256"],
        )
        self.assertNotEqual(
            baseline["contract_sha256"],
            changed["contract_sha256"],
        )

    def test_selected_instance_change_changes_aggregate_contract_digest(self):
        baseline_authority, _, baseline = _aggregate()
        changed_decisions = _decisions()
        changed_decisions["selection"]["seed"] += 1
        changed_authority = prepare_phase3_pilot_selection(
            convert_swe_evo_inventory(_inventory()),
            changed_decisions,
        )
        self.assertNotEqual(
            baseline_authority["selection"]["ordered_instance_ids"],
            changed_authority["selection"]["ordered_instance_ids"],
        )
        changed_materialization = materialize_phase3_instance_authorities(
            selection_authority=changed_authority,
            decisions=changed_decisions,
            task_inputs_by_instance=_task_inputs(changed_authority),
            execution_profile=_execution_profile(),
            shared_budget=_shared_budget(),
            research_authority_sha256="a" * 64,
            mode_order=_mode_order(),
        )
        changed = build_phase3_aggregate_pilot_contract(
            selection_authority=changed_authority,
            instance_materialization=changed_materialization,
            retry_policy=_retry_policy(),
            abort_conditions=_abort_conditions(),
        )
        self.assertNotEqual(
            baseline["contract_sha256"],
            changed["contract_sha256"],
        )

    def test_provider_free_preflight_is_byte_stable_and_reports_zero_calls(self):
        selection_authority, materialization, contract = _aggregate()
        first = build_phase3_provider_free_preflight_receipt(
            pilot_contract=contract,
            selection_authority=selection_authority,
            instance_materialization=materialization,
        )
        second = build_phase3_provider_free_preflight_receipt(
            pilot_contract=copy.deepcopy(contract),
            selection_authority=copy.deepcopy(selection_authority),
            instance_materialization=copy.deepcopy(materialization),
        )

        self.assertEqual(first, second)
        self.assertEqual(
            provider_free_preflight_receipt_bytes(first),
            provider_free_preflight_receipt_bytes(second),
        )
        self.assertEqual(first["usage_reconciliation"]["live_provider_calls"], 0)
        self.assertEqual(
            first["usage_reconciliation"]["scored_mode_executions"],
            0,
        )
        self.assertFalse(
            first["live_authorization_reconciliation"]["permit_issued"]
        )
        self.assertEqual(
            first["live_authorization_reconciliation"]["admission_status"],
            "denied",
        )

    def test_provider_free_preflight_v2_binds_dynamic_resource_preflight(self):
        selection_authority, materialization, contract = _aggregate()
        resource = _resource_preflight_receipt()
        receipt = build_phase3_provider_free_preflight_receipt(
            pilot_contract=contract,
            selection_authority=selection_authority,
            instance_materialization=materialization,
            resource_preflight_receipt=resource,
        )
        self.assertEqual(receipt["schema_version"], "phase3_pilot_preflight.v2")
        self.assertEqual(
            receipt["bindings"]["resource_envelope_sha256"],
            resource["binding_sha256"],
        )
        self.assertEqual(
            receipt["bindings"]["resource_preflight_receipt_sha256"],
            resource["receipt_sha256"],
        )
        self.assertIn(
            "resource_hierarchy",
            receipt["verification"]["results"],
        )
        self.assertEqual(
            validate_phase3_provider_free_preflight_receipt(
                receipt,
                pilot_contract=contract,
                selection_authority=selection_authority,
                instance_materialization=materialization,
                resource_preflight_receipt=resource,
            ),
            receipt,
        )

    def test_provider_free_preflight_v2_rejects_resource_receipt_drift(self):
        selection_authority, materialization, contract = _aggregate()
        resource = _resource_preflight_receipt()
        receipt = build_phase3_provider_free_preflight_receipt(
            pilot_contract=contract,
            selection_authority=selection_authority,
            instance_materialization=materialization,
            resource_preflight_receipt=resource,
        )
        changed = copy.deepcopy(resource)
        changed["pilot_id"] = "PILOT-PREPARATION-DRIFT"
        changed_body = copy.deepcopy(changed)
        changed_body.pop("receipt_sha256")
        changed["receipt_sha256"] = canonical_json_sha256(changed_body)
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "does not bind resource preflight",
        ):
            validate_phase3_provider_free_preflight_receipt(
                receipt,
                resource_preflight_receipt=changed,
            )

    def test_preflight_mutation_and_source_drift_fail_closed(self):
        selection_authority, materialization, contract = _aggregate()
        receipt = build_phase3_provider_free_preflight_receipt(
            pilot_contract=contract,
            selection_authority=selection_authority,
            instance_materialization=materialization,
        )
        receipt["usage_reconciliation"]["live_provider_calls"] = 1
        receipt_body = copy.deepcopy(receipt)
        receipt_body.pop("receipt_sha256")
        receipt["receipt_sha256"] = canonical_json_sha256(receipt_body)
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "schema validation",
        ):
            validate_phase3_provider_free_preflight_receipt(receipt)

        changed = copy.deepcopy(materialization)
        changed["materialization_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            Phase3PreparationError,
            "materialization_sha256",
        ):
            build_phase3_provider_free_preflight_receipt(
                pilot_contract=contract,
                selection_authority=selection_authority,
                instance_materialization=changed,
            )

    def test_missing_stale_rejected_mismatched_and_expanded_live_authority_denied(self):
        selection_authority, materialization, contract = _aggregate()
        selection = selection_authority["selection"]
        preregistrations = materialization["preregistrations_by_instance"]
        cases = []
        cases.append((None, 2, "b" * 64))
        rejected = _live_authorization(contract, decision="rejected")
        cases.append((rejected, 2, "b" * 64))
        stale = _live_authorization(contract)
        cases.append((stale, 3, "b" * 64))
        mismatched = _live_authorization(contract)
        mismatched["selection_sha256"] = "c" * 64
        cases.append((mismatched, 2, "b" * 64))
        expanded = _live_authorization(contract)
        expanded["max_total_tokens"] += 1
        cases.append((expanded, 2, "b" * 64))

        for authorization, epoch, epoch_sha256 in cases:
            with self.subTest(authorization=authorization), self.assertRaises(
                Phase3PilotError
            ):
                admit_phase3_live_launch(
                    contract,
                    authorization,
                    selection=selection,
                    preregistrations_by_instance=preregistrations,
                    expected_epoch_number=epoch,
                    expected_epoch_sha256=epoch_sha256,
                )


if __name__ == "__main__":
    unittest.main()

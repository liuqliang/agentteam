import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agentteam_runtime.experiment_contract import (
    LEGACY_MANIFEST_SCHEMA_VERSION,
    ExperimentContractError,
    ExperimentLeaseError,
    acquire_controller_lease,
    allocate_experiment_run,
    build_experiment_run_manifest,
    canonical_json_bytes,
    canonical_json_sha256,
    derive_experiment_run_id,
    ensure_executable_manifest,
    validate_experiment_protocol,
    validate_experiment_run_binding,
    validate_experiment_run_manifest,
    validate_experiment_state,
    validate_resume_binding,
)


def _protocol():
    return {
        "schema_version": "experiment_protocol.v1",
        "experiment_id": "phase2-calibration",
        "instance_id": "fixture-001",
        "repository": {
            "source": "/srv/source/repository.git",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "git_object_format": "sha1",
        },
        "goal": {
            "summary": "Implement the deterministic fixture.",
            "constraints": [
                "Do not read evaluator-only state.",
                "Preserve the declared acceptance command.",
            ],
        },
        "acceptance": {
            "command": ["python3", "-m", "unittest", "tests.test_fixture"],
            "timeout_seconds": 120,
        },
        "modes": [
            "single_codex",
            "agentteam_direct",
            "agentteam_full",
        ],
        "mode_order": [
            "agentteam_direct",
            "agentteam_full",
            "single_codex",
        ],
        "mode_instructions": {
            "single_codex": [],
            "agentteam_direct": ["Use only the preregistered taskpack."],
            "agentteam_full": ["Author and freeze the taskpack normally."],
        },
        "repetition_policy": {
            "count": 2,
            "order_strategy": "seeded_counterbalanced_rotation",
        },
        "environment": {
            "backend": "codex",
            "codex_cli_version": "codex-test-v1",
            "model": "codex-test-model",
            "reasoning_profile": "high",
            "service_configuration_sha256": "3" * 64,
            "sandbox_policy": "workspace-write",
            "permission_policy": "never",
            "network_policy": "disabled",
            "tool_allowlist": ["exec_command", "apply_patch"],
            "host_class": "deterministic-test",
            "cpu_limit": 2,
            "memory_limit_bytes": 1073741824,
            "dependency_cache_policy": "read_only_preregistered",
            "max_inflight_model_invocations": 1,
        },
        "seed": 17,
        "scored": False,
        "blind_gold": {
            "policy": "unavailable_to_runtime",
        },
        "budgets": {
            "max_total_tokens": 100000,
            "max_wall_time_seconds": 1800,
            "soft_warning_ratio": 0.8,
            "stop_boundaries": [
                "pre_provider_launch",
                "post_invocation_terminal",
                "pre_integration",
                "post_integration",
            ],
        },
        "operator_limits": {
            "expected_operator_action": 2,
            "corrective_intervention": 1,
            "decision_escalation": 1,
        },
        "evaluator": {
            "version": "fixture-evaluator.v1",
            "artifact_sha256": "4" * 64,
        },
        "direct_taskpack": {
            "sha256": "5" * 64,
            "cost_reported_separately": True,
            "available_to_full_mode": False,
        },
        "usage_contract_version": "model_invocation_usage.v1",
    }


def _release():
    return {
        "release_id": "candidate-v1",
        "release_root": "/srv/agentteam/releases/candidate-v1",
        "runtime_root": "/srv/agentteam/releases/candidate-v1/m0_runtime",
        "release_manifest_sha256": "6" * 64,
        "source_commit": "7" * 40,
        "git_object_format": "sha1",
    }


def _allocate(root, key="request-001"):
    return allocate_experiment_run(
        root,
        _protocol(),
        mode="single_codex",
        repetition_index=0,
        stable_request_key=key,
        runtime_release=_release(),
        bound_at="2026-07-27T00:00:00Z",
    )


class ExperimentContractSchemaTests(unittest.TestCase):
    def test_protocol_run_binding_and_state_schemas_are_executable(self):
        protocol = _protocol()
        manifest = build_experiment_run_manifest(
            protocol,
            mode="agentteam_direct",
            repetition_index=1,
            stable_request_key="stable-123",
        )

        self.assertIs(validate_experiment_protocol(protocol), protocol)
        self.assertIs(
            validate_experiment_run_manifest(manifest, protocol),
            manifest,
        )
        self.assertEqual(
            manifest["experiment_run_id"],
            derive_experiment_run_id(
                canonical_json_sha256(protocol),
                "agentteam_direct",
                1,
                "stable-123",
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            allocation = allocate_experiment_run(
                tmp,
                protocol,
                mode="agentteam_direct",
                repetition_index=1,
                stable_request_key="stable-123",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            self.assertEqual(
                validate_experiment_run_binding(allocation["binding"]),
                allocation["binding"],
            )
            self.assertEqual(
                validate_experiment_state(allocation["state"]),
                allocation["state"],
            )

    def test_protocol_requires_complete_three_mode_equal_input_contract(self):
        cases = {
            "missing-mode": lambda value: value["modes"].pop(),
            "duplicate-order": lambda value: value["mode_order"].__setitem__(
                0,
                value["mode_order"][1],
            ),
            "concurrent-provider-lanes": lambda value: value[
                "environment"
            ].__setitem__("max_inflight_model_invocations", 2),
            "missing-environment": lambda value: value["environment"].pop(
                "network_policy"
            ),
            "shell-acceptance": lambda value: value["acceptance"].__setitem__(
                "command",
                "python3 -m unittest",
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                protocol = _protocol()
                mutate(protocol)
                with self.assertRaises(ExperimentContractError):
                    validate_experiment_protocol(protocol)

    def test_run_manifest_binds_mode_repetition_request_and_protocol(self):
        protocol = _protocol()
        manifest = build_experiment_run_manifest(
            protocol,
            mode="single_codex",
            repetition_index=0,
            stable_request_key="stable-001",
        )

        for field, value in {
            "protocol_sha256": "0" * 64,
            "mode": "agentteam_full",
            "repetition_index": 1,
            "stable_request_key": "stable-002",
        }.items():
            with self.subTest(field=field):
                drifted = dict(manifest)
                drifted[field] = value
                with self.assertRaises(ExperimentContractError):
                    validate_experiment_run_manifest(drifted, protocol)

    def test_legacy_v1_manifest_remains_validation_only(self):
        legacy = {
            "schema_version": LEGACY_MANIFEST_SCHEMA_VERSION,
            "experiment_id": "legacy",
        }

        with self.assertRaisesRegex(ExperimentContractError, "validation-only"):
            ensure_executable_manifest(legacy)


class ExperimentAllocationTests(unittest.TestCase):
    def test_allocation_publishes_canonical_authority_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            root = Path(tmp)

            self.assertEqual(allocation["allocation_status"], "created")
            self.assertTrue(allocation["created"])
            self.assertEqual(allocation["provider_calls"], 0)
            self.assertEqual(allocation["target_mutations"], 0)
            protocol_path = Path(allocation["protocol_path"])
            manifest_path = Path(allocation["run_manifest_path"])
            binding_path = Path(allocation["run_dir"]) / "binding.json"
            state_path = Path(allocation["run_dir"]) / "state.json"
            request_path = Path(allocation["request_path"])
            for path in (
                protocol_path,
                manifest_path,
                binding_path,
                state_path,
                request_path,
            ):
                self.assertTrue(path.is_file(), path)

            self.assertEqual(
                protocol_path.read_bytes(),
                canonical_json_bytes(_protocol()) + b"\n",
            )
            self.assertEqual(
                json.loads(request_path.read_text(encoding="utf-8")),
                allocation["binding"],
            )
            self.assertFalse((Path(allocation["run_dir"]) / "repository").exists())
            self.assertEqual(allocation["state"]["status"], "prepared")
            self.assertEqual(
                allocation["binding"]["protocol_sha256"],
                canonical_json_sha256(_protocol()),
            )
            self.assertEqual(
                allocation["binding"]["run_manifest_sha256"],
                canonical_json_sha256(allocation["run_manifest"]),
            )
            self.assertEqual(
                sorted(path.name for path in (root / "protocols").iterdir()),
                [f"{allocation['protocol_sha256']}.json"],
            )

    def test_same_stable_request_returns_existing_without_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _allocate(tmp)
            provider = Mock(name="provider")

            repeated = _allocate(tmp)
            if repeated["created"]:
                provider()

            self.assertEqual(repeated["allocation_status"], "existing")
            self.assertFalse(repeated["created"])
            self.assertEqual(repeated["experiment_run_id"], first["experiment_run_id"])
            self.assertEqual(repeated["binding"], first["binding"])
            self.assertEqual(repeated["provider_calls"], 0)
            provider.assert_not_called()
            self.assertEqual(len(list((Path(tmp) / "runs").iterdir())), 1)
            self.assertEqual(len(list((Path(tmp) / "requests").iterdir())), 1)

    def test_resume_reads_published_protocol_not_mutable_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol_path = root / "mutable-input.json"
            protocol_path.write_text(
                json.dumps(_protocol(), indent=2) + "\n",
                encoding="utf-8",
            )
            allocation = allocate_experiment_run(
                root / "experiment-state",
                protocol_path,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="request-from-path",
                runtime_release=_release(),
                bound_at="2026-07-27T00:00:00Z",
            )
            protocol_path.write_text('{"tampered":true}\n', encoding="utf-8")

            accepted = validate_resume_binding(
                allocation["run_dir"],
                runtime_release=_release(),
                repository=_protocol()["repository"],
            )

            self.assertEqual(accepted["resume_status"], "accepted")
            self.assertEqual(accepted["protocol"], _protocol())

    def test_request_drift_fails_before_provider_or_target_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target-repository"
            target.mkdir()
            marker = target / "marker.txt"
            marker.write_text("unchanged\n", encoding="utf-8")
            _allocate(root)
            protocol_count = len(list((root / "protocols").iterdir()))
            provider = Mock(name="provider")
            drifted = _protocol()
            drifted["seed"] += 1

            with self.assertRaises(ExperimentContractError):
                result = allocate_experiment_run(
                    root,
                    drifted,
                    mode="single_codex",
                    repetition_index=0,
                    stable_request_key="request-001",
                    runtime_release=_release(),
                )
                if result["created"]:
                    provider()

            provider.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(len(list((root / "protocols").iterdir())), protocol_count)

    def test_distinct_request_keys_allocate_collision_safe_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _allocate(tmp, "request-a")
            second = _allocate(tmp, "request-b")

            self.assertNotEqual(first["experiment_run_id"], second["experiment_run_id"])
            self.assertEqual(len(first["experiment_run_id"].split("-")[-1]), 64)
            self.assertEqual(len(list((Path(tmp) / "runs").iterdir())), 2)

    def test_tampered_published_protocol_fails_closed_without_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            protocol_path = Path(allocation["protocol_path"])
            protocol_path.write_text(
                json.dumps(_protocol(), indent=2) + "\n",
                encoding="utf-8",
            )
            provider = Mock(name="provider")

            with self.assertRaisesRegex(ExperimentContractError, "canonically"):
                result = _allocate(tmp)
                if result["created"]:
                    provider()

            provider.assert_not_called()


class ExperimentLeaseAndResumeTests(unittest.TestCase):
    def test_competing_controller_lease_fails_closed_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            first = acquire_controller_lease(
                allocation["run_dir"],
                controller_id="controller-a",
                lease_id="lease-a",
                acquired_at="2026-07-27T00:00:01Z",
            )
            self.addCleanup(first.release)

            with self.assertRaises(ExperimentLeaseError):
                acquire_controller_lease(
                    allocation["run_dir"],
                    controller_id="controller-b",
                    lease_id="lease-b",
                )

            first.release()
            with acquire_controller_lease(
                allocation["run_dir"],
                controller_id="controller-b",
                lease_id="lease-b",
            ) as second:
                self.assertTrue(second.held)
                self.assertEqual(second.record["controller_id"], "controller-b")
            self.assertFalse(second.held)

    def test_resume_accepts_exact_binding_and_rejects_all_identity_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            protocol = _protocol()
            repository = protocol["repository"]
            release = _release()

            accepted = validate_resume_binding(
                allocation["run_dir"],
                protocol=protocol,
                run_manifest=allocation["run_manifest"],
                runtime_release=release,
                repository=repository,
                stable_request_key="request-001",
                experiment_run_id=allocation["experiment_run_id"],
                protocol_sha256=allocation["protocol_sha256"],
                run_manifest_sha256=allocation["run_manifest_sha256"],
            )
            self.assertEqual(accepted["resume_status"], "accepted")
            self.assertEqual(accepted["provider_calls"], 0)

            drifted_protocol = copy.deepcopy(protocol)
            drifted_protocol["seed"] += 1
            drifted_manifest = build_experiment_run_manifest(
                protocol,
                mode="single_codex",
                repetition_index=0,
                stable_request_key="another-request",
            )
            drifted_release = _release()
            drifted_release["release_id"] = "candidate-v2"
            drifted_repository = dict(repository)
            drifted_repository["source"] = "/srv/other/repository.git"
            drifted_object_format = {
                **repository,
                "commit": "8" * 64,
                "tree": "9" * 64,
                "git_object_format": "sha256",
            }
            cases = {
                "protocol": {"protocol": drifted_protocol},
                "run-manifest": {"run_manifest": drifted_manifest},
                "release": {"runtime_release": drifted_release},
                "repository": {"repository": drifted_repository},
                "object-format": {"repository": drifted_object_format},
                "request": {"stable_request_key": "another-request"},
            }
            for name, overrides in cases.items():
                with self.subTest(name=name):
                    arguments = {
                        "protocol": protocol,
                        "run_manifest": allocation["run_manifest"],
                        "runtime_release": release,
                        "repository": repository,
                        "stable_request_key": "request-001",
                    }
                    arguments.update(overrides)
                    provider = Mock(name=f"provider-{name}")
                    with self.assertRaises(ExperimentContractError):
                        result = validate_resume_binding(
                            allocation["run_dir"],
                            **arguments,
                        )
                        if result["resume_status"] == "accepted":
                            provider()
                    provider.assert_not_called()

    def test_resume_rejects_tampered_request_binding_and_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            request_path = Path(allocation["request_path"])
            request_path.write_text(
                json.dumps(allocation["binding"], indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ExperimentContractError, "canonically"):
                validate_resume_binding(allocation["run_dir"])

        with tempfile.TemporaryDirectory() as tmp:
            allocation = _allocate(tmp)
            state_path = Path(allocation["run_dir"]) / "state.json"
            state = dict(allocation["state"])
            state.update(
                {
                    "status": "budget_stopped",
                    "state_version": 2,
                    "updated_at": "2026-07-27T00:00:02Z",
                }
            )
            state_path.write_bytes(canonical_json_bytes(state) + b"\n")

            with self.assertRaisesRegex(ExperimentContractError, "cannot resume"):
                validate_resume_binding(allocation["run_dir"])


if __name__ == "__main__":
    unittest.main()

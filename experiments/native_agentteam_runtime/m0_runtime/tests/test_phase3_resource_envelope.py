import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from agentteam_runtime.experiment_protocol import (
    protocol_resource_envelope_reference,
    validate_protocol_resource_envelope,
)
from agentteam_runtime.experiment_sandbox import _run_bounded_argv
from agentteam_runtime.resource_envelope import (
    GIB,
    ResourceEnvelopeError,
    ResourceEnvelopeUnavailable,
    Phase3PilotResourceOwner,
    ResourceUnitMonitor,
    SystemdResourceHierarchy,
    approved_phase3_resource_envelope_binding,
    build_resource_evidence,
    canonical_resource_envelope_sha256,
    classify_resource_exhaustion,
    read_resource_counters,
    run_phase3_provider_free_resource_preflight,
    validate_phase3_resource_preflight_receipt,
    validate_resource_evidence,
    validate_resource_envelope_binding,
    verify_host_capacity,
)


class _SystemdRunner:
    def __init__(self):
        self.hierarchy = None
        self.commands = []
        self.mismatch_unit = None

    def __call__(self, command, **_kwargs):
        command = list(command)
        self.commands.append(command)
        if "show" not in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        unit = command[command.index("show") + 1]
        hierarchy = self.hierarchy
        binding = hierarchy.binding
        if unit == hierarchy.project_slice:
            envelope = binding["envelopes"]["pilot_project"]
            control_group = "/project"
        elif unit == hierarchy.mode_slice:
            envelope_name = binding["hierarchy"]["modes"][hierarchy.mode][
                "mode_envelope"
            ]
            envelope = binding["envelopes"][envelope_name]
            control_group = "/project/mode"
        elif unit == hierarchy.control_scope:
            envelope = binding["envelopes"][
                "agentteam_control_plane_allowance"
            ]
            control_group = "/project/mode/control.service"
        else:
            envelope = binding["envelopes"]["common_workload_slot"]
            control_group = "/project/mode/workload.service"
        memory_max = envelope["memory_max_bytes"]
        if unit == self.mismatch_unit:
            memory_max += 1
        stdout = "\n".join(
            [
                f"CPUQuotaPerSecUSec={envelope['cpu_quota']}s",
                f"MemoryHigh={envelope['memory_high_bytes']}",
                f"MemoryMax={memory_max}",
                f"TasksMax={envelope['tasks_max']}",
                f"MemorySwapMax={envelope['memory_swap_max_bytes']}",
                f"ControlGroup={control_group}",
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")


class _EvaluatorHierarchy:
    instance = None

    def __init__(self, binding, *, run_id, mode, owner_reference=None):
        self.binding = binding
        self.run_id = run_id
        self.mode = mode
        self.owner_reference = owner_reference
        self.prepared = False
        self.cleaned = False
        self._cgroup_tmp = tempfile.TemporaryDirectory()
        self.cgroup_root = Path(self._cgroup_tmp.name)
        cgroup = self.cgroup_root / "phase3" / "evaluator"
        cgroup.mkdir(parents=True)
        (cgroup / "cpu.stat").write_text("usage_usec 10\n", encoding="ascii")
        (cgroup / "memory.events").write_text("oom 0\n", encoding="ascii")
        (cgroup / "pids.events").write_text("max 0\n", encoding="ascii")
        (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
        self.__class__.instance = self

    def prepare(self):
        self.prepared = True

    def leaf_arguments(self, *, evaluator=False):
        assert evaluator
        return [
            "--slice=phase3-mode.slice",
            "--property=CPUQuota=400%",
            "--property=MemoryHigh=8589934592",
            "--property=MemoryMax=12884901888",
            "--property=TasksMax=384",
            "--property=MemorySwapMax=0",
        ]

    def verify_leaf(self, unit, *, evaluator=False):
        assert evaluator
        return {"ControlGroup": "/phase3/evaluator", "Unit": unit}

    def identity(self):
        return {"project_slice": "phase3.slice", "mode_slice": "phase3-mode.slice"}

    def cleanup(self):
        self.cleaned = True
        self._cgroup_tmp.cleanup()
        return {"cleanup_attempted": True, "cleanup_complete": True}


class _PreflightOwner:
    instance = None

    def __init__(self, binding, *, pilot_id, command_runner=None):
        self.binding = binding
        self.pilot_id = pilot_id
        self.command_runner = command_runner
        self.cleaned = False
        type(self).instance = self

    def prepare(self):
        return {
            mode: {"run_id": self.pilot_id, "mode": mode}
            for mode in ("single_codex", "agentteam_direct", "agentteam_full")
        }

    def evidence(self):
        return {
            "project": build_resource_evidence(
                binding=self.binding,
                scope="project",
                identity={"control_group": "/project"},
                counters={},
            ),
            "modes": {
                mode: build_resource_evidence(
                    binding=self.binding,
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
        }

    def cleanup(self):
        self.cleaned = True
        return {
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
        }


class _PreflightHierarchy:
    def __init__(
        self,
        binding,
        *,
        run_id,
        mode,
        owner_reference,
        command_runner=None,
    ):
        self.binding = binding
        self.run_id = run_id
        self.mode = mode
        self.owner_reference = owner_reference
        self.command_runner = command_runner
        self.control_scope = f"{mode}-control.service"

    def prepare(self, *, check_host=False):
        assert check_host is False

    def control_plane_command(self, command):
        return ["probe", self.mode, "control_plane", *command]

    def leaf_command(self, command, *, unit, evaluator=False):
        scope = "evaluator" if evaluator else "workload"
        return ["probe", self.mode, scope, unit, *command]

    def stop_transient_unit(self, _unit):
        return True


class _PreflightMonitor:
    def __init__(self, hierarchy, unit, *, scope, evaluator=False):
        self.hierarchy = hierarchy
        self.unit = unit
        self.scope = scope
        self.evaluator = evaluator
        self.cancelled = False

    def start(self):
        return self

    def finish(self, *, binding):
        return build_resource_evidence(
            binding=binding,
            scope=self.scope,
            identity={"control_group": f"/{self.hierarchy.mode}/{self.scope}"},
            counters={},
        )

    def cancel(self):
        self.cancelled = True


class Phase3ResourceEnvelopeTests(unittest.TestCase):
    def test_provider_free_preflight_runs_exact_eight_probe_inventory(self):
        commands = []

        def runner(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        receipt = run_phase3_provider_free_resource_preflight(
            approved_phase3_resource_envelope_binding(),
            pilot_id="PILOT-PROVIDER-FREE",
            command_runner=runner,
            owner_factory=_PreflightOwner,
            hierarchy_factory=_PreflightHierarchy,
            monitor_factory=_PreflightMonitor,
        )
        self.assertEqual(len(commands), 8)
        self.assertEqual(len(receipt["probe_records"]), 8)
        self.assertEqual(
            receipt["provider_reconciliation"],
            {
                "provider_calls": 0,
                "model_invocations": 0,
                "scored_mode_executions": 0,
            },
        )
        self.assertTrue(_PreflightOwner.instance.cleaned)

    def test_provider_free_preflight_failure_still_cleans_owner(self):
        calls = 0

        def runner(command, **_kwargs):
            nonlocal calls
            calls += 1
            return subprocess.CompletedProcess(
                command,
                9 if calls == 3 else 0,
                "",
                "",
            )

        with self.assertRaisesRegex(
            ResourceEnvelopeUnavailable,
            "probe failed",
        ):
            run_phase3_provider_free_resource_preflight(
                approved_phase3_resource_envelope_binding(),
                pilot_id="PILOT-PROVIDER-FAILURE",
                command_runner=runner,
                owner_factory=_PreflightOwner,
                hierarchy_factory=_PreflightHierarchy,
                monitor_factory=_PreflightMonitor,
            )
        self.assertTrue(_PreflightOwner.instance.cleaned)

    def test_provider_free_preflight_prepare_failure_still_cleans_owner(self):
        class FailingOwner(_PreflightOwner):
            def prepare(self):
                raise ResourceEnvelopeUnavailable("prepare failed")

        with self.assertRaisesRegex(
            ResourceEnvelopeUnavailable,
            "prepare failed",
        ):
            run_phase3_provider_free_resource_preflight(
                approved_phase3_resource_envelope_binding(),
                pilot_id="PILOT-PREPARE-FAILURE",
                command_runner=lambda *_args, **_kwargs: None,
                owner_factory=FailingOwner,
                hierarchy_factory=_PreflightHierarchy,
                monitor_factory=_PreflightMonitor,
            )
        self.assertTrue(FailingOwner.instance.cleaned)

    def test_provider_free_preflight_receipt_mutation_fails_closed(self):
        receipt = run_phase3_provider_free_resource_preflight(
            approved_phase3_resource_envelope_binding(),
            pilot_id="PILOT-PROVIDER-MUTATION",
            command_runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
            owner_factory=_PreflightOwner,
            hierarchy_factory=_PreflightHierarchy,
            monitor_factory=_PreflightMonitor,
        )
        receipt["probe_records"].pop()
        with self.assertRaisesRegex(
            ResourceEnvelopeError,
            "invalid",
        ):
            validate_phase3_resource_preflight_receipt(receipt)

    def test_provider_free_preflight_rejects_rehashed_nested_evidence_drift(self):
        receipt = run_phase3_provider_free_resource_preflight(
            approved_phase3_resource_envelope_binding(),
            pilot_id="PILOT-PROVIDER-EVIDENCE-DRIFT",
            command_runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
            owner_factory=_PreflightOwner,
            hierarchy_factory=_PreflightHierarchy,
            monitor_factory=_PreflightMonitor,
        )
        receipt["probe_records"][0]["evidence"]["outcome"][
            "resource_exhausted"
        ] = True
        body = deepcopy(receipt)
        body.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        with self.assertRaisesRegex(ResourceEnvelopeError, "outcome"):
            validate_phase3_resource_preflight_receipt(receipt)

    def test_provider_free_preflight_rejects_malformed_cleanup_as_contract_error(self):
        receipt = run_phase3_provider_free_resource_preflight(
            approved_phase3_resource_envelope_binding(),
            pilot_id="PILOT-PROVIDER-CLEANUP-DRIFT",
            command_runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, "", ""
            ),
            owner_factory=_PreflightOwner,
            hierarchy_factory=_PreflightHierarchy,
            monitor_factory=_PreflightMonitor,
        )
        receipt["cleanup"] = []
        with self.assertRaisesRegex(ResourceEnvelopeError, "receipt is invalid"):
            validate_phase3_resource_preflight_receipt(receipt)

    def test_resource_evidence_validator_rejects_scope_drift(self):
        binding = approved_phase3_resource_envelope_binding()
        evidence = build_resource_evidence(
            binding=binding,
            scope="workload",
            identity={"control_group": "/workload"},
            counters={},
        )
        with self.assertRaisesRegex(ResourceEnvelopeError, "scope"):
            validate_resource_evidence(evidence, expected_scope="evaluator")

    def test_resource_evidence_validator_rejects_non_string_scope(self):
        evidence = build_resource_evidence(
            binding=approved_phase3_resource_envelope_binding(),
            scope="workload",
            identity={"control_group": "/workload"},
            counters={},
        )
        evidence["scope"] = []
        with self.assertRaisesRegex(ResourceEnvelopeError, "evidence is invalid"):
            validate_resource_evidence(evidence)

    def test_resource_evidence_validator_rejects_malformed_nested_counters(self):
        evidence = build_resource_evidence(
            binding=approved_phase3_resource_envelope_binding(),
            scope="workload",
            identity={"control_group": "/workload"},
            counters={},
        )
        evidence["counters"] = {"memory": []}
        with self.assertRaisesRegex(ResourceEnvelopeError, "counters"):
            validate_resource_evidence(evidence)

    def test_versioned_contract_has_exact_approved_envelopes(self):
        binding = approved_phase3_resource_envelope_binding()
        self.assertIs(validate_resource_envelope_binding(binding), binding)
        self.assertEqual(binding["schema_version"], "phase3_resource_envelope.v2")
        self.assertEqual(
            binding["envelopes"],
            {
                "common_workload_slot": {
                    "cpu_quota": 4,
                    "memory_high_bytes": 8 * GIB,
                    "memory_max_bytes": 12 * GIB,
                    "tasks_max": 384,
                    "memory_swap_max_bytes": 0,
                },
                "single_codex_mode": {
                    "cpu_quota": 4,
                    "memory_high_bytes": 8 * GIB,
                    "memory_max_bytes": 12 * GIB,
                    "tasks_max": 384,
                    "memory_swap_max_bytes": 0,
                },
                "agentteam_control_plane_allowance": {
                    "cpu_quota": 4,
                    "memory_high_bytes": 8 * GIB,
                    "memory_max_bytes": 12 * GIB,
                    "tasks_max": 384,
                    "memory_swap_max_bytes": 0,
                },
                "agentteam_mode": {
                    "cpu_quota": 8,
                    "memory_high_bytes": 16 * GIB,
                    "memory_max_bytes": 24 * GIB,
                    "tasks_max": 768,
                    "memory_swap_max_bytes": 0,
                },
                "pilot_project": {
                    "cpu_quota": 16,
                    "memory_high_bytes": 32 * GIB,
                    "memory_max_bytes": 48 * GIB,
                    "tasks_max": 1024,
                    "memory_swap_max_bytes": 0,
                },
            },
        )
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "phase3_resource_envelope.schema.json"
        )
        Draft202012Validator.check_schema(
            json.loads(schema_path.read_text(encoding="utf-8"))
        )
        mutated = deepcopy(binding)
        mutated["envelopes"]["common_workload_slot"]["cpu_quota"] = 5
        with self.assertRaisesRegex(ResourceEnvelopeError, "differs"):
            validate_resource_envelope_binding(mutated)

    def test_protocol_binding_is_additive_and_historical_protocols_work(self):
        protocol = {
            "environment": {
                "cpu_limit": 4,
                "memory_limit_bytes": 12 * GIB,
            }
        }
        self.assertIsNone(validate_protocol_resource_envelope(protocol))
        with self.assertRaisesRegex(ResourceEnvelopeError, "requires"):
            validate_protocol_resource_envelope(protocol, require_binding=True)
        binding = approved_phase3_resource_envelope_binding()
        reference = protocol_resource_envelope_reference(protocol, binding)
        self.assertEqual(
            reference["resource_envelope_sha256"],
            canonical_resource_envelope_sha256(binding),
        )
        drifted = deepcopy(protocol)
        drifted["environment"]["cpu_limit"] = 3
        with self.assertRaisesRegex(ResourceEnvelopeError, "cpu_limit"):
            validate_protocol_resource_envelope(drifted, binding)

    def test_host_capacity_reserves_scheduler_and_os_headroom(self):
        binding = approved_phase3_resource_envelope_binding()
        capacity = verify_host_capacity(
            binding,
            cpu_count=17,
            memory_total_bytes=49 * GIB,
        )
        self.assertEqual(capacity["reserved_cpu"], 1)
        with self.assertRaisesRegex(ResourceEnvelopeUnavailable, "CPU"):
            verify_host_capacity(
                binding,
                cpu_count=16,
                memory_total_bytes=64 * GIB,
            )
        with self.assertRaisesRegex(ResourceEnvelopeUnavailable, "memory"):
            verify_host_capacity(
                binding,
                cpu_count=32,
                memory_total_bytes=48 * GIB,
            )

    def test_hierarchy_applies_reads_back_and_drains_descendants(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()
        with tempfile.TemporaryDirectory() as tmp:
            hierarchy = SystemdResourceHierarchy(
                binding,
                run_id="RUN-P3B-001",
                mode="agentteam_full",
                command_runner=runner,
                cgroup_root=tmp,
            )
            runner.hierarchy = hierarchy
            identity = hierarchy.prepare(check_host=False)
            control_command = hierarchy.control_plane_command(["/bin/true"])
            control = hierarchy.verify_control_plane()
            leaf = hierarchy.verify_leaf("workload.service")
            self.assertEqual(
                leaf["ControlGroup"],
                "/project/mode/workload.service",
            )
            self.assertEqual(
                control["verified_limits"]["CPUQuotaPerSecUSec"],
                4_000_000,
            )
            arguments = hierarchy.leaf_arguments()
            self.assertIn("--property=MemoryHigh=8589934592", arguments)
            self.assertIn("--property=MemoryMax=12884901888", arguments)
            self.assertIn("--property=MemorySwapMax=0", arguments)
            self.assertEqual(
                identity["units"]["project"]["verified_limits"]["TasksMax"],
                1024,
            )
            for control_group in (
                "project",
                "project/mode",
                "project/mode/control.service",
            ):
                path = Path(tmp) / control_group
                path.mkdir(parents=True, exist_ok=True)
                (path / "cgroup.events").write_text(
                    "populated 0\nfrozen 0\n",
                    encoding="ascii",
                )
            cleanup = hierarchy.cleanup()
            self.assertTrue(cleanup["cleanup_complete"])
            self.assertEqual(
                set(cleanup["cgroup_population"].values()),
                {"drained"},
            )
            self.assertIn("--wait", control_command)
            self.assertIn("--property=TasksMax=384", control_command)
            self.assertNotIn("--pid=1234", control_command)

    def test_readback_mismatch_fails_before_leaf_admission(self):
        runner = _SystemdRunner()
        hierarchy = SystemdResourceHierarchy(
            approved_phase3_resource_envelope_binding(),
            run_id="RUN-P3B-002",
            mode="single_codex",
            command_runner=runner,
        )
        runner.hierarchy = hierarchy
        runner.mismatch_unit = hierarchy.project_slice
        with self.assertRaisesRegex(ResourceEnvelopeUnavailable, "mismatch"):
            hierarchy.prepare(check_host=False)
        self.assertFalse(
            any("workload.service" in command for command in runner.commands)
        )

    def test_cleanup_refuses_a_still_populated_descendant(self):
        runner = _SystemdRunner()
        with tempfile.TemporaryDirectory() as tmp:
            hierarchy = SystemdResourceHierarchy(
                approved_phase3_resource_envelope_binding(),
                run_id="RUN-P3B-POPULATED",
                mode="agentteam_direct",
                command_runner=runner,
                cgroup_root=tmp,
            )
            runner.hierarchy = hierarchy
            hierarchy.prepare(check_host=False)
            for control_group in ("project", "project/mode"):
                path = Path(tmp) / control_group
                path.mkdir(parents=True, exist_ok=True)
                (path / "cgroup.events").write_text(
                    "populated 1\nfrozen 0\n",
                    encoding="ascii",
                )
            cleanup = hierarchy.cleanup()
        self.assertFalse(cleanup["cleanup_complete"])
        self.assertEqual(
            set(cleanup["cgroup_population"].values()),
            {"populated"},
        )

    def test_borrower_revalidates_but_never_cleans_owner_units(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()
        owner = SystemdResourceHierarchy(
            binding,
            run_id="PILOT-OWNER",
            mode="agentteam_full",
            command_runner=runner,
        )
        runner.hierarchy = owner
        owner.prepare(check_host=False)
        reference = owner.owner_reference()
        runner.commands.clear()
        borrower = SystemdResourceHierarchy(
            binding,
            run_id="PILOT-OWNER",
            mode="agentteam_full",
            command_runner=runner,
            owner_reference=reference,
        )
        runner.hierarchy = borrower
        borrower.prepare(check_host=False)
        cleanup = borrower.cleanup()
        self.assertFalse(cleanup["cleanup_attempted"])
        self.assertFalse(cleanup["cleanup_owner"])
        self.assertFalse(
            any("stop" in command for command in runner.commands)
        )

    def test_borrower_rejects_owner_identity_drift(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()
        owner = SystemdResourceHierarchy(
            binding,
            run_id="PILOT-DRIFT",
            mode="single_codex",
            command_runner=runner,
        )
        runner.hierarchy = owner
        owner.prepare(check_host=False)
        reference = owner.owner_reference()
        reference["units"]["mode"]["ControlGroup"] = "/escaped"
        borrower = SystemdResourceHierarchy(
            binding,
            run_id="PILOT-DRIFT",
            mode="single_codex",
            command_runner=runner,
            owner_reference=reference,
        )
        runner.hierarchy = borrower
        with self.assertRaisesRegex(
            ResourceEnvelopeUnavailable,
            "identity changed",
        ):
            borrower.prepare(check_host=False)

    def test_pilot_owner_issues_three_mode_references_and_cleans_project_once(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()

        class BoundHierarchy(SystemdResourceHierarchy):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                runner.hierarchy = self

        owner = Phase3PilotResourceOwner(
            binding,
            pilot_id="PILOT-THREE-MODES",
            command_runner=runner,
            hierarchy_factory=BoundHierarchy,
        )
        references = owner.prepare(
            host_capacity={
                "cpu_count": 17,
                "memory_total_bytes": 49 * GIB,
            }
        )
        self.assertEqual(
            set(references),
            {"single_codex", "agentteam_direct", "agentteam_full"},
        )
        self.assertEqual(
            {item["run_id"] for item in references.values()},
            {"PILOT-THREE-MODES"},
        )
        for hierarchy in owner.hierarchies.values():
            for identity in hierarchy._identities.values():
                identity["ControlGroup"] = "/missing"
        runner.commands.clear()
        cleanup = owner.cleanup()
        project_stops = [
            command
            for command in runner.commands
            if "stop" in command
            and any(
                item.endswith(".slice")
                and "single-codex" not in item
                and "agentteam-direct" not in item
                and "agentteam-full" not in item
                for item in command
            )
        ]
        self.assertTrue(cleanup["cleanup_complete"])
        self.assertEqual(len(project_stops), 1)

    def test_pilot_owner_reads_project_and_mode_aggregate_evidence(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()
        with tempfile.TemporaryDirectory() as tmp:
            class BoundHierarchy(SystemdResourceHierarchy):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    runner.hierarchy = self

            owner = Phase3PilotResourceOwner(
                binding,
                pilot_id="PILOT-AGGREGATE",
                command_runner=runner,
                cgroup_root=tmp,
                hierarchy_factory=BoundHierarchy,
            )
            owner.prepare(
                host_capacity={
                    "cpu_count": 17,
                    "memory_total_bytes": 49 * GIB,
                }
            )
            identities = {
                owner.hierarchies["single_codex"]._identities["project"][
                    "ControlGroup"
                ],
                *(
                    hierarchy._identities["mode"]["ControlGroup"]
                    for hierarchy in owner.hierarchies.values()
                ),
            }
            for control_group in identities:
                cgroup = Path(tmp) / control_group.lstrip("/")
                cgroup.mkdir(parents=True, exist_ok=True)
                (cgroup / "cpu.stat").write_text(
                    "usage_usec 50\n",
                    encoding="ascii",
                )
                (cgroup / "memory.events").write_text(
                    "oom 0\n",
                    encoding="ascii",
                )
                (cgroup / "pids.events").write_text(
                    "max 0\n",
                    encoding="ascii",
                )
                (cgroup / "cgroup.events").write_text(
                    "populated 0\n",
                    encoding="ascii",
                )
            evidence = owner.evidence()
            self.assertEqual(evidence["project"]["scope"], "project")
            self.assertEqual(
                set(evidence["modes"]),
                {"single_codex", "agentteam_direct", "agentteam_full"},
            )
            self.assertTrue(
                all(
                    item["counters"]["cpu"]["usage_usec"] == 50
                    for item in evidence["modes"].values()
                )
            )

    def test_monitor_captures_short_lived_unit_without_owning_parents(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()
        with tempfile.TemporaryDirectory() as tmp:
            hierarchy = SystemdResourceHierarchy(
                binding,
                run_id="PILOT-MONITOR",
                mode="agentteam_direct",
                command_runner=runner,
                cgroup_root=tmp,
            )
            runner.hierarchy = hierarchy
            hierarchy.prepare(check_host=False)
            cgroup = Path(tmp) / "project" / "mode" / "control.service"
            cgroup.mkdir(parents=True)
            (cgroup / "cpu.stat").write_text(
                "usage_usec 25\nnr_throttled 0\n",
                encoding="ascii",
            )
            (cgroup / "memory.events").write_text(
                "oom 0\noom_kill 0\n",
                encoding="ascii",
            )
            (cgroup / "pids.events").write_text(
                "max 0\n",
                encoding="ascii",
            )
            (cgroup / "cgroup.events").write_text(
                "populated 1\n",
                encoding="ascii",
            )
            monitor = ResourceUnitMonitor(
                hierarchy,
                hierarchy.control_scope,
                scope="control_plane",
                sample_interval_seconds=0.01,
            ).start()
            evidence = monitor.finish(binding=binding)
            self.assertEqual(evidence["scope"], "control_plane")
            self.assertEqual(evidence["counters"]["cpu"]["usage_usec"], 25)
            self.assertFalse(evidence["outcome"]["resource_exhausted"])

    def test_borrower_can_stop_leaf_without_stopping_shared_slices(self):
        binding = approved_phase3_resource_envelope_binding()
        runner = _SystemdRunner()
        owner = SystemdResourceHierarchy(
            binding,
            run_id="PILOT-LEAF-CLEANUP",
            mode="agentteam_full",
            command_runner=runner,
        )
        runner.hierarchy = owner
        owner.prepare(check_host=False)
        borrower = SystemdResourceHierarchy(
            binding,
            run_id="PILOT-LEAF-CLEANUP",
            mode="agentteam_full",
            command_runner=runner,
            owner_reference=owner.owner_reference(),
        )
        runner.hierarchy = borrower
        borrower.prepare(check_host=False)
        runner.commands.clear()
        self.assertTrue(borrower.stop_transient_unit("leaf.service"))
        self.assertEqual(
            runner.commands,
            [["systemctl", "--user", "stop", "leaf.service"]],
        )

    def test_counters_and_exhaustion_are_not_model_quality_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cgroup = Path(tmp) / "project" / "mode" / "leaf"
            cgroup.mkdir(parents=True)
            (cgroup / "cpu.stat").write_text(
                "usage_usec 900\nuser_usec 700\nsystem_usec 200\n"
                "nr_periods 8\nnr_throttled 3\nthrottled_usec 100\n",
                encoding="ascii",
            )
            (cgroup / "memory.current").write_text("1024\n", encoding="ascii")
            (cgroup / "memory.peak").write_text("4096\n", encoding="ascii")
            (cgroup / "memory.events").write_text(
                "low 0\nhigh 2\nmax 1\noom 1\noom_kill 1\n",
                encoding="ascii",
            )
            (cgroup / "pids.current").write_text("4\n", encoding="ascii")
            (cgroup / "pids.peak").write_text("260\n", encoding="ascii")
            (cgroup / "pids.events").write_text("max 1\n", encoding="ascii")
            (cgroup / "cgroup.events").write_text(
                "populated 0\nfrozen 0\n",
                encoding="ascii",
            )
            counters = read_resource_counters(
                "/project/mode/leaf",
                cgroup_root=tmp,
            )
        outcome = classify_resource_exhaustion(counters, timed_out=True)
        self.assertEqual(outcome["classification"], "resource_limit_exhausted")
        self.assertFalse(outcome["model_quality_failure"])
        self.assertEqual(counters["memory"]["peak"], 4096)
        self.assertEqual(counters["cpu"]["nr_throttled"], 3)
        self.assertIn("pids_max", outcome["reasons"])
        self.assertIn("resource_driven_timeout", outcome["reasons"])
        evidence = build_resource_evidence(
            binding=approved_phase3_resource_envelope_binding(),
            scope="workload",
            identity={"control_group": "/project/mode/leaf"},
            counters=counters,
            timed_out=True,
            cleanup={"cleanup_complete": True},
        )
        self.assertEqual(
            evidence["outcome"]["classification"],
            "resource_limit_exhausted",
        )

    def test_evaluator_uses_common_leaf_and_returns_resource_evidence(self):
        captured = {}

        def capture(command, **_kwargs):
            captured["command"] = command
            return {
                "returncode": 0,
                "timed_out": False,
                "stdout": "",
                "stderr": "",
                "stdout_bytes": b"",
                "stderr_bytes": b"",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        with patch(
            "agentteam_runtime.experiment_sandbox._capture_bounded_process",
            side_effect=capture,
        ), patch(
            "agentteam_runtime.experiment_sandbox.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            result = _run_bounded_argv(
                ["/bin/true"],
                cwd="/",
                environment={"PATH": "/usr/bin:/bin"},
                timeout_seconds=5,
                max_output_bytes=4096,
                cpu_limit=4,
                memory_limit_bytes=12 * GIB,
                resource_envelope_binding=(
                    approved_phase3_resource_envelope_binding()
                ),
                resource_mode="agentteam_direct",
                resource_run_id="RUN-P3B-EVAL",
                resource_hierarchy_factory=_EvaluatorHierarchy,
                resource_hierarchy_reference={
                    "schema_version": "phase3_resource_hierarchy_reference.v1"
                },
            )
        self.assertIn("--slice=phase3-mode.slice", captured["command"])
        self.assertIn("--property=CPUQuota=400%", captured["command"])
        self.assertNotIn("--collect", captured["command"])
        self.assertEqual(result["resource_evidence"]["scope"], "evaluator")
        self.assertTrue(_EvaluatorHierarchy.instance.cleaned)


if __name__ == "__main__":
    unittest.main()

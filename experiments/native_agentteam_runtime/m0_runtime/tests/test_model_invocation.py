import json
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from agentteam_runtime.model_invocation import (
    ExecutionGroupIdentity,
    ModelInvocationCall,
    ProviderExecution,
    _systemd_gated_supervisor_command,
    invocation_context_from_message,
)
from agentteam_runtime.resource_envelope import (
    approved_phase3_resource_envelope_binding,
)


class _ResourceRunner:
    captured = None

    def __init__(self, lifecycle, command, **kwargs):
        self.lifecycle = lifecycle
        self.command = list(command)
        self.kwargs = kwargs
        self.__class__.captured = self

    def prepare(self):
        return ExecutionGroupIdentity(
            gated_supervisor_pid=123,
            gated_supervisor_pgid=123,
            host_boot_id="11111111-1111-1111-1111-111111111111",
            gated_supervisor_start_ticks=99,
            launch_nonce_sha256="1" * 64,
            systemd_linger_enabled=True,
            systemd_transient_unit="worker.service",
            systemd_transient_invocation_id="2" * 32,
            systemd_transient_kill_mode="control-group",
            systemd_user_manager_identity="InvocationID:manager",
            systemd_transient_control_group="/project/mode/worker.service",
            systemd_user_service_invocation_id="3" * 32,
            systemd_user_service_control_group="/user.slice/user-1000.slice",
            systemd_user_service_kill_mode="control-group",
        )

    def prepare_resources_before_admission(self):
        return None

    def permit_and_wait(self, **_kwargs):
        return ProviderExecution(
            self.command,
            0,
            "",
            "",
            resource_evidence={
                "resource_evidence_schema_version": (
                    "phase3_resource_evidence.v1"
                ),
                "outcome": {
                    "classification": None,
                    "resource_exhausted": False,
                    "reasons": [],
                    "model_quality_failure": None,
                },
            },
        )

    def cleanup_after_terminal(self):
        return {
            "leaf_unit_stopped": True,
            "leaf_cgroup_empty": True,
        }


class ModelInvocationResourceTests(unittest.TestCase):
    def test_supervisor_command_applies_leaf_limits_before_exec_separator(self):
        command = _systemd_gated_supervisor_command(
            "worker.service",
            "/runtime/model_invocation.py",
            "/authority/spec.json",
            resource_arguments=(
                "--slice=mode.slice",
                "--property=CPUQuota=400%",
                "--property=MemoryHigh=8589934592",
                "--property=MemoryMax=12884901888",
                "--property=TasksMax=384",
                "--property=MemorySwapMax=0",
            ),
        )
        separator = command.index("--")
        self.assertLess(command.index("--slice=mode.slice"), separator)
        self.assertLess(command.index("--property=MemoryMax=12884901888"), separator)

    def test_model_worker_binding_reaches_runner_and_persists_cleanup(self):
        binding = approved_phase3_resource_envelope_binding()
        message = {
            "to_agent": "agent-worker",
            "payload": {
                "project": "phase3",
                "run_id": "RUN-P3B-MODEL",
                "taskpack_id": "phase3-taskpack",
                "attempt_id": "ATTEMPT-1",
                "runtime_execution_session_id": "SESSION-1",
                "lifecycle_owner_token": "OWNER-1",
                "agent_id": "agent-worker",
                "agent_role": "implementation_worker",
                "usage_stage": "implementation_worker",
                "coverage_class": "supported_model_invocation",
                "experiment_mode": "agentteam_full",
                "resource_envelope_required": True,
                "resource_project_id": "PILOT-PROJECT-1",
                "resource_envelope_binding": binding,
                "resource_hierarchy_reference": {
                    "schema_version": "phase3_resource_hierarchy_reference.v1",
                    "binding_sha256": "a" * 64,
                },
            },
        }
        context = invocation_context_from_message(message, model="gpt-test")
        with tempfile.TemporaryDirectory() as tmp:
            call = ModelInvocationCall(
                tmp,
                context,
                supported=True,
                systemd_runner_factory=_ResourceRunner,
            )
            execution = call.execute(
                ["/bin/true"],
                cwd="/",
                input_text="",
                timeout_seconds=5,
            )
            terminal = call.finalize("completed", execution)
            resource_path = Path(tmp) / "model_invocations" / terminal[
                "invocation_id"
            ] / "resource.json"
            resource = json.loads(resource_path.read_text(encoding="utf-8"))
        self.assertEqual(
            _ResourceRunner.captured.kwargs["resource_envelope_binding"],
            binding,
        )
        self.assertEqual(
            _ResourceRunner.captured.kwargs["resource_mode"],
            "agentteam_full",
        )
        self.assertEqual(
            _ResourceRunner.captured.kwargs["resource_run_id"],
            "PILOT-PROJECT-1",
        )
        self.assertTrue(resource["cleanup"]["leaf_unit_stopped"])
        self.assertTrue(resource["cleanup"]["leaf_cgroup_empty"])

    def test_resource_readback_precedes_provider_admission(self):
        events = []

        class OrderedRunner(_ResourceRunner):
            def prepare_resources_before_admission(self):
                events.append("resource_readback")

            def prepare(self):
                events.append("supervisor_prepare")
                return super().prepare()

        class OrderedCall(ModelInvocationCall):
            def _acquire_experiment_provider_admission(self):
                events.append("provider_admission")

        message = {
            "to_agent": "agent-worker",
            "payload": {
                "project": "phase3",
                "run_id": "RUN-P3B-ORDER",
                "taskpack_id": "phase3-taskpack",
                "attempt_id": "ATTEMPT-ORDER",
                "runtime_execution_session_id": "SESSION-ORDER",
                "lifecycle_owner_token": "OWNER-ORDER",
                "agent_id": "agent-worker",
                "agent_role": "implementation_worker",
                "usage_stage": "implementation_worker",
                "coverage_class": "supported_model_invocation",
                "experiment_mode": "single_codex",
                "resource_envelope_required": True,
                "resource_envelope_binding": (
                    approved_phase3_resource_envelope_binding()
                ),
                "resource_hierarchy_reference": {
                    "schema_version": "phase3_resource_hierarchy_reference.v1",
                    "binding_sha256": "a" * 64,
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            call = OrderedCall(
                tmp,
                invocation_context_from_message(message, model="gpt-test"),
                supported=True,
                systemd_runner_factory=OrderedRunner,
            )
            call.execute(
                ["/bin/true"],
                cwd="/",
                input_text="",
                timeout_seconds=5,
            )
        self.assertEqual(
            events,
            [
                "resource_readback",
                "supervisor_prepare",
                "provider_admission",
            ],
        )


if __name__ == "__main__":
    unittest.main()

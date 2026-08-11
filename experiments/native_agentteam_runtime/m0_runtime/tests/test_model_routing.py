import json
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime.mailbox_worker import _runtime_adapter_for_dispatch
from agentteam_runtime.agentteam import (
    _set_taskpack_model_routing_policy,
    _submit_model_routing_policies,
)
from agentteam_runtime.m0_runtime import (
    CodexRuntimeAdapter,
    replay_events,
    run_simulation,
)
from agentteam_runtime.model_invocation import invocation_context_from_message
from agentteam_runtime.model_routing import (
    author_model_route,
    default_model_routing_policy,
    select_model_route,
    validate_model_routing_policy,
)
from agentteam_runtime.profile import build_project_profile
from agentteam_runtime.retry_decision import decide_retry
from agentteam_runtime.taskpack import draft_taskpack_files, load_taskpack
from agentteam_runtime.two_phase_scheduler import TwoPhaseFileScheduler


class ModelRoutingTest(unittest.TestCase):
    class _RetryAdapter:
        def __init__(self):
            self.messages = []

        def run(self, message, worktree_path=None):
            self.messages.append(message)
            if len(self.messages) == 1:
                return {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {"error": "first attempt failed"},
                }
            return {
                "result_status": "completed",
                "changed_files": [],
                "output": {"operator_summary": {"what_changed": "none"}},
            }

    @staticmethod
    def _append_two_phase_result(inflight, *, result_status, output):
        record = {
            "message_id": f"RESULT-{inflight['message_id']}",
            "from_agent": inflight["agent_id"],
            "to_agent": "agent-scheduler",
            "message_type": "runtime_result",
            "correlation_id": f"{inflight['task_id']}:{inflight['attempt_id']}",
            "created_at": "2026-08-10T00:00:00Z",
            "payload": {
                "source_message_id": inflight["message_id"],
                "task_id": inflight["task_id"],
                "attempt_id": inflight["attempt_id"],
                "lease_id": inflight["lease_id"],
                "result_status": result_status,
                "changed_files": [],
                "output": output,
            },
        }
        outbox = Path(inflight["outbox_path"])
        outbox.parent.mkdir(parents=True, exist_ok=True)
        outbox.write_text(json.dumps(record) + "\n", encoding="utf-8")

    @staticmethod
    def _two_phase_scheduler(root, *, max_attempts=1):
        agent_pool_path = root / "agent_pool.json"
        backlog_path = root / "backlog.json"
        output_dir = root / "run"
        agent_pool_path.write_text(
            json.dumps(
                {
                    "scheduler_agent_id": "agent-scheduler",
                    "model_routing_policy": default_model_routing_policy(),
                    "agents": [
                        {
                            "agent_id": "agent-repo-map-1",
                            "role": "repo_map_agent",
                            "status": "idle",
                            "inbox_path": "mailboxes/repo-map/inbox.jsonl",
                            "outbox_path": "mailboxes/repo-map/outbox.jsonl",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        backlog_path.write_text(
            json.dumps(
                {
                    "backlog_id": "BL-TWO-PHASE-ROUTING",
                    "items": [
                        {
                            "task_id": "TASK-REPO-MAP",
                            "objective": "map repository",
                            "required_role": "repo_map_agent",
                            "risk_target": "L0",
                            "read_scope": ["."],
                            "write_scope": [],
                            "depends_on": [],
                            "blockers": [],
                            "backlog_status": "ready",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            max_attempts=max_attempts,
        )

    def test_cost_aware_routes_cover_role_and_risk(self):
        policy = default_model_routing_policy()
        cases = (
            ("repo-map-l0", "repo_map_agent", "L0", "gpt-5.6-luna", "medium"),
            ("implementation-l1", "implementation_worker", "L1", "gpt-5.6-terra", "medium"),
            ("implementation-l2", "implementation_worker", "L2", "gpt-5.6-sol", "high"),
        )

        for case_id, role, risk_target, model, reasoning_profile in cases:
            with self.subTest(case_id=case_id):
                route = select_model_route(
                    policy,
                    role=role,
                    risk_target=risk_target,
                    attempt_number=1,
                )
                self.assertEqual(
                    (route["model"], route["reasoning_profile"]),
                    (model, reasoning_profile),
                )

    def test_retry_escalates_once_without_changing_task_authority(self):
        policy = default_model_routing_policy()
        first = select_model_route(
            policy,
            role="implementation_worker",
            risk_target="L1",
            attempt_number=1,
        )
        retry = select_model_route(
            policy,
            role="implementation_worker",
            risk_target="L1",
            attempt_number=2,
            retry_decision=decide_retry(
                attempt_id="ATTEMPT-001",
                failure_category="runtime_error",
                retryable=True,
                runtime_result={
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {},
                },
            ),
        )

        self.assertEqual(first["model"], "gpt-5.6-terra")
        self.assertEqual(retry["model"], "gpt-5.6-sol")
        self.assertEqual(retry["reasoning_profile"], "high")
        self.assertEqual(retry["escalation_level"], 1)
        self.assertIn("RETRY-ATTEMPT-001", retry["selection_reason"])

    def test_transient_retry_keeps_the_same_model(self):
        policy = default_model_routing_policy()
        decision = decide_retry(
            attempt_id="ATTEMPT-001",
            failure_category="runtime_error",
            retryable=True,
            runtime_result={
                "result_status": "failed",
                "changed_files": [],
                "output": {
                    "adapter": "codex",
                    "exit_code": 1,
                    "stderr": "429 rate limit exceeded",
                },
            },
        )

        route = select_model_route(
            policy,
            role="implementation_worker",
            risk_target="L1",
            attempt_number=2,
            retry_decision=decision,
        )

        self.assertEqual(decision["action"], "retry_same_model")
        self.assertEqual(route["model"], "gpt-5.6-terra")
        self.assertEqual(route["escalation_level"], 0)

    def test_fixed_policy_never_escalates(self):
        policy = default_model_routing_policy(
            fixed_profile={
                "model": "gpt-5.6-terra",
                "reasoning_profile": "low",
            }
        )
        validate_model_routing_policy(policy)

        route = select_model_route(
            policy,
            role="implementation_worker",
            risk_target="L3",
            attempt_number=4,
        )

        self.assertEqual(route["model"], "gpt-5.6-terra")
        self.assertEqual(route["reasoning_profile"], "low")
        self.assertEqual(route["escalation_level"], 0)

    def test_author_defaults_to_balanced_model(self):
        route = author_model_route(default_model_routing_policy())
        self.assertEqual(route["model"], "gpt-5.6-terra")
        self.assertEqual(route["reasoning_profile"], "medium")

    def test_codex_author_routes_when_worker_runtime_is_fake(self):
        author_policy, worker_policy = _submit_model_routing_policies(
            author_runtime="codex",
            runtime_backend="fake",
        )

        route = author_model_route(author_policy)
        self.assertEqual(route["model"], "gpt-5.6-terra")
        self.assertEqual(route["reasoning_profile"], "medium")
        self.assertIsNone(worker_policy)

    def test_dispatch_route_overrides_long_lived_worker_adapter_per_attempt(self):
        base = CodexRuntimeAdapter(
            model="gpt-5.6-sol",
            reasoning_profile="high",
            timeout_seconds=123,
        )
        route = select_model_route(
            default_model_routing_policy(),
            role="repo_map_agent",
            risk_target="L0",
            attempt_number=1,
        )
        routed = _runtime_adapter_for_dispatch(
            base,
            {"payload": {"model_routing": route}},
        )

        self.assertIsNot(routed, base)
        self.assertEqual(routed.model, "gpt-5.6-luna")
        self.assertEqual(routed.reasoning_profile, "medium")
        self.assertEqual(routed.timeout_seconds, 123)
        self.assertEqual(base.model, "gpt-5.6-sol")

    def test_cli_policy_injection_enables_adaptive_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            draft = root / "draft"
            result = draft_taskpack_files(
                project_root=project,
                goal="implement a bounded change",
                draft_root=draft,
                taskpack_id="routing-default",
            )
            _set_taskpack_model_routing_policy(
                result["taskpack_dir"],
                default_model_routing_policy(),
            )

            loaded = load_taskpack(result["taskpack_dir"])
            policy = loaded["agent_pool"]["model_routing_policy"]
            self.assertEqual(policy["mode"], "adaptive")
            self.assertTrue(policy["retry_escalation"])

    def test_explicit_model_keeps_legacy_fixed_profile_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            result = draft_taskpack_files(
                project_root=project,
                goal="implement a bounded change",
                draft_root=root / "draft",
                taskpack_id="routing-fixed-compat",
                codex_model="gpt-5.6-sol",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertNotIn("model_routing_policy", loaded["agent_pool"])
            for profile in loaded["agent_pool"]["role_runtime_profiles"].values():
                self.assertEqual(profile["model"], "gpt-5.6-sol")

    def test_scheduler_freezes_route_in_each_retry_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_pool_path = root / "agent_pool.json"
            backlog_path = root / "backlog.json"
            output_dir = root / "run"
            agent_pool_path.write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "model_routing_policy": default_model_routing_policy(),
                        "agents": [
                            {
                                "agent_id": "agent-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "mailboxes/worker/inbox.jsonl",
                                "outbox_path": "mailboxes/worker/outbox.jsonl",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "backlog_id": "BL-ROUTING",
                        "items": [
                            {
                                "task_id": "TASK-ROUTING",
                                "objective": "exercise retry routing",
                                "required_role": "implementation_worker",
                                "risk_target": "L1",
                                "read_scope": ["."],
                                "write_scope": [],
                                "depends_on": [],
                                "blockers": [],
                                "backlog_status": "ready",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            adapter = self._RetryAdapter()

            result = run_simulation(
                agent_pool_path,
                backlog_path,
                output_dir,
                runtime_adapter=adapter,
                max_attempts=2,
            )

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(len(adapter.messages), 2)
            first = adapter.messages[0]["payload"]["model_routing"]
            retry = adapter.messages[1]["payload"]["model_routing"]
            self.assertEqual(first["model"], "gpt-5.6-terra")
            self.assertEqual(retry["model"], "gpt-5.6-sol")
            self.assertEqual(retry["escalation_level"], 1)
            self.assertEqual(
                result["attempts"][0]["retry_decision"]["action"],
                "escalate_model",
            )
            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["retry_decision"],
                result["attempts"][0]["retry_decision"],
            )

    def test_two_phase_dispatch_publishes_authoritative_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheduler = self._two_phase_scheduler(root)

            scheduler.dispatch_ready()

            inflight = scheduler.state["inflight_attempts"][0]
            inbox = (
                Path(inflight["step_dir"])
                / "mailboxes"
                / "repo-map"
                / "inbox.jsonl"
            )
            message = json.loads(inbox.read_text(encoding="utf-8").splitlines()[0])
            route = message["payload"]["model_routing"]
            self.assertEqual(route["model"], "gpt-5.6-luna")
            self.assertEqual(route["reasoning_profile"], "medium")
            self.assertEqual(inflight["model_routing"], route)

    def test_two_phase_transient_retry_handoff_keeps_base_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheduler = self._two_phase_scheduler(root, max_attempts=2)

            scheduler.dispatch_ready()
            first = scheduler.state["inflight_attempts"][0]
            self._append_two_phase_result(
                first,
                result_status="failed",
                output={
                    "adapter": "codex",
                    "exit_code": 1,
                    "stderr": "429 rate limit exceeded",
                },
            )
            collected = scheduler.collect_ready_results()["results"][0]
            scheduler.dispatch_ready()
            second = scheduler.state["inflight_attempts"][0]
            inbox = (
                Path(second["step_dir"])
                / "mailboxes"
                / "repo-map"
                / "inbox.jsonl"
            )
            message = json.loads(inbox.read_text(encoding="utf-8").splitlines()[-1])

            self.assertEqual(
                collected["retry_decision"]["action"],
                "retry_same_model",
            )
            self.assertEqual(
                message["payload"]["retry_handoff"]["retry_decision"],
                collected["retry_decision"],
            )
            self.assertEqual(message["payload"]["model"], "gpt-5.6-luna")
            self.assertEqual(
                message["payload"]["model_routing"]["escalation_level"],
                0,
            )

    def test_invocation_context_preserves_route_reason(self):
        route = select_model_route(
            default_model_routing_policy(),
            role="implementation_worker",
            risk_target="L1",
            attempt_number=2,
            retry_decision=decide_retry(
                attempt_id="ATTEMPT-001",
                failure_category="runtime_error",
                retryable=True,
                runtime_result={
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {},
                },
            ),
        )
        context = invocation_context_from_message(
            {
                "to_agent": "agent-worker-1",
                "payload": {
                    "task_id": "TASK-1",
                    "attempt_id": "ATTEMPT-2",
                    "model": route["model"],
                    "reasoning_profile": route["reasoning_profile"],
                    "model_routing": route,
                },
            }
        )

        self.assertEqual(context["model_routing"], route)
        self.assertIn("RETRY-ATTEMPT-001", context["model_routing"]["selection_reason"])

    def test_fixed_project_model_persists_explicit_reasoning_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = build_project_profile(
                tmp,
                codex_model="gpt-5.6-terra",
            )

        self.assertEqual(profile["codex_model"], "gpt-5.6-terra")
        self.assertEqual(profile["reasoning_profile"], "high")


if __name__ == "__main__":
    unittest.main()

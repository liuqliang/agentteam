import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from agentteam_runtime.decision_ledger import DecisionLedger, decision_record_sha256
from agentteam_runtime.decision_runtime import (
    DecisionRuntimeError,
    load_run_decision_binding,
    publish_run_decision_binding,
    validate_taskpack_decision_contract,
)
from agentteam_runtime.projection_db import (
    read_projected_decision_graph,
    rebuild_project_projection_db,
)
from agentteam_runtime.two_phase_scheduler import TwoPhaseFileScheduler


ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"


class FixedClock:
    def __init__(self):
        self.base = datetime(2026, 8, 6, tzinfo=UTC)
        self.seconds = 0

    def now(self):
        value = self.base + timedelta(seconds=self.seconds)
        self.seconds += 1
        return value.isoformat().replace("+00:00", "Z")


class DecisionRuntimeTests(unittest.TestCase):
    def _decision(self, decision_id="DEC-direction", *, kind="direction", parent=None):
        return {
            "schema_version": "decision_record.v1",
            "decision_id": decision_id,
            "revision": 1,
            "decision_kind": kind,
            "subject": "implementation_route" if kind == "execution" else "project_direction",
            "authority_level": "L2",
            "parent_decision_id": parent,
            "supersedes_decision_id": None,
            "statement": "Implement the explicitly authorized runtime change.",
            "selected_option": "bounded_implementation",
            "alternatives": [],
            "rationale": "The taskpack authority selected this bounded route.",
            "scope": ["runtime"],
            "expected_outcome": "The declared task is implemented and verified.",
            "acceptance_refs": ["verification-command"],
            "status": "active",
            "created_at": "2026-08-06T00:00:00Z",
            "created_by": "taskpack-semantic-authority",
            "previous_revision_sha256": None,
        }

    def _contract(self):
        return {
            "schema_version": "taskpack_decision_contract.v1",
            "root_decision_id": "DEC-direction",
            "decisions": [
                self._decision(),
                self._decision(
                    "DEC-execution",
                    kind="execution",
                    parent="DEC-direction",
                ),
            ],
            "task_bindings": {"TASK-001": "DEC-execution"},
        }

    def _publish(self, root):
        work_root = root / "work"
        frozen_dir = work_root / "frozen" / "decision-taskpack"
        run_dir = work_root / "runs" / "decision-taskpack"
        frozen_dir.mkdir(parents=True)
        (frozen_dir / "manifest.json").write_text(
            json.dumps({"digest_sha256": "a" * 64}),
            encoding="utf-8",
        )
        taskpack = {
            "taskpack_id": "decision-taskpack",
            "decision_contract": self._contract(),
        }
        binding = publish_run_decision_binding(
            work_root,
            frozen_dir,
            run_dir,
            taskpack,
            task_ids=["TASK-001"],
        )
        return work_root, run_dir, binding

    def test_contract_rejects_scheduler_authored_acceptance_and_unknown_tasks(self):
        contract = self._contract()
        contract["decisions"].append(
            self._decision(
                "DEC-acceptance",
                kind="acceptance",
                parent="DEC-execution",
            )
        )
        with self.assertRaisesRegex(
            DecisionRuntimeError,
            "acceptance decisions must be created",
        ):
            validate_taskpack_decision_contract(contract, task_ids=["TASK-001"])

        contract = self._contract()
        contract["task_bindings"]["TASK-UNKNOWN"] = "DEC-direction"
        with self.assertRaisesRegex(DecisionRuntimeError, "unknown task"):
            validate_taskpack_decision_contract(contract, task_ids=["TASK-001"])

    def test_publish_and_load_binding_are_idempotent_and_link_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            work_root, run_dir, first = self._publish(Path(temporary))
            second = publish_run_decision_binding(
                work_root,
                work_root / "frozen" / "decision-taskpack",
                run_dir,
                {
                    "taskpack_id": "decision-taskpack",
                    "decision_contract": self._contract(),
                },
                task_ids=["TASK-001"],
            )

            self.assertEqual(first, second)
            self.assertEqual(load_run_decision_binding(run_dir), first)
            ledger = DecisionLedger(work_root)
            self.assertEqual(
                [item["decision_id"] for item in ledger.latest_decisions()],
                ["DEC-direction", "DEC-execution"],
            )
            self.assertEqual(ledger.artifact_links()[0]["artifact_kind"], "contract")

    def test_scheduler_inherits_decision_and_records_acceptance_before_noop_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root, run_dir, _binding = self._publish(root)
            agent_pool = root / "agent_pool.json"
            backlog = root / "backlog.json"
            agent_pool.write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "agent-worker",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "mailboxes/agent-worker/inbox.jsonl",
                                "outbox_path": "mailboxes/agent-worker/outbox.jsonl",
                                "lease": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            backlog.write_text(
                json.dumps(
                    {
                        "backlog_id": "BL-decision-taskpack",
                        "items": [
                            {
                                "task_id": "TASK-001",
                                "objective": "Perform one bounded no-op verification.",
                                "backlog_status": "ready",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                                "read_scope": ["."],
                                "write_scope": [],
                                "required_role": "implementation_worker",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool,
                backlog,
                run_dir,
                clock=FixedClock(),
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            message = json.loads(
                Path(inflight["step_dir"])
                .joinpath("mailboxes/agent-worker/inbox.jsonl")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(message["payload"]["decision_id"], "DEC-execution")
            result_record = {
                "message_id": "RESULT-001",
                "from_agent": "agent-worker",
                "to_agent": "agent-scheduler",
                "message_type": "runtime_result",
                "correlation_id": inflight["correlation_id"],
                "created_at": "2026-08-06T00:01:00Z",
                "payload": {
                    "source_message_id": inflight["message_id"],
                    "task_id": inflight["task_id"],
                    "attempt_id": inflight["attempt_id"],
                    "lease_id": inflight["lease_id"],
                    "result_status": "completed",
                    "changed_files": [],
                    "output": {
                        "operator_summary": {
                            "what_changed": ["未修改代码，仅完成合同验证。"],
                            "verification_summary": ["合同验证通过。"],
                        }
                    },
                },
            }
            outbox = Path(inflight["outbox_path"])
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(json.dumps(result_record) + "\n", encoding="utf-8")

            result = scheduler.collect_ready_results()["results"][0]
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            acceptance_id = result["acceptance_decision_id"]
            event_types = [item["event_type"] for item in events]
            self.assertLess(
                event_types.index("acceptance_decision_recorded"),
                event_types.index("integration_noop_verified"),
            )
            inherited = [
                item for item in events if item["event_type"] == "message_dispatched"
            ][0]
            accepted = [
                item for item in events if item["event_type"] == "integration_noop_verified"
            ][0]
            self.assertEqual(inherited["decision_id"], "DEC-execution")
            self.assertEqual(accepted["decision_id"], acceptance_id)
            decision = {
                item["decision_id"]: item
                for item in DecisionLedger(work_root).latest_decisions()
            }[acceptance_id]
            self.assertEqual(decision["created_by"], "verification-integration-controller")
            projection = rebuild_project_projection_db(work_root)
            with sqlite3.connect(projection["db_path"]) as connection:
                self.assertEqual(
                    connection.execute(
                        "select decision_id from runs where run_id = ?",
                        (run_dir.name,),
                    ).fetchone()[0],
                    "DEC-direction",
                )
                self.assertEqual(
                    connection.execute(
                        "select decision_id from tasks where run_id = ? and task_id = ?",
                        (run_dir.name, "TASK-001"),
                    ).fetchone()[0],
                    "DEC-execution",
                )
                projected_acceptance = connection.execute(
                    "select decision_id from events where run_id = ? "
                    "and event_type = 'acceptance_decision_recorded'",
                    (run_dir.name,),
                ).fetchone()[0]
                self.assertEqual(projected_acceptance, acceptance_id)
            graph = read_projected_decision_graph(work_root, "DEC-direction")
            execution_node = graph["decision_graph"]["children"][0]
            self.assertEqual(
                execution_node["execution"]["tasks"],
                [{"run_id": run_dir.name, "task_id": "TASK-001"}],
            )
            self.assertEqual(
                execution_node["execution"]["attempts"][0]["attempt_id"],
                result["attempt_id"],
            )
            validator = Draft202012Validator(
                json.loads((SCHEMAS / "event.schema.json").read_text(encoding="utf-8"))
            )
            validator.validate(inherited)
            validator.validate(
                [
                    item
                    for item in events
                    if item["event_type"] == "acceptance_decision_recorded"
                ][0]
            )

    def test_legacy_scheduler_does_not_fabricate_decision_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent_pool = root / "agent_pool.json"
            backlog = root / "backlog.json"
            agent_pool.write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "agent-worker",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "inbox.jsonl",
                                "outbox_path": "outbox.jsonl",
                                "lease": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            backlog.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": "TASK-001",
                                "objective": "Legacy task.",
                                "backlog_status": "ready",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                                "read_scope": ["."],
                                "write_scope": [],
                                "required_role": "implementation_worker",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            run_dir = root / "run"
            scheduler = TwoPhaseFileScheduler(agent_pool, backlog, run_dir, clock=FixedClock())
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            message = json.loads(
                (run_dir / "steps" / inflight["step_id"] / "inbox.jsonl").read_text(
                    encoding="utf-8"
                )
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(events)
            self.assertTrue(all("decision_id" not in event for event in events))
            self.assertNotIn("decision_id", message["payload"])
            self.assertNotIn("decision_id", inflight)

    def test_inactive_inherited_decision_stops_before_acceptance_or_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work_root, run_dir, _binding = self._publish(root)
            agent_pool = root / "agent_pool.json"
            backlog = root / "backlog.json"
            agent_pool.write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "agent-worker",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "inbox.jsonl",
                                "outbox_path": "outbox.jsonl",
                                "lease": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            backlog.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": "TASK-001",
                                "objective": "Do not integrate stale authority.",
                                "backlog_status": "ready",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                                "read_scope": ["."],
                                "write_scope": [],
                                "required_role": "implementation_worker",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            scheduler = TwoPhaseFileScheduler(agent_pool, backlog, run_dir, clock=FixedClock())
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            ledger = DecisionLedger(work_root)
            execution = {
                item["decision_id"]: item for item in ledger.latest_decisions()
            }["DEC-execution"]
            completed = deepcopy(execution)
            completed.update(
                {
                    "revision": 2,
                    "status": "completed",
                    "created_at": "2026-08-06T00:02:00Z",
                    "previous_revision_sha256": decision_record_sha256(execution),
                }
            )
            ledger.append_decision(completed)
            outbox = Path(inflight["outbox_path"])
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(
                json.dumps(
                    {
                        "message_id": "RESULT-stale",
                        "from_agent": "agent-worker",
                        "to_agent": "agent-scheduler",
                        "message_type": "runtime_result",
                        "correlation_id": inflight["correlation_id"],
                        "created_at": "2026-08-06T00:03:00Z",
                        "payload": {
                            "source_message_id": inflight["message_id"],
                            "task_id": inflight["task_id"],
                            "attempt_id": inflight["attempt_id"],
                            "lease_id": inflight["lease_id"],
                            "result_status": "completed",
                            "changed_files": [],
                            "output": {"operator_summary": {"what_changed": ["none"]}},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DecisionRuntimeError, "no longer active"):
                scheduler.collect_ready_results()
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("acceptance_decision_recorded", events)
            self.assertNotIn("integration_noop_verified", events)


if __name__ == "__main__":
    unittest.main()

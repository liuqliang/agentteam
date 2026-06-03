import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentteam_runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    FileSchedulerDaemon,
    FileMailboxExternalRuntimeAdapter,
    FileMailboxRuntimeAdapter,
    FileMailboxSubprocessRuntimeAdapter,
    FileMailboxWorkerProcessSupervisor,
    FileMailboxWorkerPoolSupervisor,
    FileMailboxWorker,
    ShellRuntimeAdapter,
    TwoPhaseFileScheduler,
    audit_worktree_diff,
    build_planner_context,
    classify_attempt_outcome,
    normalize_task_proposal,
    read_scheduler_state_index,
    replay_events,
    run_file_daemon,
    run_scheduler_loop,
    run_simulation,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "schemas"


class FixedClock:
    def __init__(self):
        self._ticks = iter(
            f"2026-05-31T00:00:{second:02d}Z"
            for second in range(60)
        )

    def now(self):
        return next(self._ticks)


class M0RuntimeTests(unittest.TestCase):
    def test_build_planner_context_summarizes_state_roles_and_scopes(self):
        agent_pool = {
            "agents": [
                {"agent_id": "agent-planner", "role": "task_planner"},
                {"agent_id": "agent-repo-map", "role": "repo_map_agent"},
            ]
        }
        state = {
            "backlog": {
                "items": [
                    {"task_id": "TASK-DONE", "backlog_status": "done"},
                    {"task_id": "TASK-BLOCKED", "backlog_status": "blocked"},
                ]
            },
            "steps": [{"task_id": "TASK-DONE", "step_status": "processed"}],
            "inflight_attempts": [],
        }

        context = build_planner_context(
            agent_pool,
            state,
            milestone_id="M22",
            default_worker_role="repo_map_agent",
            allowed_read_scopes=["."],
            allowed_write_scopes=["generated/"],
        )

        self.assertEqual(context["context_schema_version"], "planner_context.v1")
        self.assertEqual(context["milestone_id"], "M22")
        self.assertEqual(context["default_worker_role"], "repo_map_agent")
        self.assertEqual(context["allowed_write_scopes"], ["generated/"])
        self.assertEqual(
            context["available_agent_roles"],
            ["repo_map_agent", "task_planner"],
        )
        self.assertEqual(context["backlog_summary"]["done"], 1)
        self.assertEqual(context["backlog_summary"]["blocked"], 1)
        self.assertEqual(context["completed_task_ids"], ["TASK-DONE"])
        self.assertIn("proposal_contract", context)

    def test_task_proposal_normalizes_valid_generated_tasks(self):
        proposal = {
            "milestone_id": "M21",
            "tasks": [
                {
                    "task_id": "TASK-M21-001",
                    "objective": "Add a bounded generated task.",
                    "read_scope": ["experiments/native_agentteam_runtime/"],
                    "write_scope": ["experiments/native_agentteam_runtime/generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L1",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        normalized = normalize_task_proposal(
            proposal,
            existing_task_ids={"DECOMPOSE-M21-001"},
        )

        self.assertEqual(normalized["proposal_status"], "accepted")
        self.assertEqual(normalized["generated_task_ids"], ["TASK-M21-001"])
        self.assertEqual(normalized["tasks"][0]["backlog_status"], "ready")
        self.assertEqual(normalized["tasks"][0]["milestone_id"], "M21")

    def test_task_proposal_rejects_duplicate_existing_task_id(self):
        proposal = {
            "milestone_id": "M21",
            "tasks": [
                {
                    "task_id": "TASK-M21-001",
                    "objective": "Duplicate task id.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "duplicate task_id"):
            normalize_task_proposal(
                proposal,
                existing_task_ids={"TASK-M21-001"},
            )

    def test_task_proposal_rejects_unknown_required_role(self):
        proposal = {
            "milestone_id": "M22",
            "tasks": [
                {
                    "task_id": "TASK-M22-001",
                    "objective": "Use an unknown role.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "required_role": "unknown_role",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "unknown required_role"):
            normalize_task_proposal(
                proposal,
                allowed_roles={"repo_map_agent"},
            )

    def test_task_proposal_rejects_write_scope_outside_allowed_prefix(self):
        proposal = {
            "milestone_id": "M22",
            "tasks": [
                {
                    "task_id": "TASK-M22-001",
                    "objective": "Write outside generated scope.",
                    "read_scope": ["."],
                    "write_scope": ["src/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "write_scope outside allowed scope"):
            normalize_task_proposal(
                proposal,
                allowed_roles={"repo_map_agent"},
                allowed_write_scopes=["generated/"],
            )

    def test_run_simulation_dispatches_ready_task_and_validates_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )

            self.assertEqual(result["task_id"], "TASK-001")
            self.assertEqual(result["attempt_id"], "ATTEMPT-001")
            self.assertEqual(result["lease_id"], "LEASE-001")
            self.assertEqual(result["message_id"], "MSG-0001")
            self.assertEqual(result["worktree_id"], "WT-ATTEMPT-001")
            self.assertEqual(result["validation_status"], "accepted")

            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            self.assertTrue(inbox.exists())
            message = json.loads(inbox.read_text(encoding="utf-8").strip())
            self.assertEqual(message["message_type"], "dispatch_task")
            self.assertEqual(message["payload"]["attempt_id"], "ATTEMPT-001")
            self.assertEqual(message["payload"]["worktree_id"], "WT-ATTEMPT-001")

    def test_scheduler_loop_runs_ready_tasks_until_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            state = json.loads(Path(summary["state_path"]).read_text(encoding="utf-8"))
            statuses = {
                item["task_id"]: item["backlog_status"]
                for item in state["backlog"]["items"]
            }

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(statuses["TASK-001"], "done")
            self.assertEqual(statuses["TASK-002"], "done")
            self.assertTrue((output_dir / "steps" / "STEP-0001-TASK-001").exists())
            self.assertTrue((output_dir / "steps" / "STEP-0002-TASK-002").exists())

    def test_file_daemon_tick_records_worker_registry_and_processes_one_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            daemon = FileSchedulerDaemon(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            summary = daemon.tick()

            registry = json.loads(
                (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["daemon_status"], "running")
            self.assertEqual(summary["tick_status"], "processed")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(
                summary["worker_registry_path"],
                str(output_dir / "state" / "worker_registry.json"),
            )
            self.assertEqual(registry["tick_count"], 1)
            self.assertEqual(registry["registry_status"], "active")
            self.assertEqual(
                [worker["agent_id"] for worker in registry["workers"]],
                ["agent-repo-map", "agent-worker-1"],
            )
            self.assertEqual(
                {worker["worker_status"] for worker in registry["workers"]},
                {"idle"},
            )
            self.assertTrue((output_dir / "steps" / "STEP-0001-TASK-001").exists())

    def test_file_daemon_run_until_idle_reuses_worker_registry_across_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_file_daemon(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            registry = json.loads(
                (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(summary["tick_count"], 3)
            self.assertEqual(registry["tick_count"], 3)
            self.assertEqual(registry["registry_status"], "active")

    def test_file_mailbox_worker_poll_once_writes_runtime_result_to_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=FakeRuntimeAdapter(),
                clock=FixedClock(),
            )
            summary = worker.poll_once()

            result_message = _read_first_jsonl(outbox)

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["source_message_id"], "MSG-MAILBOX-001")
            self.assertEqual(result_message["message_type"], "runtime_result")
            self.assertEqual(
                result_message["payload"]["source_message_id"],
                "MSG-MAILBOX-001",
            )
            self.assertEqual(result_message["payload"]["result_status"], "completed")
            self.assertEqual(
                result_message["payload"]["changed_files"],
                ["generated/m0_generated_repo_index.json"],
            )

    def test_file_mailbox_worker_cli_processes_one_message_in_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-SUBPROCESS-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.mailbox_worker",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--output-dir",
                    str(output_dir),
                    "--agent-id",
                    "agent-repo-map",
                    "--message-id",
                    "MSG-SUBPROCESS-001",
                    "--runtime",
                    "fake",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_message = _read_first_jsonl(outbox)

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["source_message_id"], "MSG-SUBPROCESS-001")
            self.assertNotEqual(summary["worker_pid"], os.getpid())
            self.assertEqual(completed.stderr, "")
            self.assertEqual(result_message["message_type"], "runtime_result")

    def test_file_mailbox_worker_cli_can_use_codex_delegate_from_payload_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_mailbox.py"
            target_file = "generated/mailbox_codex_delegate.json"
            _init_git_repo(repo)
            _write_fake_codex(fake_codex, changed_file=target_file)
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-CODEX-MAILBOX-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            message["payload"]["worktree_path"] = str(repo)
            _append_test_jsonl(inbox, [message])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.mailbox_worker",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--output-dir",
                    str(output_dir),
                    "--agent-id",
                    "agent-repo-map",
                    "--message-id",
                    "MSG-CODEX-MAILBOX-001",
                    "--runtime",
                    "codex",
                    "--codex-command-json",
                    json.dumps([sys.executable, str(fake_codex)]),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_message = _read_first_jsonl(outbox)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["result_status"], "completed")
            self.assertTrue((repo / target_file).exists())
            self.assertEqual(result_message["payload"]["output"]["adapter"], "codex")

    def test_scheduler_loop_can_round_trip_runtime_result_through_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FileMailboxRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    runtime_adapter=FakeRuntimeAdapter(),
                    clock=FixedClock(),
                ),
            )

            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())
            self.assertEqual(_read_first_jsonl(first_outbox)["message_type"], "runtime_result")
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxRuntimeAdapter"},
            )

    def test_scheduler_loop_can_run_mailbox_worker_as_one_shot_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FileMailboxSubprocessRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    timeout_seconds=30,
                ),
            )
            state = read_scheduler_state_index(output_dir)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxSubprocessRuntimeAdapter"},
            )

    def test_scheduler_loop_can_use_long_running_mailbox_worker_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            supervisor = FileMailboxWorkerProcessSupervisor(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                env=env,
                poll_interval_seconds=0.01,
            )

            start = supervisor.start()
            try:
                summary = run_scheduler_loop(
                    FIXTURES / "sample_agent_pool.json",
                    backlog_path,
                    output_dir,
                    clock=FixedClock(),
                    runtime_adapter=FileMailboxExternalRuntimeAdapter(
                        FIXTURES / "sample_agent_pool.json",
                        timeout_seconds=5,
                        poll_interval_seconds=0.01,
                    ),
                )
                self.assertIsNone(supervisor.process.poll())
            finally:
                stop = supervisor.stop()

            state = read_scheduler_state_index(output_dir)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            second_outbox = (
                output_dir
                / "steps"
                / "STEP-0002-TASK-002"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(start["worker_status"], "running")
            self.assertEqual(stop["worker_status"], "stopped")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertNotEqual(start["worker_pid"], os.getpid())
            self.assertTrue(first_outbox.exists())
            self.assertTrue(second_outbox.exists())
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxExternalRuntimeAdapter"},
            )

    def test_file_mailbox_worker_process_supervisor_reports_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            supervisor = FileMailboxWorkerProcessSupervisor(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
                env=env,
                poll_interval_seconds=0.01,
            )

            before = supervisor.health()
            start = supervisor.start()
            try:
                running = supervisor.health()
            finally:
                stop = supervisor.stop()
            stopped = supervisor.health()

            self.assertEqual(before["worker_status"], "not_started")
            self.assertEqual(running["worker_status"], "running")
            self.assertEqual(running["worker_pid"], start["worker_pid"])
            self.assertEqual(running["exit_code"], None)
            self.assertEqual(stop["worker_status"], "stopped")
            self.assertEqual(stopped["worker_status"], "exited")
            self.assertEqual(stopped["exit_code"], 0)

    def test_scheduler_loop_writes_canonical_event_log_for_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            events_path = Path(summary["events_path"])
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            event_schema = json.loads((SCHEMAS / "event.schema.json").read_text(encoding="utf-8"))
            allowed_event_keys = set(event_schema["properties"].keys())
            snapshot = replay_events(events_path)

            self.assertEqual(events_path, output_dir / "events.jsonl")
            self.assertTrue(all(set(event.keys()).issubset(allowed_event_keys) for event in events))
            self.assertEqual(
                [event["sequence"] for event in events],
                list(range(1, len(events) + 1)),
            )
            self.assertEqual(events[0]["event_id"], "EVT-0001")
            self.assertEqual(
                {event["step_id"] for event in events},
                {"STEP-0001-TASK-001", "STEP-0002-TASK-002"},
            )
            self.assertTrue(
                all(event["source_event_id"].startswith("EVT-") for event in events)
            )
            self.assertEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(snapshot["tasks"]["TASK-002"]["task_status"], "done")

    def test_scheduler_loop_uses_task_scoped_lease_and_message_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            snapshot = replay_events(summary["events_path"])
            first_message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            second_message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0002-TASK-002"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )

            self.assertEqual(
                set(snapshot["leases"].keys()),
                {"TASK-001-LEASE-001", "TASK-002-LEASE-001"},
            )
            self.assertEqual(first_message["message_id"], "TASK-001-MSG-0001")
            self.assertEqual(first_message["payload"]["lease_id"], "TASK-001-LEASE-001")
            self.assertEqual(second_message["message_id"], "TASK-002-MSG-0001")
            self.assertEqual(second_message["payload"]["lease_id"], "TASK-002-LEASE-001")

    def test_scheduler_loop_writes_sqlite_state_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            db_path = Path(summary["state_db_path"])
            root_event_count = len(
                Path(summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            with sqlite3.connect(db_path) as connection:
                tasks = connection.execute(
                    "select task_id, task_status from tasks order by task_id"
                ).fetchall()
                attempts = connection.execute(
                    "select attempt_id, task_id, attempt_status from attempts order by attempt_id"
                ).fetchall()
                leases = connection.execute(
                    "select lease_id, lease_status from leases order by lease_id"
                ).fetchall()
                runtime_sessions = connection.execute(
                    """
                    select runtime_session_id, task_id, attempt_id, session_status, result_status
                    from runtime_sessions
                    order by runtime_session_id
                    """
                ).fetchall()
                event_count = connection.execute("select count(*) from events").fetchone()[0]

            self.assertTrue(db_path.exists())
            self.assertEqual(tasks, [("TASK-001", "done"), ("TASK-002", "done")])
            self.assertEqual(
                attempts,
                [
                    ("TASK-001-ATTEMPT-001", "TASK-001", "completed"),
                    ("TASK-002-ATTEMPT-001", "TASK-002", "completed"),
                ],
            )
            self.assertEqual(
                leases,
                [
                    ("TASK-001-LEASE-001", "released"),
                    ("TASK-002-LEASE-001", "released"),
                ],
            )
            self.assertEqual(
                runtime_sessions,
                [
                    (
                        "SESSION-TASK-001-ATTEMPT-001",
                        "TASK-001",
                        "TASK-001-ATTEMPT-001",
                        "stopped",
                        "completed",
                    ),
                    (
                        "SESSION-TASK-002-ATTEMPT-001",
                        "TASK-002",
                        "TASK-002-ATTEMPT-001",
                        "stopped",
                        "completed",
                    ),
                ],
            )
            self.assertEqual(event_count, root_event_count)

    def test_read_scheduler_state_index_returns_query_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            state = read_scheduler_state_index(output_dir)
            root_event_count = len(
                Path(summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            self.assertEqual(state["state_db_path"], summary["state_db_path"])
            self.assertEqual(state["events_path"], summary["events_path"])
            self.assertEqual(
                state["tasks"],
                [
                    {"task_id": "TASK-001", "task_status": "done"},
                    {"task_id": "TASK-002", "task_status": "done"},
                ],
            )
            self.assertEqual(
                state["attempts"],
                [
                    {
                        "attempt_id": "TASK-001-ATTEMPT-001",
                        "attempt_status": "completed",
                        "task_id": "TASK-001",
                        "validation_status": "accepted",
                    },
                    {
                        "attempt_id": "TASK-002-ATTEMPT-001",
                        "attempt_status": "completed",
                        "task_id": "TASK-002",
                        "validation_status": "accepted",
                    },
                ],
            )
            self.assertEqual(
                state["runtime_sessions"],
                [
                    {
                        "attempt_id": "TASK-001-ATTEMPT-001",
                        "changed_file_count": 1,
                        "lease_id": "TASK-001-LEASE-001",
                        "result_status": "completed",
                        "runtime_adapter": "FakeRuntimeAdapter",
                        "runtime_model": None,
                        "runtime_sandbox": None,
                        "runtime_session_id": "SESSION-TASK-001-ATTEMPT-001",
                        "runtime_timeout_seconds": None,
                        "session_status": "stopped",
                        "task_id": "TASK-001",
                    },
                    {
                        "attempt_id": "TASK-002-ATTEMPT-001",
                        "changed_file_count": 1,
                        "lease_id": "TASK-002-LEASE-001",
                        "result_status": "completed",
                        "runtime_adapter": "FakeRuntimeAdapter",
                        "runtime_model": None,
                        "runtime_sandbox": None,
                        "runtime_session_id": "SESSION-TASK-002-ATTEMPT-001",
                        "runtime_timeout_seconds": None,
                        "session_status": "stopped",
                        "task_id": "TASK-002",
                    },
                ],
            )
            self.assertEqual(state["event_count"], root_event_count)
            self.assertEqual(state["latest_event"]["event_type"], "backlog_updated")
            self.assertEqual(state["latest_event"]["task_id"], "TASK-002")

    def test_scheduler_state_index_records_runtime_session_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_runtime_config.py"
            _init_git_repo(repo)
            _write_fake_codex_arg_recorder(
                fake_codex,
                changed_file="generated/runtime_config.json",
            )
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(
                    command=[sys.executable, str(fake_codex)],
                    model="gpt-runtime-config",
                    sandbox="read-only",
                    timeout_seconds=30,
                ),
            )

            state = read_scheduler_state_index(output_dir)
            session = state["runtime_sessions"][0]

            self.assertEqual(session["runtime_adapter"], "CodexRuntimeAdapter")
            self.assertEqual(session["runtime_model"], "gpt-runtime-config")
            self.assertEqual(session["runtime_sandbox"], "read-only")
            self.assertEqual(session["runtime_timeout_seconds"], 30)

    def test_scheduler_core_uses_agent_runtime_profile_without_cli_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_core_profile.py"
            target_file = "generated/core_profile.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_runtime_profile(
                agent_pool_path,
                runtime_profile={
                    "adapter": "codex",
                    "command": [sys.executable, str(fake_codex)],
                    "model": "core-profile-model",
                    "sandbox": "read-only",
                    "timeout_seconds": 30,
                },
            )
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)

            run_scheduler_loop(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
            )

            state = read_scheduler_state_index(output_dir)
            session = state["runtime_sessions"][0]
            recorded = json.loads(
                (
                    output_dir
                    / "steps"
                    / "STEP-0001-TASK-001"
                    / "worktrees"
                    / "WT-TASK-001-ATTEMPT-001"
                    / target_file
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(session["runtime_adapter"], "CodexRuntimeAdapter")
            self.assertEqual(session["runtime_model"], "core-profile-model")
            self.assertEqual(session["runtime_sandbox"], "read-only")
            self.assertEqual(session["runtime_timeout_seconds"], 30)
            self.assertEqual(recorded["model"], "core-profile-model")
            self.assertEqual(recorded["sandbox"], "read-only")

    def test_read_scheduler_state_index_rebuilds_stale_sqlite_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            root_event_count = len(
                Path(summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            with sqlite3.connect(summary["state_db_path"]) as connection:
                connection.execute("delete from tasks where task_id = ?", ("TASK-002",))
                connection.execute(
                    "delete from events where sequence = (select max(sequence) from events)"
                )

            state = read_scheduler_state_index(output_dir)

            self.assertEqual(
                state["tasks"],
                [
                    {"task_id": "TASK-001", "task_status": "done"},
                    {"task_id": "TASK-002", "task_status": "done"},
                ],
            )
            self.assertEqual(state["event_count"], root_event_count)
            self.assertEqual(state["latest_event"]["task_id"], "TASK-002")

    def test_read_scheduler_state_index_rebuilds_missing_runtime_session_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            db_path = output_dir / "state" / "scheduler_state.sqlite"
            with sqlite3.connect(db_path) as connection:
                connection.execute("drop table runtime_sessions")

            state = read_scheduler_state_index(output_dir)

            self.assertEqual(len(state["runtime_sessions"]), 2)
            self.assertEqual(
                state["runtime_sessions"][0]["runtime_session_id"],
                "SESSION-TASK-001-ATTEMPT-001",
            )

    def test_scheduler_loop_respects_done_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task(
                        "TASK-001",
                        write_scope=["generated/task-001/"],
                        status="done",
                    ),
                    _backlog_task(
                        "TASK-002",
                        write_scope=["generated/task-002/"],
                        depends_on=["TASK-001"],
                    ),
                    _backlog_task(
                        "TASK-003",
                        write_scope=["generated/task-003/"],
                        depends_on=["TASK-MISSING"],
                    ),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            state = json.loads(Path(summary["state_path"]).read_text(encoding="utf-8"))
            statuses = {
                item["task_id"]: item["backlog_status"]
                for item in state["backlog"]["items"]
            }

            self.assertEqual(summary["processed_task_ids"], ["TASK-002"])
            self.assertEqual(statuses["TASK-001"], "done")
            self.assertEqual(statuses["TASK-002"], "done")
            self.assertEqual(statuses["TASK-003"], "ready")

    def test_scheduler_loop_resumes_from_persisted_state(self):
        class RecordingRuntimeAdapter:
            def __init__(self):
                self.task_ids = []

            def run(self, message, worktree_path=None):
                self.task_ids.append(message["payload"]["task_id"])
                return {
                    "result_status": "completed",
                    "changed_files": message["payload"]["write_scope"],
                    "output": {"adapter": "recording"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            adapter = RecordingRuntimeAdapter()
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            first_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=adapter,
                max_steps=1,
            )
            second_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=adapter,
            )

            self.assertEqual(first_summary["scheduler_status"], "max_steps_reached")
            self.assertEqual(second_summary["scheduler_status"], "idle")
            self.assertEqual(second_summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(adapter.task_ids, ["TASK-001", "TASK-002"])

    def test_scheduler_loop_uses_task_scoped_worktree_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            branches = [step["result"]["branch"] for step in summary["steps"]]
            worktree_ids = [step["result"]["worktree_id"] for step in summary["steps"]]

            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                branches,
                ["agentteam/TASK-001-ATTEMPT-001", "agentteam/TASK-002-ATTEMPT-001"],
            )
            self.assertEqual(
                worktree_ids,
                ["WT-TASK-001-ATTEMPT-001", "WT-TASK-002-ATTEMPT-001"],
            )

    def test_replay_reconstructs_done_task_only_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "completed")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["validation_status"], "accepted")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["worktree_id"], "WT-ATTEMPT-001")
            self.assertEqual(snapshot["leases"]["LEASE-001"]["lease_status"], "released")

    def test_run_simulation_records_runtime_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )
            events = [
                json.loads(line)
                for line in Path(result["events_path"]).read_text(encoding="utf-8").splitlines()
            ]
            session_events = [
                event
                for event in events
                if event["event_type"].startswith("runtime_session_")
            ]
            snapshot = replay_events(result["events_path"])

            self.assertEqual(result["runtime_session_id"], "SESSION-ATTEMPT-001")
            self.assertEqual(result["runtime_session_status"], "stopped")
            self.assertEqual(
                [event["event_type"] for event in session_events],
                [
                    "runtime_session_started",
                    "runtime_session_observed",
                    "runtime_session_stopped",
                ],
            )
            self.assertTrue(
                all(
                    event["payload"]["runtime_session_id"] == "SESSION-ATTEMPT-001"
                    and event["payload"]["task_id"] == "TASK-001"
                    and event["payload"]["attempt_id"] == "ATTEMPT-001"
                    and event["payload"]["lease_id"] == "LEASE-001"
                    for event in session_events
                )
            )
            self.assertEqual(
                snapshot["runtime_sessions"]["SESSION-ATTEMPT-001"]["session_status"],
                "stopped",
            )
            self.assertEqual(
                snapshot["runtime_sessions"]["SESSION-ATTEMPT-001"]["result_status"],
                "completed",
            )

    def test_emitted_types_are_allowed_by_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )

            event_schema = json.loads((SCHEMAS / "event.schema.json").read_text(encoding="utf-8"))
            allowed_events = set(event_schema["properties"]["event_type"]["enum"])
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue({event["event_type"] for event in events}.issubset(allowed_events))

            message_schema = json.loads(
                (SCHEMAS / "mailbox_message.schema.json").read_text(encoding="utf-8")
            )
            allowed_messages = set(message_schema["properties"]["message_type"]["enum"])
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            messages = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()]
            self.assertTrue({message["message_type"] for message in messages}.issubset(allowed_messages))

    def test_artifact_lint_passes_native_runtime_tree(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        summary = lint_artifacts(ROOT)

        self.assertEqual(summary["status"], "passed")
        self.assertGreaterEqual(summary["checked_json_files"], 1)
        self.assertGreaterEqual(summary["checked_jsonl_files"], 1)
        self.assertEqual(summary["errors"], [])

    def test_artifact_lint_reports_invalid_json(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_path = tmp_path / "broken.json"
            bad_path.write_text("{bad", encoding="utf-8")

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "invalid_json")
            self.assertEqual(summary["errors"][0]["path"], "broken.json")

    def test_artifact_lint_reports_invalid_event_type(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_dir = tmp_path / "schemas"
            schema_dir.mkdir()
            (schema_dir / "event.schema.json").write_text(
                json.dumps(
                    {
                        "properties": {
                            "event_type": {
                                "enum": ["known_event"],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            event = _event_record("EVT-0001", 1)
            event["event_type"] = "unknown_event"
            (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "invalid_event_type")
            self.assertEqual(summary["errors"][0]["event_type"], "unknown_event")

    def test_artifact_lint_reports_missing_event_fields(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "events.jsonl").write_text(
                json.dumps({"event_type": "scheduler_started", "sequence": 1}) + "\n",
                encoding="utf-8",
            )

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "missing_event_fields")
            self.assertIn("event_id", summary["errors"][0]["missing_fields"])

    def test_artifact_lint_reports_non_monotonic_event_sequence(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events = [
                _event_record("EVT-0001", 1),
                _event_record("EVT-0003", 3),
            ]
            (tmp_path / "events.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "non_monotonic_event_sequence")
            self.assertEqual(summary["errors"][0]["expected_sequence"], 2)
            self.assertEqual(summary["errors"][0]["actual_sequence"], 3)

    def test_artifact_lint_cli_prints_summary(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.artifact_lint",
                "--root",
                str(ROOT),
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "passed")
        self.assertGreaterEqual(summary["checked_json_files"], 1)

    def test_cli_runs_simulation_and_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(FIXTURES / "sample_backlog.json"),
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["task_id"], "TASK-001")
            self.assertTrue((output_dir / "events.jsonl").exists())

    def test_cli_can_run_scheduler_loop_until_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--run-until-idle",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = json.loads(Path(summary["state_path"]).read_text(encoding="utf-8"))
            statuses = {
                item["task_id"]: item["backlog_status"]
                for item in state["backlog"]["items"]
            }

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(statuses["TASK-001"], "done")
            self.assertEqual(statuses["TASK-002"], "done")
            self.assertEqual(summary["snapshot"]["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(summary["snapshot"]["tasks"]["TASK-002"]["task_status"], "done")
            self.assertEqual(
                set(summary["snapshot"]["leases"].keys()),
                {"TASK-001-LEASE-001", "TASK-002-LEASE-001"},
            )

    def test_cli_can_run_file_daemon_until_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue((output_dir / "state" / "worker_registry.json").exists())

    def test_cli_can_run_file_daemon_with_mailbox_worker_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-mailbox-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())

    def test_cli_can_run_file_daemon_with_mailbox_subprocess_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-mailbox-subprocess-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxSubprocessRuntimeAdapter"},
            )

    def test_cli_can_run_file_daemon_with_long_running_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
            self.assertEqual(summary["worker_process"]["stderr"], "")
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxExternalRuntimeAdapter"},
            )

    def test_cli_can_run_file_daemon_with_long_running_codex_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_long_worker.py"
            target_file = "generated/long_worker_codex_delegate.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                    "--runtime",
                    "codex",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            worktree_path = Path(
                summary["snapshot"]["attempts"]["TASK-001-ATTEMPT-001"]["worktree_path"]
            )

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
            self.assertEqual(summary["worker_process"]["worker_runtime"], "codex")
            self.assertEqual(summary["worker_process"]["stderr"], "")
            self.assertTrue((worktree_path / target_file).exists())
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-TASK-001-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "FileMailboxExternalRuntimeAdapter",
            )

    def test_cli_long_running_mailbox_worker_accepts_agent_id_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "custom_agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_id(agent_pool_path, "agent-custom-map")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                    "--daemon-long-running-worker-agent-id",
                    "agent-custom-map",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            custom_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-custom-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_process"]["worker_agent_id"], "agent-custom-map")
            self.assertTrue(custom_outbox.exists())

    def test_file_mailbox_worker_pool_supervisor_starts_and_stops_all_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            try:
                self.assertEqual(start["pool_status"], "running")
                self.assertEqual(start["worker_count"], 2)
                self.assertEqual(
                    {worker["worker_agent_id"] for worker in start["workers"]},
                    {"agent-repo-map", "agent-doc-map"},
                )
                self.assertTrue(Path(start["process_registry_path"]).exists())
                self.assertTrue(
                    all(worker["worker_pid"] != os.getpid() for worker in start["workers"])
                )
            finally:
                stop = pool.stop()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(stop["pool_status"], "stopped")
            self.assertEqual(stop["worker_count"], 2)
            self.assertEqual(registry["registry_status"], "stopped")
            self.assertEqual(
                {worker["worker_agent_id"] for worker in stop["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(
                all(worker["worker_status"] == "stopped" for worker in stop["workers"])
            )

    def test_file_mailbox_worker_pool_supervisor_restarts_exited_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            first_pid = start["workers"][0]["worker_pid"]
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            degraded = pool.health_check()
            restarted = pool.restart_exited_workers()
            try:
                recovered = pool.health_check()
            finally:
                stop = pool.stop()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(degraded["pool_status"], "degraded")
            self.assertEqual(degraded["workers"][0]["worker_status"], "exited")
            self.assertEqual(restarted["restarted_count"], 1)
            self.assertEqual(restarted["workers"][0]["restart_status"], "restarted")
            self.assertNotEqual(
                restarted["workers"][0]["new_worker"]["worker_pid"],
                first_pid,
            )
            self.assertEqual(recovered["pool_status"], "running")
            self.assertEqual(recovered["workers"][0]["worker_status"], "running")
            self.assertEqual(registry["workers"][0]["restart_count"], 1)

    def test_two_phase_scheduler_does_not_double_book_same_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_inflight=2,
            )

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_status"], "dispatched")
            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(dispatch["inflight_count"], 1)

    def test_two_phase_scheduler_retries_retryable_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=2,
            )

            first_dispatch = scheduler.dispatch_ready()
            first_inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                first_inflight["outbox_path"],
                first_inflight["message_id"],
                first_inflight["task_id"],
                first_inflight["attempt_id"],
                first_inflight["lease_id"],
                "failed",
                [],
            )

            first_collect = scheduler.collect_ready_results()
            second_dispatch = scheduler.dispatch_ready()
            second_inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                second_inflight["outbox_path"],
                second_inflight["message_id"],
                second_inflight["task_id"],
                second_inflight["attempt_id"],
                second_inflight["lease_id"],
                "completed",
                ["generated/retry.json"],
            )
            second_collect = scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(first_dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(first_collect["collected_task_ids"], ["TASK-001"])
            self.assertEqual(second_dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(second_inflight["attempt_id"], "TASK-001-ATTEMPT-002")
            self.assertEqual(second_collect["collected_task_ids"], ["TASK-001"])
            self.assertIn("recovery_routed", {event["event_type"] for event in events})
            self.assertEqual(
                [step["step_status"] for step in scheduler.state["steps"]],
                ["retry_routed", "processed"],
            )
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done"},
            )

    def test_two_phase_scheduler_dispatches_planner_task_when_auto_decompose_is_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M22",
            )

            dispatch = scheduler.dispatch_ready()
            planner_task = scheduler.state["backlog"]["items"][0]
            context_path = Path(planner_task["planner_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))
            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-DECOMPOSE-M22-001"
                / "mailboxes"
                / "agent-planner"
                / "inbox.jsonl"
            )

            self.assertEqual(dispatch["dispatch_status"], "dispatched")
            self.assertEqual(dispatch["dispatched_task_ids"], ["DECOMPOSE-M22-001"])
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["task_kind"],
                "decompose_backlog",
            )
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["required_role"],
                "task_planner",
            )
            self.assertTrue(context_path.exists())
            self.assertEqual(context["milestone_id"], "M22")
            self.assertEqual(context["allowed_write_scopes"], ["generated/"])
            self.assertEqual(message["payload"]["planner_context_path"], str(context_path))

    def test_two_phase_scheduler_applies_planner_task_proposal_to_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M21",
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M21-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M21",
                        "tasks": [
                            {
                                "task_id": "TASK-M21-001",
                                "objective": "Run generated worker task.",
                                "read_scope": ["."],
                                "write_scope": ["generated/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            collected = scheduler.collect_ready_results()

            self.assertEqual(
                collected["results"][0]["decomposition_status"],
                "applied",
            )
            self.assertEqual(
                collected["results"][0]["generated_task_ids"],
                ["TASK-M21-001"],
            )
            self.assertEqual(
                [item["task_id"] for item in scheduler.state["backlog"]["items"]],
                ["DECOMPOSE-M21-001", "TASK-M21-001"],
            )

    def test_two_phase_scheduler_rejects_planner_proposal_outside_context_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M22",
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M22-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M22",
                        "tasks": [
                            {
                                "task_id": "TASK-M22-001",
                                "objective": "Try to write outside context allowance.",
                                "read_scope": ["."],
                                "write_scope": ["src/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            collected = scheduler.collect_ready_results()

            self.assertEqual(
                collected["results"][0]["decomposition_status"],
                "rejected",
            )
            self.assertEqual(
                collected["results"][0]["failure_category"],
                "invalid_task_proposal",
            )
            self.assertEqual(len(scheduler.state["backlog"]["items"]), 1)

    def test_two_phase_scheduler_collects_expired_inflight_as_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                lease_timeout_seconds=0,
            )

            scheduler.dispatch_ready()
            collected = scheduler.collect_ready_results()
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(collected["collect_status"], "collected")
            self.assertEqual(collected["collected_task_ids"], ["TASK-001"])
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(scheduler.state["steps"][0]["failure_category"], "timeout")
            self.assertTrue(scheduler.state["steps"][0]["result"]["retryable"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "running"},
            )
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["blockers"],
                ["timeout"],
            )

    def test_two_phase_scheduler_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/two_phase_commit.json').exists()",
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "two_phase_commit.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"attempt_id": inflight["attempt_id"]}),
                encoding="utf-8",
            )
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/two_phase_commit.json"],
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["diff_audit"]["diff_status"], "matched")
            self.assertTrue(Path(result["patch_path"]).exists())
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertNotEqual(result["integration_commit_sha"], None)
            self.assertTrue(
                (integration_worktree / "generated" / "two_phase_commit.json").exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(
                snapshot["attempts"]["TASK-001-ATTEMPT-001"][
                    "integration_commit_status"
                ],
                "committed",
            )

    def test_two_phase_scheduler_dispatches_multiple_tasks_before_collecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task(
                        "TASK-002",
                        write_scope=["generated/task-002/"],
                        required_role="aux_role_1",
                    ),
                ],
            )
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_inflight=2,
            )

            pool.start()
            try:
                dispatch = scheduler.dispatch_ready()
                self.assertEqual(dispatch["dispatch_status"], "dispatched")
                self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001", "TASK-002"])
                self.assertEqual(dispatch["inflight_count"], 2)
                self.assertEqual(scheduler.summary()["processed_task_ids"], [])

                collected = None
                for _ in range(50):
                    collected = scheduler.collect_ready_results()
                    if collected["collected_count"] == 2:
                        break
                    time.sleep(0.02)
            finally:
                pool.stop()

            state = read_scheduler_state_index(output_dir)
            self.assertEqual(collected["collected_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                scheduler.summary()["processed_task_ids"],
                ["TASK-001", "TASK-002"],
            )
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done", "TASK-002": "done"},
            )

    def test_cli_can_run_file_daemon_with_static_long_running_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-worker-pool",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            process_registry = json.loads(
                Path(summary["worker_pool"]["process_registry_path"]).read_text(
                    encoding="utf-8"
                )
            )
            repo_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
            self.assertEqual(summary["worker_pool"]["worker_count"], 2)
            self.assertEqual(process_registry["registry_status"], "stopped")
            self.assertEqual(
                {worker["worker_agent_id"] for worker in summary["worker_pool"]["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(repo_outbox.exists())

    def test_cli_can_run_two_phase_scheduler_with_static_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task(
                        "TASK-002",
                        write_scope=["generated/task-002/"],
                        required_role="aux_role_1",
                    ),
                ],
            )
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--max-inflight",
                    "2",
                    "--max-attempts",
                    "2",
                    "--lease-timeout-seconds",
                    "900",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["inflight_count"], 0)
            self.assertEqual(summary["max_attempts"], 2)
            self.assertEqual(summary["lease_timeout_seconds"], 900)
            self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
            self.assertEqual(summary["worker_pool"]["worker_count"], 2)
            self.assertEqual(summary["worker_pool_health"]["pool_status"], "running")
            self.assertGreaterEqual(len(summary["worker_pool_supervision"]), 1)
            self.assertIn("restart_count", summary["worker_pool_health"]["workers"][0])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done", "TASK-002": "done"},
            )

    def test_cli_two_phase_worker_pool_can_auto_decompose_with_fake_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M21",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--max-steps",
                    "10",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertIn("DECOMPOSE-M21-001", summary["processed_task_ids"])
            self.assertIn("TASK-M21-GENERATED-001", summary["processed_task_ids"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {
                    "DECOMPOSE-M21-001": "done",
                    "TASK-M21-GENERATED-001": "done",
                },
            )

    def test_cli_two_phase_worker_pool_can_auto_decompose_with_fake_codex_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            fake_codex = tmp_path / "fake_codex_planner_and_worker.py"
            _init_git_repo(repo)
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            _write_fake_codex_planner_and_worker(fake_codex)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M23",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--runtime",
                    "codex",
                    "--max-steps",
                    "10",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertIn("DECOMPOSE-M23-001", summary["processed_task_ids"])
            self.assertIn("TASK-M23-CODEX-001", summary["processed_task_ids"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {
                    "DECOMPOSE-M23-001": "done",
                    "TASK-M23-CODEX-001": "done",
                },
            )
            self.assertEqual(_git_status_short(repo), "")

    def test_cli_two_phase_worker_pool_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--max-inflight",
                    "1",
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib; assert pathlib.Path('generated/m0_generated_repo_index.json').exists()",
                        ]
                    ),
                    "--commit-verified-integration",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = summary["steps"][0]["result"]
            integration_worktree = Path(result["integration_worktree_path"])

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertTrue(
                (
                    integration_worktree
                    / "generated"
                    / "m0_generated_repo_index.json"
                ).exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)

    def test_cli_can_show_state_index_without_agent_pool_or_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            run_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            state_db_path = output_dir / "state" / "scheduler_state.sqlite"
            state_db_path.unlink()
            root_event_count = len(
                Path(run_summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--output-dir",
                    str(output_dir),
                    "--show-state-index",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["event_count"], root_event_count)
            self.assertEqual(summary["tasks"][0]["task_id"], "TASK-001")
            self.assertEqual(summary["tasks"][1]["task_status"], "done")
            self.assertTrue(state_db_path.exists())

    def test_cli_can_create_git_worktree_when_project_root_is_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(Path(summary["worktree_path"]).exists())
            self.assertTrue((Path(summary["worktree_path"]) / "generated").is_dir())

    def test_cli_can_run_shell_runtime_adapter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_shell_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "cli_shell_result.json").exists()
            )

    def test_cli_can_apply_accepted_patch_to_integration_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_integration_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_integration_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--integrate-accepted-patch",
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            integration_worktree = Path(summary["integration_worktree_path"])

            self.assertEqual(summary["integration_status"], "applied")
            self.assertTrue(
                (integration_worktree / "generated" / "cli_integration_result.json").exists()
            )

    def test_cli_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_commit_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_commit_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib; assert pathlib.Path('generated/cli_commit_result.json').exists()",
                        ]
                    ),
                    "--commit-verified-integration",
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            integration_worktree = Path(summary["integration_worktree_path"])

            self.assertEqual(summary["integration_verification_status"], "passed")
            self.assertEqual(summary["integration_commit_status"], "committed")
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(integration_worktree), "")

    def test_project_root_creates_real_git_worktree_for_writable_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertTrue(worktree_path.exists())
            completed = subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--is-inside-work-tree"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(completed.stdout.strip(), "true")
            self.assertTrue((worktree_path / "generated" / "m0_generated_repo_index.json").exists())

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["worktree_path"],
                str(worktree_path),
            )

    def test_out_of_scope_runtime_result_is_rejected(self):
        class OutOfScopeRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["outside/generated.txt"],
                    "output": {"note": "intentionally outside write scope"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                tmp_path / "run",
                clock=FixedClock(),
                runtime_adapter=OutOfScopeRuntimeAdapter(),
            )

            self.assertEqual(result["validation_status"], "rejected")
            snapshot = replay_events(tmp_path / "run" / "events.jsonl")
            self.assertNotEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["validation_status"],
                "rejected",
            )

    def test_attempt_outcome_classifies_scope_violation_as_non_retryable(self):
        task = {"write_scope": ["generated/"]}
        result = {
            "result_status": "completed",
            "changed_files": ["outside.txt"],
            "output": {},
        }

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(outcome["failure_category"], "scope_violation")
        self.assertFalse(outcome["retryable"])

    def test_attempt_outcome_classifies_timeout_as_retryable(self):
        task = {"write_scope": ["generated/"]}
        result = {"result_status": "timed_out", "changed_files": [], "output": {}}

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(outcome["failure_category"], "timeout")
        self.assertTrue(outcome["retryable"])

    def test_worktree_diff_audit_detects_declared_file_missing_from_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)

            audit = audit_worktree_diff(repo, ["generated/missing.json"])

            self.assertEqual(audit["diff_status"], "mismatch")
            self.assertEqual(audit["declared_changed_files"], ["generated/missing.json"])
            self.assertEqual(audit["actual_changed_files"], [])
            self.assertEqual(audit["missing_declared_files"], ["generated/missing.json"])
            self.assertEqual(audit["undeclared_changed_files"], [])

    def test_worktree_diff_audit_matches_declared_file_in_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)
            target = repo / "generated" / "actual.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"created": True}), encoding="utf-8")

            audit = audit_worktree_diff(repo, ["generated/actual.json"])

            self.assertEqual(audit["diff_status"], "matched")
            self.assertEqual(audit["declared_changed_files"], ["generated/actual.json"])
            self.assertEqual(audit["actual_changed_files"], ["generated/actual.json"])
            self.assertEqual(audit["missing_declared_files"], [])
            self.assertEqual(audit["undeclared_changed_files"], [])

    def test_shell_runtime_adapter_executes_command_in_worktree_and_parses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/shell_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue((worktree_path / "generated" / "shell_result.json").exists())

    def test_worktree_attempt_writes_patch_artifact_for_actual_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "patch_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/patch_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            patch_path = Path(result["patch_path"])

            self.assertTrue(patch_path.exists())
            self.assertEqual(result["attempts"][0]["patch_path"], str(patch_path))
            self.assertIn("generated/patch_result.json", patch_path.read_text(encoding="utf-8"))

    def test_accepted_patch_applies_to_integration_worktree_without_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "integration_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_branch"], "agentteam/integration/TASK-001")
            self.assertTrue(
                (integration_worktree / "generated" / "integration_result.json").exists()
            )
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_status"],
                "applied",
            )

    def test_integration_verification_command_passes_in_integration_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "verify_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/integration_result.json').exists()",
                ],
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_verification_exit_code"], 0)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_verification_status"],
                "passed",
            )

    def test_integration_verification_command_failure_is_recorded_without_rejecting_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "verify_fail_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(result["integration_verification_exit_code"], 7)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_verification_status"],
                "failed",
            )

    def test_verified_integration_patch_can_be_committed_to_integration_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/commit_result.json').exists()",
                ],
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertIsNotNone(result["integration_commit_sha"])
            self.assertEqual(result["integration_commit_reason"], None)
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(integration_worktree), "")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_commit_status"],
                "committed",
            )

    def test_integration_commit_is_skipped_when_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_skip_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_skip_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(result["integration_commit_reason"], "verification_failed")
            self.assertEqual(result["integration_commit_sha"], None)
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertNotEqual(_git_status_short(integration_worktree), "")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_commit_status"],
                "skipped",
            )

    def test_integration_commit_is_skipped_without_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_no_verify_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_no_verify_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])

            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(result["integration_commit_reason"], "verification_not_requested")
            self.assertEqual(result["integration_commit_sha"], None)
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)

    def test_shell_runtime_adapter_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "fail_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            script.write_text(
                "import sys\nsys.stderr.write('worker failed intentionally')\nsys.exit(17)\n",
                encoding="utf-8",
            )

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "failed")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["validation_status"], "rejected")

    def test_retryable_runtime_failure_can_be_retried_and_accepted(self):
        class RetryOnceRuntimeAdapter:
            def __init__(self):
                self.attempt_ids = []

            def run(self, message, worktree_path=None):
                self.attempt_ids.append(message["payload"]["attempt_id"])
                if len(self.attempt_ids) == 1:
                    return {
                        "result_status": "failed",
                        "changed_files": [],
                        "output": {"error": "transient"},
                    }
                target = Path(worktree_path) / "generated" / "retry_result.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"attempt_id": message["payload"]["attempt_id"]}),
                    encoding="utf-8",
                )
                return {
                    "result_status": "completed",
                    "changed_files": ["generated/retry_result.json"],
                    "output": {"adapter": "retry-once"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            adapter = RetryOnceRuntimeAdapter()
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=adapter,
                max_attempts=2,
            )

            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(adapter.attempt_ids, ["ATTEMPT-001", "ATTEMPT-002"])
            self.assertEqual(result["attempt_id"], "ATTEMPT-002")
            self.assertEqual(result["attempt_count"], 2)
            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["failure_category"], None)
            self.assertEqual(result["attempts"][0]["failure_category"], "runtime_error")
            self.assertIn("recovery_routed", {event["event_type"] for event in events})
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["validation_status"],
                "rejected",
            )
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-002"]["validation_status"],
                "accepted",
            )
            self.assertTrue((Path(result["worktree_path"]) / "generated" / "retry_result.json").exists())

    def test_declared_changed_file_without_worktree_diff_is_rejected(self):
        class PhantomRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["generated/phantom.json"],
                    "output": {"adapter": "phantom"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=PhantomRuntimeAdapter(),
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(result["failure_category"], "diff_mismatch")
            self.assertEqual(
                result["diff_audit"]["missing_declared_files"],
                ["generated/phantom.json"],
            )
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["failure_category"],
                "diff_mismatch",
            )

    def test_accepted_attempt_can_remove_git_worktree_when_cleanup_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
                cleanup_accepted_worktrees=True,
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue(result["worktree_removed"])
            self.assertFalse(Path(result["worktree_path"]).exists())
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["worktree_status"],
                "removed",
            )

    def test_codex_runtime_adapter_reads_last_message_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue((worktree_path / "generated" / "codex_result.json").exists())

    def test_codex_runtime_adapter_builds_planner_prompt_contract(self):
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-planner",
            "message_type": "dispatch_task",
            "correlation_id": "DECOMPOSE-M23-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "DECOMPOSE-M23-001",
                "attempt_id": "DECOMPOSE-M23-001-ATTEMPT-001",
                "lease_id": "DECOMPOSE-M23-001-LEASE-001",
                "task_kind": "decompose_backlog",
                "milestone_id": "M23",
                "planner_context_path": (
                    "/tmp/planner_contexts/DECOMPOSE-M23-001.json"
                ),
                "objective": "Generate bounded backlog tasks.",
                "read_scope": ["."],
                "write_scope": [],
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("AgentTeam planner", prompt)
        self.assertIn("task_proposal", prompt)
        self.assertIn("DECOMPOSE-M23-001.json", prompt)
        self.assertIn('"changed_files": []', prompt)
        self.assertIn('"required_role"', prompt)

    def test_codex_runtime_adapter_runs_planner_with_fallback_worktree_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_planner.py"
            _init_git_repo(repo)
            _write_fake_codex_planner(fake_codex)

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                fallback_worktree_path=repo,
            ).run(_planner_message(tmp_path), worktree_path=None)

            self.assertEqual(result["result_status"], "completed")
            self.assertEqual(result["changed_files"], [])
            self.assertEqual(
                result["output"]["task_proposal"]["tasks"][0]["task_id"],
                "TASK-M23-CODEX-001",
            )
            self.assertEqual(_git_status_short(repo), "")

    def test_codex_runtime_adapter_rejects_dirty_fallback_worktree_after_planner_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_dirty_planner.py"
            _init_git_repo(repo)
            _write_fake_codex_planner(fake_codex, dirty_file="generated/dirty.json")

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                fallback_worktree_path=repo,
            ).run(_planner_message(tmp_path), worktree_path=None)

            self.assertEqual(result["result_status"], "failed")
            self.assertEqual(
                result["output"]["error"],
                "fallback_worktree_modified",
            )
            self.assertIn("generated/dirty.json", result["output"]["changed_files"])

    def test_codex_runtime_adapter_missing_last_message_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_no_result.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            fake_codex.write_text("import sys\nsys.stdin.read()\nprint('no result file')\n", encoding="utf-8")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "failed")

    def test_codex_runtime_adapter_default_exec_flags_match_current_cli(self):
        command = CodexRuntimeAdapter(command=["codex", "exec"])._build_command(
            "/tmp/worktree",
            "/tmp/result.json",
        )

        self.assertIn("-C", command)
        self.assertIn("-s", command)
        self.assertIn("--output-last-message", command)
        self.assertNotIn("-a", command)
        self.assertNotIn("--ask-for-approval", command)

    def test_cli_can_run_codex_runtime_adapter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_cli.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_cli_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "codex_cli_result.json").exists()
            )

    def test_cli_can_select_codex_runtime_with_command_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_runtime.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_runtime_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--runtime",
                    "codex",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "CodexRuntimeAdapter",
            )
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "codex_runtime_result.json").exists()
            )

    def test_cli_rejects_codex_runtime_without_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--runtime",
                    "codex",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("--project-root is required when --runtime codex is set", completed.stderr)

    def test_cli_passes_codex_runtime_options_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_options.py"
            target_file = "generated/codex_runtime_options.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--runtime",
                    "codex",
                    "--codex-model",
                    "gpt-test-model",
                    "--codex-sandbox",
                    "read-only",
                    "--codex-timeout-seconds",
                    "30",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            recorded = json.loads(
                (Path(summary["worktree_path"]) / target_file).read_text(encoding="utf-8")
            )

            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(recorded["model"], "gpt-test-model")
            self.assertEqual(recorded["sandbox"], "read-only")

    def test_cli_uses_agent_runtime_profile_for_codex_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_profile.py"
            target_file = "generated/codex_agent_profile.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_runtime_profile(
                agent_pool_path,
                runtime_profile={
                    "adapter": "codex",
                    "model": "agent-profile-model",
                    "sandbox": "read-only",
                    "timeout_seconds": 30,
                },
            )
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            recorded = json.loads(
                (Path(summary["worktree_path"]) / target_file).read_text(encoding="utf-8")
            )

            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "CodexRuntimeAdapter",
            )
            self.assertEqual(recorded["model"], "agent-profile-model")
            self.assertEqual(recorded["sandbox"], "read-only")


def _init_git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "config", "user.email", "agentteam@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "AgentTeam Test"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_rev_parse(repo, ref):
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _git_status_short(repo):
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _planner_message(tmp_path):
    context_path = Path(tmp_path) / "planner_contexts" / "DECOMPOSE-M23-001.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps(
            {
                "context_schema_version": "planner_context.v1",
                "milestone_id": "M23",
                "default_worker_role": "repo_map_agent",
                "allowed_read_scopes": ["."],
                "allowed_write_scopes": ["generated/"],
                "available_agent_roles": ["repo_map_agent", "task_planner"],
                "proposal_contract": {
                    "schema_version": "task_proposal.v1",
                    "required_fields": [
                        "task_id",
                        "objective",
                        "read_scope",
                        "write_scope",
                        "required_role",
                        "risk_target",
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "message_id": "MSG-0001",
        "from_agent": "agent-scheduler",
        "to_agent": "agent-planner",
        "message_type": "dispatch_task",
        "correlation_id": "DECOMPOSE-M23-001:ATTEMPT-001",
        "created_at": "2026-06-03T00:00:00Z",
        "lease_expires_at": "2026-06-03T00:15:00Z",
        "payload": {
            "task_id": "DECOMPOSE-M23-001",
            "attempt_id": "DECOMPOSE-M23-001-ATTEMPT-001",
            "lease_id": "DECOMPOSE-M23-001-LEASE-001",
            "task_kind": "decompose_backlog",
            "milestone_id": "M23",
            "default_worker_role": "repo_map_agent",
            "planner_context_path": str(context_path),
            "objective": "Generate bounded backlog tasks.",
            "read_scope": ["."],
            "write_scope": [],
        },
    }


def _read_first_jsonl(path):
    return json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])


def _append_test_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _mailbox_dispatch_message(message_id, agent_id, write_scope):
    return {
        "message_id": message_id,
        "from_agent": "agent-scheduler",
        "to_agent": agent_id,
        "message_type": "dispatch_task",
        "correlation_id": f"TASK-MAILBOX:{message_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "lease_expires_at": "2026-06-03T00:15:00Z",
        "payload": {
            "task_id": "TASK-MAILBOX",
            "attempt_id": "ATTEMPT-MAILBOX-001",
            "lease_id": "LEASE-MAILBOX-001",
            "worktree_id": "WT-MAILBOX-001",
            "worktree_path": None,
            "branch": None,
            "objective": "Exercise file mailbox worker runtime.",
            "read_scope": ["."],
            "write_scope": write_scope,
        },
    }


def _write_backlog(tmp_path, write_scope, tasks=None):
    backlog = {
        "backlog_id": "BL-TEST",
        "items": [_backlog_task("TASK-001", write_scope=write_scope)] if tasks is None else tasks,
    }
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps(backlog), encoding="utf-8")
    return path


def _write_agent_pool_with_runtime_profile(path, runtime_profile):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-02T00:00:00Z",
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "runtime_profile": runtime_profile,
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool), encoding="utf-8")


def _write_agent_pool_with_agent_id(path, agent_id):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_ids(path, agent_ids):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent" if index == 0 else f"aux_role_{index}",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for index, agent_id in enumerate(agent_ids)
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_roles(path, agent_roles):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": role,
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for agent_id, role in agent_roles
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _append_runtime_result(
    outbox_path,
    source_message_id,
    task_id,
    attempt_id,
    lease_id,
    result_status,
    changed_files,
):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-repo-map",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": {"test": "m18"},
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def _append_runtime_result_with_output(
    outbox_path,
    source_message_id,
    task_id,
    attempt_id,
    lease_id,
    result_status,
    changed_files,
    output,
):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-planner",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": output,
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def _backlog_task(
    task_id,
    write_scope,
    status="ready",
    depends_on=None,
    blockers=None,
    required_role="repo_map_agent",
):
    return {
        "task_id": task_id,
        "milestone_id": "M0",
        "objective": f"Create generated repo index for {task_id}.",
        "backlog_status": status,
        "risk_target": "L0",
        "depends_on": list(depends_on or []),
        "read_scope": ["."],
        "write_scope": write_scope,
        "required_role": required_role,
        "blockers": list(blockers or []),
    }


def _event_record(event_id, sequence):
    return {
        "actor": "agent-scheduler",
        "correlation_id": "RUN-TEST",
        "event_id": event_id,
        "event_type": "scheduler_started",
        "idempotency_key": f"scheduler-start:{sequence}",
        "payload": {"pool_id": "test"},
        "sequence": sequence,
        "target_agent_id": None,
        "time": f"2026-05-31T00:00:{sequence:02d}Z",
    }


def _write_success_worker(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "message = json.load(sys.stdin)",
                f"target = pathlib.Path({changed_file!r})",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'attempt_id': message['payload']['attempt_id']}), encoding='utf-8')",
                "print(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'shell'}",
                "}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'saw_prompt': 'dispatch_task' in prompt}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'prompt_contains_contract': 'changed_files' in prompt}",
                "}), encoding='utf-8')",
                "print(json.dumps({'event': 'fake_codex_done'}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex_planner(path, dirty_file=None):
    lines = [
        "import json",
        "import pathlib",
        "import sys",
        "args = sys.argv[1:]",
        "prompt = sys.stdin.read()",
        "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
        "worktree = pathlib.Path(args[args.index('-C') + 1])",
        "if 'AgentTeam planner' not in prompt or 'task_proposal' not in prompt:",
        "    sys.exit(7)",
    ]
    if dirty_file:
        lines.extend(
            [
                f"dirty = worktree / {dirty_file!r}",
                "dirty.parent.mkdir(parents=True, exist_ok=True)",
                "dirty.write_text('dirty planner change\\n', encoding='utf-8')",
            ]
        )
    lines.extend(
        [
            "output_path.parent.mkdir(parents=True, exist_ok=True)",
            "output_path.write_text(json.dumps({",
            "    'result_status': 'completed',",
            "    'changed_files': [],",
            "    'output': {",
            "        'adapter': 'codex',",
            "        'task_proposal': {",
            "            'milestone_id': 'M23',",
            "            'tasks': [{",
            "                'task_id': 'TASK-M23-CODEX-001',",
            "                'objective': 'Run generated Codex planner worker task.',",
            "                'read_scope': ['.'],",
            "                'write_scope': ['generated/'],",
            "                'required_role': 'repo_map_agent',",
            "                'risk_target': 'L0',",
            "                'depends_on': [],",
            "                'blockers': [],",
            "            }],",
            "        },",
            "    },",
            "}), encoding='utf-8')",
            "print(json.dumps({'event': 'fake_codex_planner_done'}))",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_fake_codex_planner_and_worker(path):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "if 'AgentTeam planner' in prompt:",
                "    if 'task_proposal' not in prompt:",
                "        sys.exit(7)",
                "    output_path.write_text(json.dumps({",
                "        'result_status': 'completed',",
                "        'changed_files': [],",
                "        'output': {",
                "            'adapter': 'codex',",
                "            'task_proposal': {",
                "                'milestone_id': 'M23',",
                "                'tasks': [{",
                "                    'task_id': 'TASK-M23-CODEX-001',",
                "                    'objective': 'Run generated Codex planner worker task.',",
                "                    'read_scope': ['.'],",
                "                    'write_scope': ['generated/'],",
                "                    'required_role': 'repo_map_agent',",
                "                    'risk_target': 'L0',",
                "                    'depends_on': [],",
                "                    'blockers': [],",
                "                }],",
                "            },",
                "        },",
                "    }), encoding='utf-8')",
                "else:",
                "    target = worktree / 'generated' / 'codex_generated_worker.json'",
                "    target.parent.mkdir(parents=True, exist_ok=True)",
                "    target.write_text(json.dumps({'generated_by': 'fake_codex_worker'}), encoding='utf-8')",
                "    output_path.write_text(json.dumps({",
                "        'result_status': 'completed',",
                "        'changed_files': ['generated/codex_generated_worker.json'],",
                "        'output': {'adapter': 'codex'},",
                "    }), encoding='utf-8')",
                "print(json.dumps({'event': 'fake_codex_planner_and_worker_done'}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex_arg_recorder(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "sandbox = args[args.index('-s') + 1]",
                "model = args[args.index('-m') + 1] if '-m' in args else None",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({",
                "    'argv': args,",
                "    'model': model,",
                "    'sandbox': sandbox,",
                "}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'mode': 'fake-options'}",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()

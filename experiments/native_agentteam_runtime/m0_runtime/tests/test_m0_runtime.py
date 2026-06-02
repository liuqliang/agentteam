import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    ShellRuntimeAdapter,
    audit_worktree_diff,
    classify_attempt_outcome,
    read_scheduler_state_index,
    replay_events,
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
            self.assertEqual(state["event_count"], root_event_count)
            self.assertEqual(state["latest_event"]["event_type"], "backlog_updated")
            self.assertEqual(state["latest_event"]["task_id"], "TASK-002")

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
            (tmp_path / "events.jsonl").write_text(
                json.dumps({"event_type": "unknown_event"}) + "\n",
                encoding="utf-8",
            )

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "invalid_event_type")
            self.assertEqual(summary["errors"][0]["event_type"], "unknown_event")

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


def _read_first_jsonl(path):
    return json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])


def _write_backlog(tmp_path, write_scope, tasks=None):
    backlog = {
        "backlog_id": "BL-TEST",
        "items": tasks or [_backlog_task("TASK-001", write_scope=write_scope)],
    }
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps(backlog), encoding="utf-8")
    return path


def _backlog_task(
    task_id,
    write_scope,
    status="ready",
    depends_on=None,
    blockers=None,
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
        "required_role": "repo_map_agent",
        "blockers": list(blockers or []),
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


if __name__ == "__main__":
    unittest.main()

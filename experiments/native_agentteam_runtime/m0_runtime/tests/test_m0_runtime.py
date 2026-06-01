import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime import FakeRuntimeAdapter, replay_events, run_simulation


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "schemas"


class FixedClock:
    def __init__(self):
        self._ticks = iter(
            [
                "2026-05-31T00:00:00Z",
                "2026-05-31T00:00:01Z",
                "2026-05-31T00:00:02Z",
                "2026-05-31T00:00:03Z",
                "2026-05-31T00:00:04Z",
                "2026-05-31T00:00:05Z",
                "2026-05-31T00:00:06Z",
                "2026-05-31T00:00:07Z",
            ]
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


def _write_backlog(tmp_path, write_scope):
    backlog = {
        "backlog_id": "BL-TEST",
        "items": [
            {
                "task_id": "TASK-001",
                "milestone_id": "M0",
                "objective": "Create generated repo index.",
                "backlog_status": "ready",
                "risk_target": "L0",
                "depends_on": [],
                "read_scope": ["."],
                "write_scope": write_scope,
                "required_role": "repo_map_agent",
                "blockers": [],
            }
        ],
    }
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps(backlog), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()

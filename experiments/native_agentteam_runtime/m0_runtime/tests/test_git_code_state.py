import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentteam_runtime.decision_ledger import DecisionLedger
from agentteam_runtime.decision_runtime import publish_run_decision_binding
from agentteam_runtime.git_code_state import (
    GitCodeStateError,
    publish_attempt_code_state,
    resolve_attempt_code_state,
    restore_attempt_workspace,
)
from agentteam_runtime.two_phase_scheduler import TwoPhaseFileScheduler


class FixedClock:
    def __init__(self):
        self.base = datetime(2026, 8, 6, tzinfo=UTC)
        self.seconds = 0

    def now(self):
        value = self.base + timedelta(seconds=self.seconds)
        self.seconds += 1
        return value.isoformat().replace("+00:00", "Z")


class GitCodeStateTests(unittest.TestCase):
    def _git(self, repo, *arguments, check=True):
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.name", "Test")
        self._git(repo, "config", "user.email", "test@example.com")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "tracked.txt")
        self._git(repo, "commit", "-q", "-m", "base")
        return repo

    def _decision(self):
        return {
            "schema_version": "decision_record.v1",
            "decision_id": "DEC-execution",
            "revision": 1,
            "decision_kind": "direction",
            "subject": "implementation_route",
            "authority_level": "L2",
            "parent_decision_id": None,
            "supersedes_decision_id": None,
            "statement": "Implement and retain recoverable code state.",
            "selected_option": "git_backed_state",
            "alternatives": [],
            "rationale": "Recovery must not depend on a temporary worktree.",
            "scope": ["runtime"],
            "expected_outcome": "The attempt can be reconstructed from Git.",
            "acceptance_refs": ["git-code-state"],
            "status": "active",
            "created_at": "2026-08-06T00:00:00Z",
            "created_by": "operator",
            "previous_revision_sha256": None,
        }

    def _binding(self, root, run_id="git-state-run"):
        work_root = root / "work"
        frozen = work_root / "frozen" / run_id
        run_dir = work_root / "runs" / run_id
        frozen.mkdir(parents=True)
        (frozen / "manifest.json").write_text(
            json.dumps({"digest_sha256": "a" * 64}),
            encoding="utf-8",
        )
        binding = publish_run_decision_binding(
            work_root,
            frozen,
            run_dir,
            {
                "taskpack_id": run_id,
                "decision_contract": {
                    "schema_version": "taskpack_decision_contract.v1",
                    "root_decision_id": "DEC-execution",
                    "decisions": [self._decision()],
                    "task_bindings": {"TASK-001": "DEC-execution"},
                },
            },
            task_ids=["TASK-001"],
        )
        return work_root, run_dir, binding

    def _attempt_worktree(self, repo, root):
        worktree = root / "attempt"
        self._git(repo, "worktree", "add", "-q", "-b", "attempt", str(worktree), "HEAD")
        return worktree

    def test_publish_is_idempotent_and_does_not_mutate_worker_git_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            work_root, _run_dir, binding = self._binding(root)
            worktree = self._attempt_worktree(repo, root)
            original_head = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
            before_status = self._git(worktree, "status", "--porcelain").stdout

            first = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=worktree,
                run_id="git-state-run",
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=["tracked.txt"],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )
            second = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=worktree,
                run_id="git-state-run",
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=["tracked.txt"],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )

            self.assertEqual(first["code_state_commit_sha"], second["code_state_commit_sha"])
            self.assertEqual(second["code_state_status"], "reused_existing")
            self.assertEqual(self._git(worktree, "rev-parse", "HEAD").stdout.strip(), original_head)
            self.assertEqual(self._git(worktree, "status", "--porcelain").stdout, before_status)
            retained = self._git(repo, "rev-parse", first["code_state_ref"]).stdout.strip()
            self.assertEqual(retained, first["code_state_commit_sha"])
            self.assertEqual(
                self._git(repo, "rev-parse", first["checkpoint_ref"]).stdout.strip(),
                first["code_state_commit_sha"],
            )
            self.assertEqual(
                self._git(repo, "show", f"{retained}:tracked.txt").stdout,
                "changed\n",
            )
            message = self._git(repo, "show", "-s", "--format=%B", retained).stdout
            self.assertIn("AgentTeam-Decision: DEC-execution", message)
            links = DecisionLedger(work_root).artifact_links("DEC-execution")
            self.assertEqual([item["artifact_kind"] for item in links].count("code_state"), 1)

    def test_unchanged_terminal_state_has_decision_bound_empty_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            _work_root, run_dir, binding = self._binding(root)
            worktree = self._attempt_worktree(repo, root)
            worker_head = self._git(worktree, "rev-parse", "HEAD").stdout.strip()

            state = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=worktree,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=[],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )

            self.assertEqual(state["code_state_status"], "unchanged")
            self.assertNotEqual(state["code_state_commit_sha"], worker_head)
            self.assertEqual(
                self._git(repo, "rev-parse", f"{state['code_state_commit_sha']}^").stdout.strip(),
                worker_head,
            )
            self.assertEqual(
                self._git(
                    repo,
                    "rev-parse",
                    f"{state['code_state_commit_sha']}^{{tree}}",
                ).stdout.strip(),
                self._git(repo, "rev-parse", f"{worker_head}^{{tree}}").stdout.strip(),
            )

    def test_restore_uses_verified_ref_and_replays_only_event_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            _work_root, run_dir, binding = self._binding(root)
            worktree = self._attempt_worktree(repo, root)
            (worktree / "tracked.txt").write_text("checkpoint\n", encoding="utf-8")
            state = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=worktree,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=["tracked.txt"],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )
            events = [
                {"sequence": 1, "event_type": "runtime_session_started", "payload": {}},
                {
                    "sequence": 2,
                    "event_type": "code_state_published",
                    "payload": {"code_state_artifact_id": state["code_state_artifact_id"]},
                },
                {"sequence": 3, "event_type": "runtime_output_received", "payload": {}},
            ]
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in events),
                encoding="utf-8",
            )
            self._git(repo, "worktree", "remove", "--force", str(worktree))

            restored = restore_attempt_workspace(
                binding,
                project_root=repo,
                destination=worktree,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                events_path=run_dir / "events.jsonl",
            )

            self.assertEqual((worktree / "tracked.txt").read_text(encoding="utf-8"), "checkpoint\n")
            self.assertIn("tracked.txt", self._git(worktree, "status", "--porcelain").stdout)
            self.assertEqual(restored["checkpoint_event_sequence"], 2)
            self.assertEqual([item["sequence"] for item in restored["event_tail"]], [3])
            self.assertEqual(restored["event_tail_count"], 1)

    def test_restore_repairs_ref_first_publication_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            work_root, run_dir, binding = self._binding(root)
            ledger_path = DecisionLedger(work_root).path
            ledger_before_publication = ledger_path.read_bytes()
            worktree = self._attempt_worktree(repo, root)
            (worktree / "tracked.txt").write_text("recover-link\n", encoding="utf-8")
            state = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=worktree,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=["tracked.txt"],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )

            ledger_path.write_bytes(ledger_before_publication)
            with self.assertRaisesRegex(GitCodeStateError, "has no decision artifact"):
                resolve_attempt_code_state(
                    binding,
                    project_root=repo,
                    run_id=run_dir.name,
                    task_id="TASK-001",
                    attempt_id="TASK-001-ATTEMPT-001",
                )
            self._git(repo, "worktree", "remove", "--force", str(worktree))

            restored = restore_attempt_workspace(
                binding,
                project_root=repo,
                destination=worktree,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
            )

            self.assertEqual(restored["code_state_commit_sha"], state["code_state_commit_sha"])
            repaired = {
                item["artifact_id"]: item
                for item in DecisionLedger(work_root).artifact_links("DEC-execution")
            }
            self.assertEqual(
                repaired[state["code_state_artifact_id"]]["digest"],
                state["code_state_commit_sha"],
            )
            self.assertEqual(
                (worktree / "tracked.txt").read_text(encoding="utf-8"),
                "recover-link\n",
            )

    def test_stale_ref_and_conflicting_republication_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            _work_root, run_dir, binding = self._binding(root)
            worktree = self._attempt_worktree(repo, root)
            base = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            (worktree / "tracked.txt").write_text("first\n", encoding="utf-8")
            state = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=worktree,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=["tracked.txt"],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )
            self._git(repo, "update-ref", state["code_state_ref"], base)
            with self.assertRaisesRegex(GitCodeStateError, "differs from decision artifact"):
                resolve_attempt_code_state(
                    binding,
                    project_root=repo,
                    run_id=run_dir.name,
                    task_id="TASK-001",
                    attempt_id="TASK-001-ATTEMPT-001",
                )
            self._git(repo, "update-ref", state["code_state_ref"], state["code_state_commit_sha"])
            (worktree / "tracked.txt").write_text("second\n", encoding="utf-8")
            with self.assertRaisesRegex(GitCodeStateError, "conflicts with current tree"):
                publish_attempt_code_state(
                    binding,
                    project_root=repo,
                    worktree_path=worktree,
                    run_id=run_dir.name,
                    task_id="TASK-001",
                    attempt_id="TASK-001-ATTEMPT-001",
                    changed_files=["tracked.txt"],
                    created_at="2026-08-06T00:01:00Z",
                    validation_status="accepted",
                )

    def test_independent_workspace_objects_are_imported_and_restorable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            _work_root, run_dir, binding = self._binding(root)
            workspace = root / "independent"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--no-hardlinks",
                    "-q",
                    str(repo),
                    str(workspace),
                ],
                check=True,
            )
            self._git(workspace, "remote", "remove", "origin")
            (workspace / "tracked.txt").write_text("independent\n", encoding="utf-8")
            state = publish_attempt_code_state(
                binding,
                project_root=repo,
                worktree_path=workspace,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                changed_files=["tracked.txt"],
                created_at="2026-08-06T00:01:00Z",
                validation_status="accepted",
            )
            self.assertEqual(
                self._git(repo, "cat-file", "-t", state["code_state_commit_sha"]).stdout.strip(),
                "commit",
            )
            self.assertEqual(
                self._git(
                    workspace,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/agentteam/export/",
                ).stdout,
                "",
            )
            shutil.rmtree(workspace)
            restored = restore_attempt_workspace(
                binding,
                project_root=repo,
                destination=workspace,
                run_id=run_dir.name,
                task_id="TASK-001",
                attempt_id="TASK-001-ATTEMPT-001",
                independent=True,
            )
            self.assertEqual(restored["recovery_status"], "restored_from_code_state")
            self.assertEqual(
                (workspace / "tracked.txt").read_text(encoding="utf-8"),
                "independent\n",
            )
            self.assertEqual(self._git(workspace, "remote").stdout, "")

    def test_scheduler_links_attempt_and_verified_integration_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            work_root, run_dir, binding = self._binding(root)
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
                                "objective": "Write one verified file.",
                                "backlog_status": "ready",
                                "risk_target": "L1",
                                "depends_on": [],
                                "blockers": [],
                                "read_scope": ["."],
                                "write_scope": ["feature.txt"],
                                "required_role": "implementation_worker",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool,
                backlog,
                run_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[sys.executable, "-c", "raise SystemExit(0)"],
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "verified\n",
                encoding="utf-8",
            )
            outbox = Path(inflight["outbox_path"])
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(
                json.dumps(
                    {
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
                            "changed_files": ["feature.txt"],
                            "output": {
                                "operator_summary": {
                                    "what_changed": ["新增 feature.txt。"],
                                    "verification_summary": ["集成验证通过。"],
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = scheduler.collect_ready_results()["results"][0]
            self.assertEqual(result["task_status"], "done")
            self.assertTrue(result["code_state_commit_sha"])
            self.assertTrue(result["integration_code_state_commit_sha"])
            self.assertNotEqual(
                result["code_state_decision_id"],
                result["integration_code_state_decision_id"],
            )
            self.assertEqual(
                (Path(inflight["worktree_path"]) / "feature.txt").read_text(encoding="utf-8"),
                "verified\n",
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            code_events = [item for item in events if item["event_type"] == "code_state_published"]
            self.assertEqual(len(code_events), 2)
            self.assertEqual(code_events[0]["decision_id"], "DEC-execution")
            self.assertEqual(
                code_events[1]["decision_id"],
                result["acceptance_decision_id"],
            )
            self.assertNotIn("code_state_artifact_id", code_events[1]["payload"])
            self.assertEqual(
                code_events[1]["payload"]["integration_code_state_artifact_id"],
                result["integration_code_state_artifact_id"],
            )
            code_links = [
                item
                for item in DecisionLedger(work_root).artifact_links()
                if item["artifact_kind"] == "code_state"
            ]
            self.assertEqual(len(code_links), 2)

    def test_scheduler_recovers_missing_attempt_worktree_before_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            _work_root, run_dir, _binding = self._binding(root)
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
                                "objective": "Recover and integrate one verified file.",
                                "backlog_status": "ready",
                                "risk_target": "L1",
                                "depends_on": [],
                                "blockers": [],
                                "read_scope": ["."],
                                "write_scope": ["feature.txt"],
                                "required_role": "implementation_worker",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool,
                backlog,
                run_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[sys.executable, "-c", "raise SystemExit(0)"],
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree = Path(inflight["worktree_path"])
            (worktree / "feature.txt").write_text("recovered\n", encoding="utf-8")
            outbox = Path(inflight["outbox_path"])
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(
                json.dumps(
                    {
                        "message_id": "RESULT-RECOVERY",
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
                            "changed_files": ["feature.txt"],
                            "output": {
                                "operator_summary": {
                                    "what_changed": ["新增可恢复文件。"],
                                    "verification_summary": ["恢复后验证通过。"],
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            published = publish_attempt_code_state(
                scheduler.decision_binding,
                project_root=repo,
                worktree_path=worktree,
                run_id=run_dir.name,
                task_id=inflight["task_id"],
                attempt_id=inflight["attempt_id"],
                changed_files=["feature.txt"],
                created_at=inflight["created_at"],
                validation_status="accepted",
            )
            self._git(repo, "worktree", "remove", "--force", str(worktree))

            resumed = TwoPhaseFileScheduler(
                agent_pool,
                backlog,
                run_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[sys.executable, "-c", "raise SystemExit(0)"],
            )
            result = resumed.collect_ready_results()["results"][0]

            self.assertEqual(result["task_status"], "done")
            self.assertEqual(result["code_state_commit_sha"], published["code_state_commit_sha"])
            self.assertEqual(result["code_state_status"], "reused_existing")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(
                (Path(inflight["worktree_path"]) / "feature.txt").read_text(encoding="utf-8"),
                "recovered\n",
            )
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            recovery_events = [
                item for item in events if item["event_type"] == "code_state_recovered"
            ]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(
                recovery_events[0]["payload"]["code_state_artifact_id"],
                published["code_state_artifact_id"],
            )
            self.assertIsNone(
                recovery_events[0]["payload"]["checkpoint_event_sequence"]
            )


if __name__ == "__main__":
    unittest.main()

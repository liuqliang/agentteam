import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentteam_runtime.decision_artifact_lifecycle import (
    artifact_cost_snapshot,
    load_operator_report,
)
from agentteam_runtime.decision_ledger import DecisionLedger
from agentteam_runtime.decision_runtime import publish_run_decision_binding
from agentteam_runtime.integration_batch import verify_integration_batch
from agentteam_runtime.integration_queue import read_integration_queue
from agentteam_runtime.git_code_state import restore_attempt_workspace
from agentteam_runtime.legacy_decision_index import build_legacy_decision_index
from agentteam_runtime.m0_runtime import replay_events
from agentteam_runtime.notifications import _event_text
from agentteam_runtime.operator_report import build_run_completion_report
from agentteam_runtime.projection_db import (
    project_projection_db_path,
    read_projected_decision_graph,
    rebuild_project_projection_db,
)
from agentteam_runtime.two_phase_scheduler import (
    TwoPhaseFileScheduler,
    _operator_report_from_state,
)


class FixedClock:
    def __init__(self):
        self.base = datetime(2026, 8, 6, tzinfo=UTC)
        self.seconds = 0

    def now(self):
        value = self.base + timedelta(seconds=self.seconds)
        self.seconds += 1
        return value.isoformat().replace("+00:00", "Z")


class DecisionArtifactLifecycleTests(unittest.TestCase):
    def _git(self, repo, *arguments):
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
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
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "base.txt")
        self._git(repo, "commit", "-q", "-m", "base")
        return repo

    def _scheduler(self, root, repo):
        work_root = root / "work"
        run_dir = work_root / "runs" / "d5-run"
        frozen = work_root / "frozen" / "d5-run"
        frozen.mkdir(parents=True)
        (frozen / "manifest.json").write_text(
            json.dumps({"digest_sha256": "a" * 64}),
            encoding="utf-8",
        )
        decision = {
            "schema_version": "decision_record.v1",
            "decision_id": "DEC-d5",
            "revision": 1,
            "decision_kind": "direction",
            "subject": "artifact_reduction",
            "authority_level": "L1",
            "parent_decision_id": None,
            "supersedes_decision_id": None,
            "statement": "Reduce redundant terminal artifacts.",
            "selected_option": "decision_linked_report_and_git_state",
            "alternatives": [],
            "rationale": "Git and one report are sufficient terminal authority.",
            "scope": ["runtime"],
            "expected_outcome": "Recovery and reporting work without a patch file.",
            "acceptance_refs": ["d5-equivalence"],
            "status": "active",
            "created_at": "2026-08-06T00:00:00Z",
            "created_by": "operator",
            "previous_revision_sha256": None,
        }
        publish_run_decision_binding(
            work_root,
            frozen,
            run_dir,
            {
                "taskpack_id": "d5-run",
                "decision_contract": {
                    "schema_version": "taskpack_decision_contract.v1",
                    "root_decision_id": "DEC-d5",
                    "decisions": [decision],
                    "task_bindings": {"TASK-001": "DEC-d5"},
                },
            },
            task_ids=["TASK-001"],
        )
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
                            "objective": "Create one deferred integration change.",
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
            integrate_accepted_patch=False,
        )
        return scheduler, work_root, run_dir

    def test_terminal_compaction_preserves_report_and_git_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            scheduler, work_root, run_dir = self._scheduler(root, repo)
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            (Path(inflight["worktree_path"]) / "feature.txt").write_text(
                "from-git-state\n",
                encoding="utf-8",
            )
            outbox = Path(inflight["outbox_path"])
            outbox.parent.mkdir(parents=True, exist_ok=True)
            outbox.write_text(
                json.dumps(
                    {
                        "message_id": "RESULT-D5",
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
                                    "what_changed": ["新增由 Git code-state 保存的功能文件。"],
                                    "verification_summary": ["延迟集成验证通过。"],
                                }
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            invocation_dir = run_dir / "model_invocations" / "INV-D5"
            invocation_dir.mkdir(parents=True)
            (invocation_dir / "terminal.json").write_text(
                json.dumps({"terminal_status": "completed"}),
                encoding="utf-8",
            )
            (invocation_dir / "stdout.jsonl").write_text(
                "".join(
                    json.dumps({"type": "provider_event", "payload": "x" * 120}) + "\n"
                    for _ in range(1000)
                ),
                encoding="utf-8",
            )
            (invocation_dir / "stderr.log").write_text("", encoding="utf-8")
            orphan_ref = "refs/agentteam/runs/d5-run/orphan/ATTEMPT-ORPHAN"
            self._git(repo, "update-ref", orphan_ref, "HEAD")
            unrelated_ref = "refs/agentteam/runs/another-run/orphan/ATTEMPT-OTHER"
            self._git(repo, "update-ref", unrelated_ref, "HEAD")
            active_export_ref = "refs/agentteam/export/unlinked-active-publication"
            self._git(repo, "update-ref", active_export_ref, "HEAD")
            result = scheduler.collect_ready_results()["results"][0]
            self.assertEqual(result["patch_retention_status"], "replaced_by_git_code_state")
            self.assertIsNone(result["patch_path"])
            self.assertFalse(any((run_dir / "attempts").rglob("*.patch")))
            queue_item = read_integration_queue(run_dir)["items"][0]
            self.assertIsNone(queue_item["patch_path"])
            self.assertEqual(
                queue_item["code_state_commit_sha"],
                result["code_state_commit_sha"],
            )
            replayed_queue = replay_events(run_dir / "events.jsonl")[
                "integration_queue"
            ]["TASK-001:TASK-001-ATTEMPT-001"]
            self.assertEqual(
                replayed_queue["code_state_commit_sha"],
                result["code_state_commit_sha"],
            )

            batch = verify_integration_batch(
                repo,
                run_dir,
                "D5-BATCH",
                [
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('feature.txt').read_text() == 'from-git-state\\n'",
                ],
            )
            self.assertEqual(batch["batch_status"], "verified")
            self.assertEqual(batch["code_state_commits"], [result["code_state_commit_sha"]])

            before_cost = artifact_cost_snapshot(run_dir)
            report_before_compaction = _operator_report_from_state(scheduler.state)
            scheduler.complete_verified_backlog(1)
            after_cost = artifact_cost_snapshot(run_dir)
            state = json.loads(scheduler.state_path.read_text(encoding="utf-8"))
            report_metadata = state["operator_report_artifact"]
            report = load_operator_report(report_metadata)
            self.assertEqual(report, report_before_compaction)
            compact_result = state["steps"][0]["result"]
            self.assertNotIn("runtime_output", compact_result)
            self.assertNotIn("changed_files", compact_result)
            self.assertEqual(compact_result["changed_file_count"], 1)
            self.assertFalse(outbox.exists())
            self.assertLessEqual(
                (invocation_dir / "stdout.jsonl").stat().st_size,
                64 * 1024,
            )
            self.assertFalse((invocation_dir / "stderr.log").exists())
            self.assertEqual(
                self._git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    orphan_ref,
                ).stdout,
                "",
            )
            self.assertEqual(
                self._git(repo, "rev-parse", result["code_state_ref"]).stdout.strip(),
                result["code_state_commit_sha"],
            )
            self.assertEqual(
                self._git(repo, "rev-parse", unrelated_ref).stdout.strip(),
                self._git(repo, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                self._git(repo, "rev-parse", active_export_ref).stdout.strip(),
                self._git(repo, "rev-parse", "HEAD").stdout.strip(),
            )
            attempt_worktree = Path(inflight["worktree_path"])
            self._git(repo, "worktree", "remove", "--force", str(attempt_worktree))
            restored = restore_attempt_workspace(
                scheduler.decision_binding,
                project_root=repo,
                destination=attempt_worktree,
                run_id=run_dir.name,
                task_id=inflight["task_id"],
                attempt_id=inflight["attempt_id"],
                events_path=run_dir / "events.jsonl",
            )
            self.assertEqual(restored["recovery_status"], "restored_from_code_state")
            self.assertEqual(
                (attempt_worktree / "feature.txt").read_text(encoding="utf-8"),
                "from-git-state\n",
            )
            self.assertLess(after_cost["file_count"], before_cost["file_count"])
            self.assertLess(after_cost["total_bytes"], before_cost["total_bytes"])
            self.assertLess(
                after_cost["redundant_unit_count"],
                before_cost["redundant_unit_count"],
            )
            self.assertLess(after_cost["redundant_bytes"], before_cost["redundant_bytes"])
            self.assertEqual(
                report["task_reports"][0]["what_changed"],
                ["新增由 Git code-state 保存的功能文件。"],
            )

            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            runtime_event = next(
                item for item in events if item["event_type"] == "runtime_output_received"
            )
            self.assertNotIn("output", runtime_event["payload"])
            self.assertNotIn("changed_files", runtime_event["payload"])
            self.assertNotIn("patch_path", runtime_event["payload"])
            validation_event = next(
                item for item in events if item["event_type"] == "validation_accepted"
            )
            self.assertEqual(
                validation_event["payload"]["evidence_artifact_id"],
                result["evidence_artifact_id"],
            )
            completed = next(item for item in events if item["event_type"] == "run_completed")
            self.assertNotIn("operator_report", completed["payload"])
            self.assertEqual(
                completed["payload"]["operator_report_artifact"]["report_sha256"],
                report_metadata["report_sha256"],
            )
            notification = _event_text(completed, run_dir, "project")
            self.assertIn("新增由 Git code-state 保存的功能文件。", notification)
            report_links = [
                item
                for item in DecisionLedger(work_root).artifact_links("DEC-d5")
                if item["artifact_kind"] == "report"
            ]
            self.assertEqual(len(report_links), 1)
            evidence_links = [
                item
                for item in DecisionLedger(work_root).artifact_links(
                    result["acceptance_decision_id"]
                )
                if item["artifact_kind"] == "evidence"
            ]
            self.assertEqual(
                [item["artifact_id"] for item in evidence_links],
                [result["evidence_artifact_id"]],
            )
            rebuilt = rebuild_project_projection_db(work_root)
            self.assertGreaterEqual(rebuilt["decision_artifacts"], 4)
            projected = read_projected_decision_graph(work_root, "DEC-d5")
            projected_ids = {
                item["artifact_id"]
                for item in projected["decision_graph"]["artifacts"]
            }
            self.assertIn(report_metadata["report_artifact_id"], projected_ids)
            self.assertIn(result["code_state_artifact_id"], projected_ids)
            project_projection_db_path(work_root).unlink()
            fallback = read_projected_decision_graph(work_root, "DEC-d5")
            self.assertEqual(fallback["projection_source"], "files")
            self.assertEqual(
                {
                    item["artifact_id"]
                    for item in fallback["decision_graph"]["artifacts"]
                },
                projected_ids,
            )
            state_backup = scheduler.state_path.with_suffix(".json.backup")
            scheduler.state_path.rename(state_backup)
            report_without_state = build_run_completion_report(
                run_dir,
                write_files=False,
            )
            state_backup.rename(scheduler.state_path)
            self.assertEqual(report_without_state["task_count"], 1)
            self.assertEqual(
                report_without_state["operator_report"]["task_reports"][0][
                    "what_changed"
                ],
                ["新增由 Git code-state 保存的功能文件。"],
            )

    def test_legacy_index_is_additive_deterministic_and_does_not_infer_rationale(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._repo(root)
            work_root = root / "work"
            run_dir = work_root / "runs" / "legacy-taskpack" / "legacy-run"
            frozen = work_root / "frozen" / "legacy-taskpack"
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "reports").mkdir()
            frozen.mkdir(parents=True)
            (run_dir / "state" / "run_identity.v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": "run_identity.v1",
                        "run_id": "legacy-run",
                        "taskpack_id": "legacy-taskpack",
                    }
                ),
                encoding="utf-8",
            )
            head = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "steps": [
                            {
                                "result": {
                                    "integration_baseline_commit_sha": head,
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "reports" / "operator.json").write_text(
                json.dumps({"summary": "historical report"}),
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event_type": "validation_accepted",
                        "payload": {
                            "task_id": "TASK-OLD",
                            "attempt_id": "ATTEMPT-OLD",
                            "validation_status": "accepted",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (frozen / "manifest.json").write_text(
                json.dumps({"digest_sha256": "b" * 64}),
                encoding="utf-8",
            )
            historical = {
                path: path.read_bytes()
                for path in [
                    run_dir / "state" / "run_identity.v1.json",
                    run_dir / "state" / "two_phase_scheduler_state.json",
                    run_dir / "reports" / "operator.json",
                    run_dir / "events.jsonl",
                    frozen / "manifest.json",
                ]
            }

            first = build_legacy_decision_index(work_root, project_root=repo)
            second = build_legacy_decision_index(work_root, project_root=repo)

            self.assertEqual(first, second)
            self.assertEqual(first["lineage_count"], 1)
            self.assertEqual(
                {path: path.read_bytes() for path in historical},
                historical,
            )
            index = json.loads(Path(first["index_path"]).read_text(encoding="utf-8"))
            decision = index["lineages"][0]["decision"]
            self.assertEqual(decision["statement"], "legacy execution lineage")
            self.assertEqual(
                decision["rationale"],
                "Historical records do not contain decision rationale.",
            )
            self.assertEqual(
                index["lineages"][0]["validation_summaries"][0]["validation_status"],
                "accepted",
            )
            links = DecisionLedger(work_root).artifact_links(decision["decision_id"])
            self.assertEqual(
                {item["artifact_kind"] for item in links},
                {"code_state", "contract", "evidence", "report"},
            )
            self.assertIn(
                "refs/agentteam/legacy/",
                self._git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/agentteam/legacy/",
                ).stdout,
            )


if __name__ == "__main__":
    unittest.main()

import contextlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentteam_runtime.projection_db as projection_db
from agentteam_runtime.operator_report import build_run_completion_report, render_run_completion_report
from agentteam_runtime.phase1_usage_report import (
    FIXED_ARTIFACT_RELATIVE as PHASE1_FINALIZATION_ARTIFACT,
    FIXED_CHANGED_PATHS,
    REPORT_EVIDENCE_SCHEMA_VERSION,
    REPORT_RELATIVE_PATH,
    ROADMAP_RELATIVE_PATH,
    Phase1UsageReportError,
    _deterministic_completion_evidence,
    _verification_summary,
    complete_phase1_usage_report,
    render_report_artifacts,
    render_milestone_report,
    validate_report_only_commit,
)
from agentteam_runtime.profile import build_project_profile
from agentteam_runtime.projection_db import rebuild_project_projection_db


NATIVE_RUNTIME_CORRECTNESS_COMMAND = [
    "python3",
    "-m",
    "unittest",
    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
]


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _token_usage():
    return {
        "usage_status": "unavailable",
        "reported_attempt_count": 0,
        "unreported_attempt_count": 1,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


def _write_operator_run(work_root, run_id="operator-path-run", steps=None):
    run_dir = work_root / "runs" / run_id
    _write_jsonl(
        run_dir / "events.jsonl",
        [
            {
                "sequence": 1,
                "event_type": "run_completed",
                "time": "2026-06-21T00:00:00Z",
                "payload": {
                    "run_status": "completed",
                    "scheduler_status": "idle",
                    "operator_report": {
                        "task_count": 1,
                        "blocked_count": 0,
                        "task_reports": [
                            {
                                "task_id": "TASK-001",
                                "result_status": "completed",
                                "what_changed": ["operator path fixture"],
                                "verification": ["operator path fixture verified"],
                                "evidence_status": "complete",
                            }
                        ],
                    },
                },
            }
        ],
    )
    _write_json(
        run_dir / "state" / "two_phase_scheduler_state.json",
        {"scheduler_status": "idle", "steps": steps or []},
    )
    return run_dir


def _follow_up_item(run_id, report_path, objective, next_step):
    return {
        "objective": objective,
        "source": "goal_memory.follow_up_queue",
        "source_taskpack_id": run_id,
        "source_report_path": str(report_path),
        "source_result_status": "completed",
        "source_run_outcome": "completed_with_review_required",
        "stop_reason": "review_gate_required",
        "recommended_next_step": next_step,
        "suggested_verification": "python3 -m unittest tests.test_projection_db_operator_paths",
        "source_evidence_paths": [{"type": "report", "path": str(report_path)}],
        "blockers": ["operator review required"],
        "token_usage": _token_usage(),
        "readiness": "ready",
    }


def _write_goal_memory(
    work_root,
    run_id,
    *,
    objective="continue operator projection coverage",
    next_step="continue operator projection coverage",
    extra_follow_up_items=None,
):
    report_path = work_root / "runs" / run_id / "reports" / "final_report.md"
    memory_path = work_root / "pursue" / f"{run_id}-goal-memory.json"
    follow_up_queue = [
        _follow_up_item(run_id, report_path, objective, next_step),
        *(extra_follow_up_items or []),
    ]
    _write_json(
        memory_path,
        {
            "memory_schema_version": "goal_memory.v1",
            "memory_path": str(memory_path),
            "latest_taskpack_id": run_id,
            "latest_run_ids": [run_id],
            "latest_round_recap": {
                "taskpack_id": run_id,
                "result_status": "completed",
                "run_outcome": "completed_with_review_required",
                "stop_reason": "review_gate_required",
                "recommended_next_step": next_step,
                "suggested_verification": "python3 -m unittest tests.test_projection_db_operator_paths",
                "evidence_paths": [{"type": "report", "path": str(report_path)}],
                "blockers": ["operator review required"],
                "token_usage": _token_usage(),
            },
            "follow_up_queue": follow_up_queue,
        },
    )
    return memory_path


def _write_authoritative_recap(work_root, run_id, memory_path):
    report_path = work_root / "runs" / run_id / "reports" / "final_report.md"
    recap_path = work_root / "pursue" / f"{run_id}.json"
    _write_json(
        recap_path,
        {
            "pursue_id": run_id,
            "rounds_completed": 1,
            "max_rounds": 2,
            "stop_reason": "review_gate_required",
            "latest_taskpack_id": run_id,
            "latest_report_path": str(report_path),
            "goal_memory_path": str(memory_path),
            "operator_next_action": f"agentteam report --taskpack {run_id}",
            "runs": [{"taskpack_id": run_id}],
        },
    )
    return recap_path


def _phase1_report_evidence(validated_code_sha):
    digests = {
        "acceptance_attempts_sha256": "1" * 64,
        "deterministic_verification_sha256": "2" * 64,
        "gate_epoch_sha256": "3" * 64,
        "p1_live_receipt_sha256": "4" * 64,
        "projection_invocation_sha256": "5" * 64,
        "selected_live_artifact_sha256": "6" * 64,
    }
    empty_totals = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "contributing_invocation_count": 0,
    }
    reported_totals = {
        "input_tokens": 7,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_tokens": 3,
        "total_tokens": 12,
        "contributing_invocation_count": 1,
    }
    return {
        "schema_version": REPORT_EVIDENCE_SCHEMA_VERSION,
        "authority": "canonical_deterministic_and_p1_live",
        "implementation_run_id": "phase1-model-invocation-usage",
        "gate_epoch": 1,
        "acceptance_series_id": "phase1-acceptance",
        "selected_acceptance_run_id": "phase1-acceptance-attempt-2",
        "git_object_format": "sha1",
        "validated_code_sha": validated_code_sha,
        "evidence_digests": digests,
        "deterministic_evidence": {
            "status": "passed",
            "scheduler_status": "awaiting_post_backlog_gates",
            "task_results": [
                {
                    "task_id": "P1-06D",
                    "attempt_id": "P1-06D-ATTEMPT-001",
                    "result_status": "completed",
                    "integration_status": "passed",
                    "integration_verification_status": "passed",
                    "integration_verification_additions_status": "passed",
                }
            ],
            "state_sha256": "7" * 64,
        },
        "projection_summary": {
            "projection_source": "db",
            "check_status": "passed",
            "schema_version": "agentteam_projection.v5",
            "invocation_count": 2,
            "open_invocations": 0,
            "lifecycle_terminal_coverage": {
                "covered": 2,
                "total": 2,
                "percent": 100.0,
                "status": "complete",
            },
            "token_usage_coverage": {
                "covered": 1,
                "total": 2,
                "percent": 50.0,
                "status": "partial",
            },
            "reported_token_totals": reported_totals,
            "partial_known_token_lower_bounds": empty_totals,
            "usage_status_counts": {
                "reported": 1,
                "partial": 0,
                "unavailable": 1,
                "not_applicable": 0,
            },
        },
        "acceptance_attempts": [
            {
                "run_id": "phase1-acceptance-attempt-1",
                "acceptance_series_id": "phase1-acceptance",
                "acceptance_attempt_id": "attempt-1",
                "selected": False,
                "controller_status": "failed",
                "usage": {
                    "invocation_count": 1,
                    "open_invocations": 0,
                    "usage_status_counts": {
                        "reported": 0,
                        "partial": 0,
                        "unavailable": 1,
                        "not_applicable": 0,
                    },
                    "reported_token_totals": empty_totals,
                    "partial_known_token_lower_bounds": empty_totals,
                },
            },
            {
                "run_id": "phase1-acceptance-attempt-2",
                "acceptance_series_id": "phase1-acceptance",
                "acceptance_attempt_id": "attempt-2",
                "selected": True,
                "controller_status": "passed",
                "usage": {
                    "invocation_count": 1,
                    "open_invocations": 0,
                    "usage_status_counts": {
                        "reported": 1,
                        "partial": 0,
                        "unavailable": 0,
                        "not_applicable": 0,
                    },
                    "reported_token_totals": reported_totals,
                    "partial_known_token_lower_bounds": empty_totals,
                },
            },
        ],
        "verification_summary": {
            "deterministic_completion_status": "passed",
            "task_result_count": 1,
            "integration_verification_passed_count": 1,
            "projection_replay_status": "passed",
            "verification_command_sha256": "8" * 64,
            "verification_result_sha256": "9" * 64,
        },
        "deterministic_fixture_token_totals": {
            "input_tokens": 270,
            "cached_input_tokens": 45,
            "output_tokens": 75,
            "reasoning_tokens": 30,
            "total_tokens": 345,
        },
        "implemented_invocation_paths": [
            "implementation_worker",
            "acceptance_live_smoke",
        ],
        "unsupported_or_unavailable_paths": ["provider_failed_before_usage"],
        "changed_files": ["agentteam_runtime/model_invocation.py"],
        "remaining_risks": [
            "P1-06E operator approval remains required before source integration"
        ],
    }


def _git(repo, *arguments):
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _initialize_report_repository(root):
    repo = Path(root) / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Phase 1 Test")
    _git(repo, "config", "user.email", "phase1-test@example.invalid")
    roadmap = repo / ROADMAP_RELATIVE_PATH
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    roadmap.write_text("# Native runtime roadmap\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "validated code")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit_report_artifacts(repo, artifacts, *, extra_path=None):
    for relative, payload in artifacts.items():
        path = Path(repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    if extra_path:
        path = Path(repo) / extra_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("out of scope\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "report only")
    return _git(repo, "rev-parse", "HEAD")


class ProjectionDbOperatorPathTests(unittest.TestCase):
    def test_projection_selects_latest_versioned_implementation_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            flat = runs_root / "phase1-run"
            versioned = runs_root / "v4" / "phase1-run"
            for run_dir, sequence, task_id in (
                (flat, 1, "OLD-TASK"),
                (versioned, 3, "CURRENT-TASK"),
            ):
                _write_json(
                    run_dir / "state" / "run_identity.v1.json",
                    {
                        "schema_version": "run_identity.v1",
                        "run_id": "phase1-run",
                        "run_kind": "implementation",
                        "creation_sequence": sequence,
                    },
                )
                _write_json(
                    run_dir / "state" / "two_phase_scheduler_state.json",
                    {
                        "scheduler_status": "idle",
                        "steps": [{"task_id": task_id}],
                    },
                )

            runs = projection_db._scan_runs(runs_root)

            self.assertEqual(len(runs), 1)
            self.assertEqual(Path(runs[0]["run_dir"]), versioned.resolve())
            self.assertEqual(
                runs[0]["state"]["steps"][0]["task_id"],
                "CURRENT-TASK",
            )

    def test_verification_summary_filters_unrelated_project_runs(self):
        summary = _verification_summary(
            {
                "status": "passed",
                "task_results": [{"task_id": "TASK-1"}],
            },
            {"check_status": "passed"},
            {
                "integration_outcomes": [
                    {
                        "run_id": "phase1-run",
                        "integration_verification_status": "passed",
                    },
                    {
                        "run_id": "unrelated-run",
                        "integration_verification_status": "passed",
                    },
                ]
            },
            implementation_run_id="phase1-run",
        )

        self.assertEqual(summary["integration_verification_passed_count"], 1)

    def test_deterministic_evidence_accepts_native_scheduler_result_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "awaiting_post_backlog_gates",
                    "steps": [
                        {
                            "task_id": "P1-06D",
                            "step_status": "processed",
                            "validation_status": "accepted",
                            "result": {
                                "attempt_id": "P1-06D-ATTEMPT-001",
                                "integration_status": "applied",
                                "integration_verification_status": "passed",
                                "integration_verification_additions_status": (
                                    "not_requested"
                                ),
                                "integration_verification_additions": [],
                            },
                        }
                    ],
                },
            )

            evidence = _deterministic_completion_evidence(run_dir)

            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(
                evidence["task_results"][0]["result_status"],
                "completed",
            )
            self.assertEqual(
                evidence["task_results"][0]["attempt_id"],
                "P1-06D-ATTEMPT-001",
            )

    def test_deterministic_evidence_rejects_unaccepted_or_unverified_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            state_path = (
                run_dir / "state" / "two_phase_scheduler_state.json"
            )
            state = {
                "scheduler_status": "awaiting_post_backlog_gates",
                "steps": [
                    {
                        "task_id": "P1-06D",
                        "step_status": "processed",
                        "validation_status": "rejected",
                        "result": {
                            "attempt_id": "P1-06D-ATTEMPT-001",
                            "integration_status": "applied",
                            "integration_verification_status": "passed",
                            "integration_verification_additions_status": (
                                "not_requested"
                            ),
                            "integration_verification_additions": [],
                        },
                    }
                ],
            }
            _write_json(state_path, state)
            self.assertEqual(
                _deterministic_completion_evidence(run_dir)["status"],
                "failed",
            )

            state["steps"][0]["validation_status"] = "accepted"
            state["steps"][0]["result"][
                "integration_verification_status"
            ] = "failed"
            _write_json(state_path, state)
            self.assertEqual(
                _deterministic_completion_evidence(run_dir)["status"],
                "failed",
            )

    def test_completion_report_keeps_authoritative_recap_behavior_without_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_id = "operator-path-run"
            run_dir = _write_operator_run(work_root, run_id)
            memory_path = _write_goal_memory(
                work_root,
                run_id,
                next_step="authoritative file next step",
            )
            _write_authoritative_recap(work_root, run_id, memory_path)

            report = build_run_completion_report(
                run_dir,
                project="agentteam",
                write_files=False,
            )

        recap = report["pursue_recap"]
        self.assertNotIn("projection_source", recap)
        self.assertEqual(recap["latest_taskpack_id"], run_id)
        self.assertEqual(
            recap["latest_round_recap"]["recommended_next_step"],
            "authoritative file next step",
        )
        self.assertEqual(recap["latest_follow_up_queue"]["item_count"], 1)

    def test_completion_report_uses_fresh_projection_before_scanning_authoritative_recap(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_id = "operator-path-run"
            run_dir = _write_operator_run(work_root, run_id)
            _write_goal_memory(
                work_root,
                run_id,
                objective="projection DB selected next goal",
                next_step="projection DB next step",
            )
            rebuild_project_projection_db(work_root)

            with patch(
                "agentteam_runtime.operator_report.find_pursue_recap_for_run",
                side_effect=AssertionError("authoritative recap fallback should not be scanned"),
            ):
                report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

        recap = report["pursue_recap"]
        self.assertEqual(recap["projection_source"], "db")
        self.assertEqual(recap["projection_status"], "fresh")
        self.assertEqual(recap["latest_taskpack_id"], run_id)
        self.assertTrue(recap["projection_db_path"].endswith("agentteam.db"))
        self.assertEqual(
            recap["latest_round_recap"]["recommended_next_step"],
            "projection DB next step",
        )
        self.assertEqual(
            recap["latest_follow_up_queue"]["next_goal"],
            "projection DB selected next goal",
        )

    def test_completion_report_exposes_structured_pursue_evidence_for_selected_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_id = "operator-path-run"
            run_dir = _write_operator_run(work_root, run_id)
            _write_goal_memory(
                work_root,
                run_id,
                objective="projection DB selected next goal",
                next_step="projection DB next step",
            )
            rebuild_project_projection_db(work_root)

            report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

        recap = report["pursue_recap"]
        structured = recap["structured_evidence"]
        self.assertEqual(structured["schema_version"], "pursue_structured_evidence.v1")
        self.assertEqual(structured["source_report_path"], recap["latest_report_path"])
        self.assertEqual(structured["goal_memory_path"], recap["goal_memory_path"])
        self.assertEqual(structured["selected_next_goal"], "projection DB selected next goal")
        self.assertEqual(structured["previous_result_status"], "completed")
        self.assertEqual(
            structured["previous_run_outcome"],
            "completed_with_review_required",
        )
        self.assertEqual(structured["previous_blockers"], ["operator review required"])
        self.assertEqual(
            structured["previous_evidence_paths"],
            [{"path": recap["latest_report_path"], "type": "report"}],
        )
        rendered = render_run_completion_report(report)
        self.assertIn("Structured evidence:", rendered)
        self.assertIn(
            "selected_next_goal=projection DB selected next goal",
            rendered,
        )

    def test_completion_report_structures_projected_handoff_verification_evidence(self):
        worker_addition = {
            "label": "worker focused check",
            "command": ["python3", "-m", "unittest", "tests.worker_focus"],
            "reason": "worker requested focused verification",
        }
        integration_addition = {
            "label": "integration focused check",
            "command": ["python3", "-m", "unittest", "tests.integration_focus"],
            "reason": "integration requested focused verification",
        }
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_id = "operator-path-run"
            run_dir = _write_operator_run(
                work_root,
                run_id,
                steps=[
                    {
                        "step_id": "STEP-0001",
                        "task_id": "TASK-001",
                        "attempt_id": "ATTEMPT-001",
                        "result": {
                            "task_id": "TASK-001",
                            "attempt_id": "ATTEMPT-001",
                            "result_status": "completed",
                            "runtime_output": {
                                "verification_additions": [worker_addition],
                            },
                            "evidence_status": "complete",
                            "integration_status": "passed",
                            "integration_verification_status": "passed",
                            "integration_verification_additions_status": "passed",
                            "integration_verification_additions": [integration_addition],
                        },
                    }
                ],
            )
            _write_goal_memory(
                work_root,
                run_id,
                objective="projection DB selected next goal",
                next_step="projection DB next step",
            )
            rebuild_project_projection_db(work_root)

            report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

        structured = report["pursue_recap"]["structured_evidence"]
        self.assertEqual(structured["worker_evidence_status"], "complete")
        self.assertEqual(structured["integration_status"], "passed")
        self.assertEqual(structured["integration_verification_status"], "passed")
        self.assertEqual(
            [
                item["label"]
                for item in structured["verification_additions"]
            ],
            ["worker focused check", "integration focused check"],
        )
        rendered = render_run_completion_report(report)
        self.assertIn(
            "integration_verification_status=passed",
            rendered,
        )
        self.assertIn("verification_additions=worker focused check=unknown", rendered)
        self.assertIn("integration focused check=unknown", rendered)

    def test_completion_report_falls_back_to_authoritative_recap_when_projection_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_id = "operator-path-run"
            run_dir = _write_operator_run(work_root, run_id)
            memory_path = _write_goal_memory(
                work_root,
                run_id,
                objective="stale projection next goal",
                next_step="stale projection next step",
            )
            _write_authoritative_recap(work_root, run_id, memory_path)
            rebuild_project_projection_db(work_root)

            report_path = work_root / "runs" / run_id / "reports" / "final_report.md"
            _write_goal_memory(
                work_root,
                run_id,
                objective="fresh authoritative next goal",
                next_step="fresh authoritative next step",
                extra_follow_up_items=[
                    _follow_up_item(
                        run_id,
                        report_path,
                        "second authoritative queue item",
                        "second authoritative next step",
                    )
                ],
            )
            _write_authoritative_recap(work_root, run_id, memory_path)

            report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

        recap = report["pursue_recap"]
        self.assertNotIn("projection_source", recap)
        self.assertEqual(
            recap["latest_round_recap"]["recommended_next_step"],
            "fresh authoritative next step",
        )
        self.assertEqual(recap["latest_follow_up_queue"]["item_count"], 2)

    def test_phase1_milestone_renderer_is_byte_stable_and_includes_all_attempts(self):
        evidence = _phase1_report_evidence("a" * 40)

        first = render_milestone_report(evidence)
        second = render_milestone_report(
            json.loads(json.dumps(evidence, sort_keys=True))
        )

        self.assertEqual(first, second)
        self.assertIn(b"finalization_pending", first)
        self.assertIn(b"phase1-acceptance-attempt-1", first)
        self.assertIn(b"phase1-acceptance-attempt-2", first)
        self.assertIn(b"provider_failed_before_usage", first)
        self.assertNotIn(b"final_report_sha", first)

        invalid = json.loads(json.dumps(evidence))
        invalid["acceptance_attempts"][0].pop("acceptance_series_id")
        with self.assertRaisesRegex(
            Phase1UsageReportError,
            "explicit series",
        ):
            render_milestone_report(invalid)

    def test_report_only_commit_validator_rejects_tamper_and_extra_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, validated = _initialize_report_repository(Path(tmp))
            evidence = _phase1_report_evidence(validated)
            artifacts = render_report_artifacts(
                evidence,
                (repo / ROADMAP_RELATIVE_PATH).read_bytes(),
            )
            final = _commit_report_artifacts(repo, artifacts)

            lineage = validate_report_only_commit(
                repo,
                validated_code_sha=validated,
                final_report_sha=final,
                expected_evidence_digests=evidence["evidence_digests"],
                expected_artifacts=artifacts,
            )
            self.assertEqual(lineage["parent_commit_sha"], validated)
            self.assertEqual(
                lineage["changed_paths"],
                sorted(FIXED_CHANGED_PATHS),
            )

            report_path = repo / REPORT_RELATIVE_PATH
            report_path.write_bytes(
                report_path.read_bytes() + b"\ntampered\n"
            )
            with self.assertRaisesRegex(
                Phase1UsageReportError,
                "clean integration worktree",
            ):
                validate_report_only_commit(
                    repo,
                    validated_code_sha=validated,
                    final_report_sha=final,
                    expected_evidence_digests=evidence["evidence_digests"],
                )

        with tempfile.TemporaryDirectory() as tmp:
            repo, validated = _initialize_report_repository(Path(tmp))
            evidence = _phase1_report_evidence(validated)
            artifacts = render_report_artifacts(
                evidence,
                (repo / ROADMAP_RELATIVE_PATH).read_bytes(),
            )
            final = _commit_report_artifacts(
                repo,
                artifacts,
                extra_path="unexpected.txt",
            )
            with self.assertRaisesRegex(
                Phase1UsageReportError,
                "outside the fixed report scope",
            ):
                validate_report_only_commit(
                    repo,
                    validated_code_sha=validated,
                    final_report_sha=final,
                    expected_evidence_digests=evidence["evidence_digests"],
                )

    def test_phase1_complete_controller_is_resumable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._phase1_completion_fixture(root)
            interrupted = {"count": 0}

            def fail_once(path, artifact):
                interrupted["count"] += 1
                if interrupted["count"] == 1:
                    raise OSError("simulated crash after report commit")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            with self._patched_phase1_completion(fixture):
                with self.assertRaisesRegex(
                    Phase1UsageReportError,
                    "simulated crash after report commit",
                ):
                    complete_phase1_usage_report(
                        **fixture["arguments"],
                        final_publisher=fail_once,
                    )
                recovered = complete_phase1_usage_report(
                    **fixture["arguments"],
                    final_publisher=fail_once,
                )
                repeated = complete_phase1_usage_report(
                    **fixture["arguments"],
                    final_publisher=fail_once,
                )

            self.assertFalse(recovered["idempotent"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(interrupted["count"], 2)
            self.assertEqual(
                _git(fixture["repo"], "rev-list", "--count", "HEAD"),
                "2",
            )
            artifact_path = (
                fixture["selected_run_dir"]
                / PHASE1_FINALIZATION_ARTIFACT
            )
            finalization = json.loads(
                artifact_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                finalization["parent_commit_sha"],
                fixture["validated_code_sha"],
            )
            self.assertEqual(
                finalization["changed_paths"],
                sorted(FIXED_CHANGED_PATHS),
            )

    def test_phase1_complete_controller_accepts_versioned_implementation_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._phase1_completion_fixture(
                Path(tmp),
                namespace="v4",
            )
            with self._patched_phase1_completion(fixture):
                result = complete_phase1_usage_report(
                    **fixture["arguments"],
                )
            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                fixture["context"]["run_dir"],
                fixture["work_root"]
                / "runs"
                / "v4"
                / "phase1-model-invocation-usage",
            )

    def test_native_runtime_verification_profile_stays_compatible_with_focused_addition(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            runtime_root = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime"
            (runtime_root / "agentteam_runtime").mkdir(parents=True)
            (runtime_root / "agentteam_runtime" / "__init__.py").write_text("", encoding="utf-8")
            (runtime_root / "tests").mkdir()
            for test_name in [
                "test_taskpack.py",
                "test_m0_runtime.py",
                "test_projection_db_operator_paths.py",
            ]:
                (runtime_root / "tests" / test_name).write_text("", encoding="utf-8")

            profile = build_project_profile(
                repo,
                project_key="agentteam-native",
                work_root=Path(tmp) / "agentteam-work",
            )

        self.assertEqual(
            profile["verification_profile"]["correctness"]["command"],
            NATIVE_RUNTIME_CORRECTNESS_COMMAND,
        )

    def _phase1_completion_fixture(self, root, *, namespace=None):
        repo, validated = _initialize_report_repository(root)
        work_root = root / "work"
        implementation_run_id = "phase1-model-invocation-usage"
        implementation_run_dir = work_root / "runs"
        if namespace is not None:
            implementation_run_dir /= namespace
        implementation_run_dir /= implementation_run_id
        implementation_run_dir.mkdir(parents=True)
        _write_json(
            implementation_run_dir / "state" / "run_identity.v1.json",
            {
                "schema_version": "run_identity.v1",
                "project_key": "phase1-test",
                "run_id": implementation_run_id,
                "taskpack_id": implementation_run_id,
                "run_kind": "implementation",
                "creation_sequence": 1,
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
        selected_run_id = "phase1-acceptance-attempt-2"
        selected_run_dir = work_root / "runs" / selected_run_id
        selected_run_dir.mkdir(parents=True)
        profile = {
            "project_key": "phase1-test",
            "work_root": str(work_root),
        }
        live_declaration = {
            "gate_id": "P1-LIVE",
            "evidence_artifact": (
                "acceptance/model-invocation-live-smoke.v1.json"
            ),
        }
        final_declaration = {
            "gate_id": "P1-06E",
            "depends_on": ["P1-LIVE"],
            "evidence_artifact": (
                "acceptance/phase1-usage-finalization.v1.json"
            ),
            "evidence_schema": (
                "experiments/native_agentteam_runtime/schemas/"
                "phase1_usage_finalization.schema.json"
            ),
            "required_status_field": "controller_validation_status",
            "required_status_value": "passed",
            "commit_field": "final_report_sha",
            "integration_head_relation": "equals",
        }
        context = {
            "profile": profile,
            "work_root": work_root,
            "project_root": repo,
            "run_dir": implementation_run_dir,
            "taskpack": {"taskpack_id": implementation_run_id},
            "declarations": [live_declaration, final_declaration],
            "declarations_by_id": {
                "P1-LIVE": live_declaration,
                "P1-06E": final_declaration,
            },
            "gate_root": (
                implementation_run_dir / "state" / "post_backlog_gates"
            ),
            "epochs_root": (
                implementation_run_dir
                / "state"
                / "post_backlog_gates"
                / "epochs"
            ),
            "locks_root": (
                implementation_run_dir
                / "state"
                / "post_backlog_gates"
                / "locks"
            ),
        }
        epoch = {
            "record": {
                "epoch_number": 1,
                "integration_head_sha": validated,
                "validated_code_sha": validated,
                "integration_branch": _git(
                    repo,
                    "symbolic-ref",
                    "--short",
                    "HEAD",
                ),
                "git_object_format": "sha1",
            },
            "digest": "3" * 64,
        }
        return {
            "repo": repo,
            "work_root": work_root,
            "profile": profile,
            "context": context,
            "epoch": epoch,
            "evidence": _phase1_report_evidence(validated),
            "validated_code_sha": validated,
            "selected_run_dir": selected_run_dir,
            "arguments": {
                "profile_project_root": repo,
                "candidate_project_root": repo,
                "implementation_run_id": implementation_run_id,
                "implementation_run_dir": implementation_run_dir,
                "gate_epoch": 1,
                "work_root": work_root,
                "acceptance_series_id": "phase1-acceptance",
                "run_id": selected_run_id,
                "validated_code_sha": validated,
            },
        }

    @contextlib.contextmanager
    def _patched_phase1_completion(self, fixture):
        declarations = fixture["context"]["declarations_by_id"]
        patches = [
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_profile_runtime.load_project_profile",
                return_value=fixture["profile"],
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_gate_runtime._require_post_backlog_gate_context",
                return_value=fixture["context"],
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_gate_runtime._require_gate_declaration",
                side_effect=lambda _context, gate_id: declarations[gate_id],
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_gate_runtime._require_current_gate_epoch",
                return_value=fixture["epoch"],
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_gate_runtime._gate_mutation_locks",
                side_effect=lambda _context, _gate_ids: (
                    contextlib.nullcontext()
                ),
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_gate_runtime._gate_integration_worktree",
                return_value=fixture["repo"],
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_acceptance_runtime._require_same_git_repository",
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_acceptance_runtime._verify_candidate_runtime_modules",
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_require_live_gate_passed",
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "build_report_evidence",
                return_value=fixture["evidence"],
            ),
            patch(
                "agentteam_runtime.phase1_usage_report."
                "_validate_existing_finalization_evidence",
            ),
            patch(
                "agentteam_runtime.phase1_usage_report._final_gate_state",
                return_value="awaiting_operator_review",
            ),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            yield


if __name__ == "__main__":
    unittest.main()

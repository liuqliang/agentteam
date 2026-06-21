import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentteam_runtime.operator_report import build_run_completion_report
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


def _write_operator_run(work_root, run_id="operator-path-run"):
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
        {"scheduler_status": "idle", "steps": []},
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


class ProjectionDbOperatorPathTests(unittest.TestCase):
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

            report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

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


if __name__ == "__main__":
    unittest.main()

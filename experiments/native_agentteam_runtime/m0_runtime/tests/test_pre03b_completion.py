import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentteam_runtime import agentteam, cli
from agentteam_runtime.completion_summary import build_completion_summary
from agentteam_runtime.notifications import _event_text
from agentteam_runtime.two_phase_scheduler import TwoPhaseFileScheduler


class RecordingNotificationSink:
    def __init__(self):
        self.calls = []

    def notify(self, event, context):
        self.calls.append({"event": event, "context": context})
        return []


class Pre03BCompletionTests(unittest.TestCase):
    def _scheduler_fixture(self, root, *, gated):
        root = Path(root)
        agent_pool = root / "agent_pool.json"
        backlog = root / "backlog.json"
        output_dir = root / "run"
        agent_pool.write_text(
            json.dumps({"pool_id": "POOL-PRE03B", "agents": []}),
            encoding="utf-8",
        )
        backlog.write_text(
            json.dumps({"backlog_id": "BL-PRE03B", "items": []}),
            encoding="utf-8",
        )
        if gated:
            gate_root = output_dir / "state" / "post_backlog_gates"
            gate_root.mkdir(parents=True)
            (gate_root / "gate_state.v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": "post_backlog_gate_state.v1",
                        "implementation_run_id": output_dir.name,
                        "state": "awaiting_validated_baseline",
                        "gate_declaration_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )
        sink = RecordingNotificationSink()
        return (
            TwoPhaseFileScheduler(
                agent_pool,
                backlog,
                output_dir,
                notification_sink=sink,
            ),
            sink,
        )

    def test_gated_idle_emits_backlog_completed_without_run_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, sink = self._scheduler_fixture(tmp, gated=True)

            summary = scheduler.run_until_idle(max_ticks=1, poll_interval_seconds=0)
            events = [
                json.loads(line)
                for line in scheduler.events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(summary["scheduler_status"], "awaiting_post_backlog_gates")
            self.assertEqual(summary["milestone_status"], "awaiting_post_backlog_gates")
            self.assertIn("gate seal-baseline", summary["next_action"])
            self.assertEqual(
                [event["event_type"] for event in events if event["event_type"].endswith("completed")],
                ["backlog_completed"],
            )
            self.assertEqual(
                [call["event"]["event_type"] for call in sink.calls],
                ["run_started", "backlog_completed"],
            )

    def test_ungated_idle_preserves_run_completed_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, sink = self._scheduler_fixture(tmp, gated=False)

            summary = scheduler.run_until_idle(max_ticks=1, poll_interval_seconds=0)

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["milestone_status"], "completed")
            self.assertEqual(
                [call["event"]["event_type"] for call in sink.calls],
                ["run_started", "run_completed"],
            )

    def test_backlog_completion_replay_does_not_duplicate_event_or_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, sink = self._scheduler_fixture(tmp, gated=True)
            scheduler.run_until_idle(max_ticks=1, poll_interval_seconds=0)
            scheduler.state["run_event_ids"].pop("backlog_completed")
            scheduler._write_state()

            replayed = scheduler.complete_verified_backlog(2)
            events = [
                json.loads(line)
                for line in scheduler.events_path.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(replayed["scheduler_status"], "awaiting_post_backlog_gates")
            self.assertEqual(
                sum(event["event_type"] == "backlog_completed" for event in events),
                1,
            )
            self.assertEqual(
                sum(call["event"]["event_type"] == "backlog_completed" for call in sink.calls),
                1,
            )

    def test_fresh_all_gate_evaluation_emits_one_completion_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, sink = self._scheduler_fixture(tmp, gated=True)
            scheduler.run_until_idle(max_ticks=1, poll_interval_seconds=0)
            decision = {
                "all_passed": True,
                "epoch_number": 1,
                "gates": [
                    {"gate_id": "P1-LIVE", "state": "passed"},
                    {"gate_id": "P1-06E", "state": "passed"},
                ],
            }
            context = {
                "taskpack": {
                    "files": {
                        "agent_pool": "agent_pool.json",
                        "backlog": "backlog.json",
                    }
                },
                "frozen_dir": Path(tmp),
                "run_dir": scheduler.output_dir,
                "project_root": Path(tmp),
                "profile": {},
            }
            with mock.patch.object(
                agentteam,
                "_evaluate_post_backlog_gates",
                return_value=decision,
            ) as evaluate:
                with mock.patch.object(
                    agentteam,
                    "_post_backlog_gate_notification_sink",
                    return_value=sink,
                ):
                    first = agentteam._complete_gated_milestone_if_ready(context)
                    replayed = agentteam._complete_gated_milestone_if_ready(context)

            events = [
                json.loads(line)
                for line in scheduler.events_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(evaluate.call_count, 2)
            self.assertFalse(first["idempotent"])
            self.assertTrue(replayed["idempotent"])
            self.assertEqual(
                sum(event["event_type"] == "run_completed" for event in events),
                1,
            )
            self.assertEqual(
                sum(call["event"]["event_type"] == "run_completed" for call in sink.calls),
                1,
            )

    def test_lower_level_simulation_rejects_gated_taskpack_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent_pool.json").write_text(
                json.dumps({"pool_id": "POOL-PRE03B", "agents": []}),
                encoding="utf-8",
            )
            (root / "backlog.json").write_text(
                json.dumps({"backlog_id": "BL-PRE03B", "items": []}),
                encoding="utf-8",
            )
            (root / "taskpack.yaml").write_text(
                json.dumps({"post_backlog_gates": [{"gate_id": "P1-LIVE"}]}),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                cli.main(
                    [
                        "--agent-pool",
                        str(root / "agent_pool.json"),
                        "--backlog",
                        str(root / "backlog.json"),
                        "--output-dir",
                        str(root / "run"),
                        "--run-until-idle",
                    ]
                )

            self.assertIn("post-backlog gates require", stderr.getvalue())
            self.assertFalse((root / "run" / "events.jsonl").exists())

    def test_one_shot_rejects_gated_taskpack_before_building_runtime_args(self):
        loaded = {
            "taskpack": {
                "taskpack_id": "TASKPACK-PRE03B",
                "post_backlog_gates": [{"gate_id": "P1-LIVE"}],
            }
        }
        with mock.patch.object(agentteam, "load_taskpack", return_value=loaded):
            with mock.patch.object(agentteam, "build_taskpack_runtime_args") as build_args:
                with self.assertRaisesRegex(
                    agentteam.AgentTeamCliError,
                    "--one-shot cannot launch",
                ):
                    agentteam._run_frozen_taskpack(
                        "/tmp/frozen-pre03b",
                        "/tmp/runs-pre03b",
                        one_shot=True,
                    )

        build_args.assert_not_called()

    def test_pending_summary_and_feishu_text_never_recommend_integration(self):
        summary = build_completion_summary(
            run_id="RUN-PRE03B",
            run_status="awaiting_post_backlog_gates",
            task_count=1,
            blocked_count=0,
            task_reports=[],
            integration_baseline={"branch": "agentteam/integration"},
        )
        text = _event_text(
            {
                "event_type": "backlog_completed",
                "payload": {
                    "run_status": "awaiting_post_backlog_gates",
                    "next_action": "agentteam gate register --gate P1-LIVE",
                },
            },
            "/tmp/run-pre03b",
            "agentteam",
        )

        self.assertNotIn("agentteam integrate", summary["integration_recommendation"])
        self.assertEqual(
            summary["follow_up_recommendation"]["action"],
            "complete_post_backlog_gate",
        )
        self.assertIn("gate register --gate P1-LIVE", text)
        self.assertIn("integration is not yet authorized", text)

    def test_canonical_event_contract_declares_backlog_completed(self):
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "event.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("backlog_completed", schema["properties"]["event_type"]["enum"])


if __name__ == "__main__":
    unittest.main()

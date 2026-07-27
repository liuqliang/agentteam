import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime import agentteam
from agentteam_runtime.projection_db import (
    ProjectionIntegrityError,
    build_isolated_acceptance_projection,
    build_project_stats,
    check_project_projection_db,
    rebuild_project_projection_db,
)


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_jsonl(path, payloads):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(payload, sort_keys=True) + "\n" for payload in payloads),
        encoding="utf-8",
    )


def _start(
    invocation_id,
    *,
    run_id="implementation-run",
    stage="implementation_worker",
    attempt_id=None,
    coverage_class="supported_model_invocation",
    implementation_run_id=None,
    gate_epoch=None,
):
    return {
        "start_schema_version": "model_invocation_started.v1",
        "invocation_id": invocation_id,
        "project": "projection-project",
        "run_id": run_id,
        "pursue_id": "PURSUE-001",
        "round_index": 1,
        "taskpack_id": "projection-taskpack",
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "task_id": "TASK-001",
        "attempt_id": attempt_id or f"{invocation_id}-ATTEMPT",
        "runtime_execution_session_id": f"SESSION-{invocation_id}",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "agent_id": "agent-implementation-worker-1",
        "role": "implementation_worker",
        "usage_stage": stage,
        "backend": "codex",
        "model": "gpt-projection",
        "coverage_class": coverage_class,
        "started_at": "2026-07-23T00:00:00Z",
    }


def _terminal(
    start,
    *,
    usage_status="reported",
    terminal_status="completed",
    total_tokens=10,
    unavailable_reason=None,
    provider_usage_scope="invocation",
):
    terminal = {
        **start,
        "usage_schema_version": "model_invocation_usage.v1",
        "usage_event_id": f"USAGE-{start['invocation_id']}",
        "provider_session_id": f"PROVIDER-{start['invocation_id']}",
        "provider_turn_id": f"TURN-{start['invocation_id']}",
        "terminal_status": terminal_status,
        "usage_status": usage_status,
        "usage_source": "codex_jsonl",
        "provider_usage_scope": provider_usage_scope,
        "accounting_method": (
            "provider_reported" if usage_status == "reported" else "unavailable"
        ),
        "provider_usage_snapshot": None,
        "unavailable_reason": unavailable_reason,
        "input_tokens": total_tokens - 2 if total_tokens is not None else None,
        "cached_input_tokens": 2 if total_tokens is not None else None,
        "output_tokens": 2 if total_tokens is not None else None,
        "reasoning_tokens": None,
        "total_tokens": total_tokens,
        "finished_at": "2026-07-23T00:00:01Z",
        "wall_time_seconds": 1.0,
        "source_artifact_path": "runs/implementation-run/events.jsonl",
    }
    terminal.pop("start_schema_version")
    return terminal


def _event(event_type, payload, sequence):
    return {
        "event_id": f"EVT-{sequence:04d}",
        "event_type": event_type,
        "sequence": sequence,
        "source_event_id": (
            payload.get("usage_event_id") or payload.get("invocation_id")
        ),
        "payload": payload,
    }


def _write_run_identity(
    work_root,
    run_id,
    *,
    run_kind="implementation",
    implementation_run_id=None,
    gate_epoch=None,
):
    identity = {
        "schema_version": "run_identity.v1",
        "project_key": "projection-project",
        "run_id": run_id,
        "taskpack_id": "projection-taskpack",
        "run_kind": run_kind,
        "created_at": "2026-07-23T00:00:00Z",
    }
    if run_kind == "implementation":
        identity["creation_sequence"] = 1
    else:
        identity["implementation_run_id"] = implementation_run_id
        identity["gate_epoch"] = gate_epoch
    _write_json(
        Path(work_root) / "runs" / run_id / "state" / "run_identity.v1.json",
        identity,
    )


class InvocationProjectionTests(unittest.TestCase):
    def _write_authority_fixture(self, work_root):
        _write_run_identity(work_root, "implementation-run")
        reported = _start("INV-reported", attempt_id="ATTEMPT-001")
        partial = _start("INV-partial", attempt_id="ATTEMPT-002")
        open_start = _start("INV-open", attempt_id="ATTEMPT-003")
        events = [
            _event("model_invocation_started", reported, 1),
            _event(
                "model_invocation_usage_recorded",
                _terminal(reported, total_tokens=10),
                2,
            ),
            _event("model_invocation_started", partial, 3),
            _event(
                "model_invocation_usage_recorded",
                _terminal(
                    partial,
                    usage_status="partial",
                    total_tokens=900,
                    unavailable_reason="provider_session_lineage_ambiguous",
                    provider_usage_scope="session_cumulative",
                ),
                4,
            ),
        ]
        _write_jsonl(
            Path(work_root) / "runs" / "implementation-run" / "events.jsonl",
            events,
        )
        lifecycle_dir = (
            Path(work_root)
            / "runs"
            / "implementation-run"
            / "model_invocations"
            / open_start["invocation_id"]
        )
        _write_json(lifecycle_dir / "started.json", open_start)
        return open_start

    def test_file_and_database_stats_share_invocation_semantics_and_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            self._write_authority_fixture(work_root)

            file_stats = build_project_stats(work_root)
            rebuilt = rebuild_project_projection_db(work_root)
            db_stats = build_project_stats(work_root)
            first_digest = rebuilt["invocation_digest"]

            self.assertEqual(rebuilt["invocations"], 3)
            self.assertEqual(db_stats["projection_source"], "db")
            self.assertEqual(
                file_stats["model_invocation_usage"],
                db_stats["model_invocation_usage"],
            )
            usage = db_stats["model_invocation_usage"]
            self.assertEqual(usage["invocation_count"], 3)
            self.assertEqual(
                usage["lifecycle_terminal_coverage"]["covered"],
                2,
            )
            self.assertEqual(usage["lifecycle_terminal_coverage"]["total"], 3)
            self.assertEqual(usage["token_usage_coverage"]["covered"], 1)
            self.assertEqual(usage["reported_token_totals"]["total_tokens"], 10)
            self.assertIsNone(
                usage["partial_known_token_lower_bounds"]["total_tokens"]
            )
            self.assertEqual(
                usage["attempt_breakdown"]["ATTEMPT-001"]["invocation_count"],
                1,
            )

            db_path = Path(rebuilt["db_path"])
            db_path.unlink()
            rebuilt_again = rebuild_project_projection_db(work_root)
            self.assertEqual(rebuilt_again["invocation_digest"], first_digest)
            self.assertEqual(
                build_project_stats(work_root)["model_invocation_usage"],
                db_stats["model_invocation_usage"],
            )

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "pragma table_info(invocations)"
                    )
                }
            self.assertTrue(
                {
                    "invocation_id",
                    "usage_event_id",
                    "run_kind",
                    "lifecycle_status",
                    "record_sha256",
                }.issubset(columns)
            )

    def test_open_recovery_changes_coverage_without_changing_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            open_start = self._write_authority_fixture(work_root)
            before = build_project_stats(work_root)["model_invocation_usage"]
            terminal_path = (
                work_root
                / "runs"
                / "implementation-run"
                / "model_invocations"
                / open_start["invocation_id"]
                / "terminal.json"
            )
            _write_json(
                terminal_path,
                _terminal(
                    open_start,
                    usage_status="unavailable",
                    total_tokens=None,
                    terminal_status="launch_failed",
                    unavailable_reason="worker_process_confirmed_dead",
                ),
            )
            after = build_project_stats(work_root)["model_invocation_usage"]

            self.assertEqual(before["invocation_count"], after["invocation_count"])
            self.assertEqual(before["open_invocations"], 1)
            self.assertEqual(after["open_invocations"], 0)
            self.assertEqual(
                after["lifecycle_terminal_coverage"],
                {"covered": 3, "total": 3, "percent": 100.0, "status": "complete"},
            )
            self.assertEqual(after["token_usage_coverage"]["covered"], 1)

    def test_safe_partial_tokens_are_only_a_separate_lower_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            start = _start("INV-safe-partial")
            terminal = _terminal(
                start,
                usage_status="partial",
                total_tokens=9,
                unavailable_reason="provider_payload_incomplete",
            )
            _write_jsonl(
                work_root / "runs" / "implementation-run" / "events.jsonl",
                [
                    _event("model_invocation_started", start, 1),
                    _event(
                        "model_invocation_usage_recorded",
                        terminal,
                        2,
                    ),
                ],
            )

            usage = build_project_stats(work_root)["model_invocation_usage"]

            self.assertIsNone(usage["reported_token_totals"]["total_tokens"])
            self.assertEqual(
                usage["partial_known_token_lower_bounds"]["total_tokens"],
                9,
            )
            self.assertEqual(
                usage["observed_token_lower_bound"]["total_tokens"],
                9,
            )

    def test_filters_and_corrupt_fallback_preserve_file_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            self._write_authority_fixture(work_root)
            rebuilt = rebuild_project_projection_db(work_root)
            filtered = build_project_stats(
                work_root,
                filters={"attempt": "ATTEMPT-001", "stage": "implementation_worker"},
            )

            self.assertEqual(
                filtered["model_invocation_usage"]["invocation_count"],
                1,
            )
            self.assertEqual(
                filtered["model_invocation_usage"]["authority_invocation_count"],
                3,
            )
            Path(rebuilt["db_path"]).write_text("not sqlite", encoding="utf-8")
            fallback = build_project_stats(
                work_root,
                filters={"attempt_id": "ATTEMPT-001"},
            )
            self.assertEqual(fallback["projection_source"], "files")
            self.assertEqual(
                fallback["projection_warning"],
                "projection_db_unavailable",
            )
            self.assertEqual(
                fallback["model_invocation_usage"]["invocation_count"],
                1,
            )

            args = agentteam._build_parser().parse_args(
                [
                    "stats",
                    "--run",
                    "implementation-run",
                    "--run-kind",
                    "implementation",
                    "--implementation-run",
                    "logical-run",
                    "--gate-epoch",
                    "2",
                    "--pursue",
                    "PURSUE-001",
                    "--round",
                    "1",
                    "--stage",
                    "implementation_worker",
                    "--role",
                    "implementation_worker",
                    "--task",
                    "TASK-001",
                    "--attempt",
                    "ATTEMPT-001",
                    "--backend",
                    "codex",
                    "--model",
                    "gpt-projection",
                ]
            )
            self.assertEqual(args.stats_run_id, "implementation-run")
            self.assertEqual(args.gate_epoch, 2)
            self.assertEqual(args.round_index, 1)

    def test_passed_acceptance_and_isolated_staged_artifact_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_root = root / "work"
            acceptance_run = "acceptance-attempt-1"
            _write_run_identity(
                work_root,
                acceptance_run,
                run_kind="acceptance_evidence",
                implementation_run_id="implementation-run",
                gate_epoch=3,
            )
            acceptance_start = _start(
                "INV-acceptance",
                run_id=acceptance_run,
                stage="acceptance_live_smoke",
                implementation_run_id="implementation-run",
                gate_epoch=3,
            )
            acceptance_terminal = _terminal(acceptance_start, total_tokens=22)
            passed = {
                "schema_version": "model_invocation_live_smoke.v1",
                "project": "projection-project",
                "run_id": acceptance_run,
                "run_kind": "acceptance_evidence",
                "taskpack_id": "projection-taskpack",
                "implementation_run_id": "implementation-run",
                "gate_epoch": 3,
                "controller_validation_status": "passed",
                "invocation_start_record": acceptance_start,
                "invocation_usage_record": acceptance_terminal,
            }
            _write_json(
                work_root / "runs" / acceptance_run / "acceptance" / "live.json",
                passed,
            )
            outside = dict(passed)
            outside["run_id"] = "outside-run"
            outside["invocation_start_record"] = _start(
                "INV-outside",
                run_id="outside-run",
                stage="acceptance_live_smoke",
                implementation_run_id="implementation-run",
                gate_epoch=3,
            )
            outside["invocation_usage_record"] = _terminal(
                outside["invocation_start_record"],
                total_tokens=999,
            )
            _write_json(root / "outside" / "live.json", outside)

            stats = build_project_stats(work_root)
            usage = stats["model_invocation_usage"]
            self.assertEqual(usage["invocation_count"], 1)
            self.assertEqual(
                usage["run_kind_breakdown"]["acceptance_evidence"][
                    "invocation_count"
                ],
                1,
            )
            self.assertEqual(
                usage["implementation_run_breakdown"]["implementation-run"][
                    "invocation_count"
                ],
                1,
            )

            staged_start = _start(
                "INV-staged",
                run_id="acceptance-attempt-2",
                stage="acceptance_live_smoke",
                implementation_run_id="implementation-run",
                gate_epoch=3,
            )
            staged = {
                **passed,
                "run_id": "acceptance-attempt-2",
                "controller_validation_status": "staged",
                "invocation_start_record": staged_start,
                "invocation_usage_record": _terminal(
                    staged_start,
                    total_tokens=33,
                ),
            }
            staged_path = (
                work_root
                / "runs"
                / "acceptance-attempt-2"
                / "acceptance"
                / "staging"
                / "candidate.json"
            )
            _write_json(staged_path, staged)
            isolated_db = root / "isolated" / "acceptance.db"
            isolated = build_isolated_acceptance_projection(
                work_root,
                staged_path,
                isolated_db,
                filters={"run": "acceptance-attempt-2"},
            )
            self.assertEqual(
                isolated["model_invocation_usage"]["invocation_count"],
                1,
            )
            self.assertEqual(
                isolated["model_invocation_usage"]["token_usage_coverage"][
                    "percent"
                ],
                100.0,
            )
            self.assertFalse(
                (work_root / "agentteam.db").exists(),
                "isolated projection must not publish the project DB",
            )
            self.assertEqual(
                build_project_stats(work_root)["model_invocation_usage"][
                    "invocation_count"
                ],
                1,
            )

    def test_registered_controller_lifecycle_projects_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            run_dir = work_root / "runs" / "controller-run"
            session_root = (
                run_dir
                / "state"
                / "controller_invocations"
                / "runtime_diagnostic"
                / "DIAGNOSTIC-SESSION-001"
            )
            start = _start(
                "INV-controller",
                run_id="controller-run",
                stage="runtime_diagnostic",
            )
            start["runtime_execution_session_id"] = "DIAGNOSTIC-SESSION-001"
            start["agent_id"] = "runtime-diagnostic-controller"
            start["role"] = "runtime_diagnostic"
            terminal = _terminal(start, total_tokens=15)
            _write_json(
                session_root / "controller_claim.json",
                {
                    "claim_schema_version": (
                        "model_invocation_controller_claim.v1"
                    ),
                    "authority_root": str(session_root.resolve()),
                    "runtime_execution_session_id": (
                        "DIAGNOSTIC-SESSION-001"
                    ),
                    "usage_stage": "runtime_diagnostic",
                },
            )
            _write_json(
                session_root
                / "model_invocations"
                / start["invocation_id"]
                / "started.json",
                start,
            )
            _write_json(
                session_root
                / "model_invocations"
                / start["invocation_id"]
                / "terminal.json",
                terminal,
            )
            _write_jsonl(
                run_dir / "events.jsonl",
                [
                    _event("model_invocation_started", start, 1),
                    _event(
                        "model_invocation_usage_recorded",
                        terminal,
                        2,
                    ),
                ],
            )
            unregistered = _start(
                "INV-unregistered",
                run_id="controller-run",
                stage="development_smoke",
            )
            _write_json(
                run_dir
                / "state"
                / "controller_invocations"
                / "development_smoke"
                / "UNREGISTERED"
                / "model_invocations"
                / unregistered["invocation_id"]
                / "started.json",
                unregistered,
            )

            usage = build_project_stats(work_root)["model_invocation_usage"]

            self.assertEqual(usage["invocation_count"], 1)
            self.assertEqual(
                usage["stage_breakdown"]["runtime_diagnostic"][
                    "invocation_count"
                ],
                1,
            )
            self.assertEqual(
                usage["role_breakdown"]["runtime_diagnostic"][
                    "invocation_count"
                ],
                1,
            )

    def test_source_metadata_does_not_create_a_conflicting_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            start = _start("INV-source-metadata")
            event_start = {
                **start,
                "_source_artifact_path": (
                    "model_invocations/INV-source-metadata/started.json"
                ),
                "_source_record_sha256": "a" * 64,
            }
            _write_jsonl(
                work_root / "runs" / "implementation-run" / "events.jsonl",
                [_event("model_invocation_started", event_start, 1)],
            )
            _write_json(
                work_root
                / "runs"
                / "implementation-run"
                / "model_invocations"
                / start["invocation_id"]
                / "started.json",
                start,
            )

            result = rebuild_project_projection_db(work_root)

            self.assertEqual(result["invocations"], 1)

    def test_conflicting_duplicate_start_is_an_integrity_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            start = _start("INV-conflict")
            _write_jsonl(
                work_root / "runs" / "implementation-run" / "events.jsonl",
                [_event("model_invocation_started", start, 1)],
            )
            conflicting = dict(start)
            conflicting["model"] = "other-model"
            _write_json(
                work_root
                / "runs"
                / "implementation-run"
                / "model_invocations"
                / start["invocation_id"]
                / "started.json",
                conflicting,
            )

            with self.assertRaises(ProjectionIntegrityError):
                rebuild_project_projection_db(work_root)


if __name__ == "__main__":
    unittest.main()

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentteam_runtime.live_codex_smoke import (
    SUPPORTED_NON_WORKER_INVOCATION_INVENTORY,
)
from agentteam_runtime.model_invocation import (
    ExecutionGroupIdentity,
    InvocationLifecycle,
    assess_execution_group_fence,
    import_model_invocation_lifecycle,
    replay_model_invocation_events,
)
from agentteam_runtime.projection_db import (
    build_project_stats,
    rebuild_project_projection_db,
)
from agentteam_runtime.two_phase_scheduler import (
    SUPPORTED_WORKER_INVOCATION_INVENTORY,
    reconcile_orphaned_invocation,
)


POSITIVE_RUN_ID = "phase1-usage-positive"
NEGATIVE_RUN_ID = "phase1-usage-recovery-negative"
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

CORE_WORKER_PATHS = (
    {
        "route": "decompose_backlog",
        "role": "*",
        "usage_stage": "planner_or_task_slicer",
    },
    {
        "route": "worker",
        "role": "repo_map_agent",
        "usage_stage": "repo_map",
    },
    {
        "route": "worker",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
    },
    {
        "route": "worker",
        "role": "reviewer",
        "usage_stage": "review_or_repair",
    },
    {
        "route": "worker",
        "role": "semantic_architecture_agent",
        "usage_stage": "semantic_architecture",
    },
)

CORE_NON_WORKER_PATHS = (
    "taskpack_author",
    "follow_up_author",
    "runtime_diagnostic",
)

TRACKED_DEVELOPMENT_SMOKES = tuple(
    sorted(
        path
        for path in SUPPORTED_NON_WORKER_INVOCATION_INVENTORY
        if path.startswith("development_smoke_")
    )
)

POSITIVE_PATH_MATRIX = (
    {
        "path": "taskpack_author",
        "role": "taskpack_author",
        "usage_stage": "taskpack_author",
    },
    {
        "path": "planner_or_task_slicer",
        "role": "task_planner",
        "usage_stage": "planner_or_task_slicer",
    },
    {
        "path": "repo_map",
        "role": "repo_map_agent",
        "usage_stage": "repo_map",
    },
    {
        "path": "implementation_worker",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "resumed_session_turn": 1,
    },
    {
        "path": "review_or_repair",
        "role": "reviewer",
        "usage_stage": "review_or_repair",
        "resumed_session_turn": 2,
    },
    {
        "path": "semantic_architecture",
        "role": "semantic_architecture_agent",
        "usage_stage": "semantic_architecture",
    },
    {
        "path": "failed_after_reported_usage",
        "role": "implementation_worker",
        "usage_stage": "implementation_worker",
        "terminal_status": "failed",
    },
    {
        "path": "follow_up_author",
        "role": "follow_up_author",
        "usage_stage": "follow_up_author",
    },
    {
        "path": "runtime_diagnostic",
        "role": "runtime_diagnostic",
        "usage_stage": "runtime_diagnostic",
    },
) + tuple(
    {
        "path": smoke_path,
        "role": "development_smoke",
        "usage_stage": "development_smoke",
    }
    for smoke_path in TRACKED_DEVELOPMENT_SMOKES
)

POSITIVE_STAGE_COUNTS = {
    "development_smoke": len(TRACKED_DEVELOPMENT_SMOKES),
    "follow_up_author": 1,
    "implementation_worker": 2,
    "planner_or_task_slicer": 1,
    "repo_map": 1,
    "review_or_repair": 1,
    "runtime_diagnostic": 1,
    "semantic_architecture": 1,
    "taskpack_author": 1,
}

POSITIVE_TOKEN_TOTALS = {
    "input_tokens": 270,
    "cached_input_tokens": 45,
    "output_tokens": 75,
    "reasoning_tokens": 30,
    "total_tokens": 345,
}


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_run_identity(work_root, run_id):
    _write_json(
        Path(work_root)
        / "runs"
        / run_id
        / "state"
        / "run_identity.v1.json",
        {
            "schema_version": "run_identity.v1",
            "project_key": "phase1-usage-fixture",
            "run_id": run_id,
            "taskpack_id": "phase1-model-invocation-usage",
            "run_kind": "implementation",
            "creation_sequence": 1,
            "created_at": "2026-07-23T00:00:00Z",
        },
    )


def _execution_identity(index):
    return ExecutionGroupIdentity(
        gated_supervisor_pid=4000 + index,
        gated_supervisor_pgid=4000 + index,
        host_boot_id="12345678-1234-1234-1234-123456789abc",
        gated_supervisor_start_ticks=9000 + index,
        launch_nonce_sha256=f"{index:064x}",
        systemd_linger_enabled=True,
        systemd_transient_unit=f"agentteam-inv-fixture-{index:03d}.service",
        systemd_transient_invocation_id=f"{index:032x}",
        systemd_transient_kill_mode="control-group",
        systemd_user_manager_identity="manager-monotonic:123456",
        systemd_transient_control_group=(
            f"/user.slice/agentteam-inv-fixture-{index:03d}.service"
        ),
        systemd_user_service_invocation_id="f" * 32,
        systemd_user_service_control_group="/user.slice/user-1000.slice",
        systemd_user_service_kill_mode="mixed",
    )


def _invocation_context(
    path,
    index,
    *,
    run_id=POSITIVE_RUN_ID,
    usage_stage=None,
    role=None,
    lease_id=None,
):
    invocation_id = f"INV-PHASE1-{run_id.upper()}-{index:03d}"
    lease_id = lease_id or f"LEASE-PHASE1-{index:03d}"
    resumed_turn = path.get("resumed_session_turn")
    predecessor_id = (
        f"INV-PHASE1-{run_id.upper()}-004"
        if resumed_turn == 2
        else None
    )
    predecessor_turn = "provider-turn-1" if resumed_turn == 2 else None
    return invocation_id, {
        "project": "phase1-usage-fixture",
        "run_id": run_id,
        "pursue_id": "PURSUE-PHASE1",
        "round_index": 1,
        "taskpack_id": "phase1-model-invocation-usage",
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": f"PATH-{path['path']}",
        "attempt_id": f"ATTEMPT-PHASE1-{index:03d}",
        "runtime_execution_session_id": f"RUNTIME-SESSION-{index:03d}",
        "requested_provider_session_id": (
            "PROVIDER-SESSION-RESUMED" if resumed_turn else None
        ),
        "provider_resume_mode": "explicit" if resumed_turn else "new",
        "provider_predecessor_invocation_id": predecessor_id,
        "provider_predecessor_turn_id": predecessor_turn,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": lease_id,
        "agent_id": f"agent-{path['path']}",
        "role": role or path["role"],
        "usage_stage": usage_stage or path["usage_stage"],
        "backend": "codex",
        "model": "gpt-deterministic-fixture",
        "coverage_class": "supported_model_invocation",
        "provider_usage_scope": "invocation",
        "provider_session_lock_held": bool(resumed_turn),
        "provider_project_binding_valid": bool(resumed_turn),
        "provider_lineage_status": (
            "authoritative" if resumed_turn else None
        ),
        "previous_provider_session_id": (
            "PROVIDER-SESSION-RESUMED" if resumed_turn == 2 else None
        ),
        "previous_provider_turn_id": predecessor_turn,
        "previous_invocation_id": predecessor_id,
    }


def _reported_usage_jsonl(index, path):
    resumed_turn = path.get("resumed_session_turn")
    return json.dumps(
        {
            "type": "turn.completed",
            "provider_usage_scope": "invocation",
            "provider_session_id": (
                "PROVIDER-SESSION-RESUMED"
                if resumed_turn
                else f"PROVIDER-SESSION-{index:03d}"
            ),
            "provider_turn_id": (
                f"provider-turn-{resumed_turn}"
                if resumed_turn
                else f"provider-turn-{index:03d}"
            ),
            "usage": {
                "input_tokens": 10 + index,
                "cached_input_tokens": 3,
                "output_tokens": 5,
                "reasoning_tokens": 2,
                "total_tokens": 15 + index,
            },
        },
        sort_keys=True,
    )


def _build_positive_fixture(work_root, replay_events_path):
    run_dir = Path(work_root) / "runs" / POSITIVE_RUN_ID
    _write_run_identity(work_root, POSITIVE_RUN_ID)
    lifecycles = {}
    terminals = {}
    for index, path in enumerate(POSITIVE_PATH_MATRIX, start=1):
        invocation_id, context = _invocation_context(path, index)
        lifecycle = InvocationLifecycle(
            run_dir,
            context,
            invocation_id=invocation_id,
            started_at=f"2026-07-23T00:{index:02d}:00Z",
        )
        lifecycle.publish_start(_execution_identity(index))
        terminal = lifecycle.finalize(
            path.get("terminal_status", "completed"),
            stdout=_reported_usage_jsonl(index, path),
            finished_at=f"2026-07-23T00:{index:02d}:01Z",
        )
        import_model_invocation_lifecycle(
            replay_events_path,
            lifecycle.started_path,
            terminal_path=lifecycle.terminal_path,
            actor="phase1-deterministic-fixture",
            run_id=POSITIVE_RUN_ID,
            step_id="P1-06A-POSITIVE",
            source_root=work_root,
            require_terminal=True,
        )
        lifecycles[path["path"]] = lifecycle
        terminals[path["path"]] = terminal
    return {
        "run_dir": run_dir,
        "lifecycles": lifecycles,
        "terminals": terminals,
    }


def _coverage(covered, total):
    return {
        "covered": covered,
        "total": total,
        "percent": (covered * 100.0) / total,
        "status": "complete" if covered == total else "partial",
    }


class Phase1UsageEndToEndTests(unittest.TestCase):
    def test_positive_fixture_has_complete_exact_replay_stable_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_root = root / "positive-work"
            replay_events_path = root / "replay" / "positive-events.jsonl"

            fixture = _build_positive_fixture(
                work_root,
                replay_events_path,
            )

            for expected in CORE_WORKER_PATHS:
                self.assertIn(
                    expected,
                    SUPPORTED_WORKER_INVOCATION_INVENTORY,
                )
            self.assertEqual(
                set(SUPPORTED_NON_WORKER_INVOCATION_INVENTORY)
                - {"acceptance_live_smoke"},
                {
                    *CORE_NON_WORKER_PATHS,
                    *TRACKED_DEVELOPMENT_SMOKES,
                },
            )
            self.assertEqual(
                {path["path"] for path in POSITIVE_PATH_MATRIX},
                {
                    "taskpack_author",
                    "planner_or_task_slicer",
                    "repo_map",
                    "implementation_worker",
                    "review_or_repair",
                    "semantic_architecture",
                    "failed_after_reported_usage",
                    "follow_up_author",
                    "runtime_diagnostic",
                    *TRACKED_DEVELOPMENT_SMOKES,
                },
            )

            resumed_first = fixture["terminals"]["implementation_worker"]
            resumed_second = fixture["terminals"]["review_or_repair"]
            self.assertEqual(
                resumed_first["provider_session_id"],
                resumed_second["provider_session_id"],
            )
            self.assertNotEqual(
                resumed_first["runtime_execution_session_id"],
                resumed_second["runtime_execution_session_id"],
            )
            self.assertEqual(
                resumed_second["provider_predecessor_invocation_id"],
                resumed_first["invocation_id"],
            )
            self.assertEqual(
                resumed_first["accounting_method"],
                "provider_reported",
            )
            self.assertEqual(
                resumed_second["provider_usage_scope"],
                "invocation",
            )
            self.assertEqual(
                fixture["terminals"]["failed_after_reported_usage"][
                    "terminal_status"
                ],
                "failed",
            )
            self.assertEqual(
                fixture["terminals"]["failed_after_reported_usage"][
                    "usage_status"
                ],
                "reported",
            )

            first_replay = replay_model_invocation_events(replay_events_path)
            replay_event_bytes = replay_events_path.read_bytes()
            for lifecycle in fixture["lifecycles"].values():
                self.assertEqual(
                    import_model_invocation_lifecycle(
                        replay_events_path,
                        lifecycle.started_path,
                        terminal_path=lifecycle.terminal_path,
                        actor="phase1-deterministic-fixture",
                        run_id=POSITIVE_RUN_ID,
                        step_id="P1-06A-POSITIVE",
                        source_root=work_root,
                        require_terminal=True,
                    ),
                    [],
                )
            second_replay = replay_model_invocation_events(
                replay_events_path
            )
            self.assertEqual(first_replay, second_replay)
            self.assertEqual(
                replay_events_path.read_bytes(),
                replay_event_bytes,
            )
            self.assertEqual(
                first_replay["invocation_count"],
                len(POSITIVE_PATH_MATRIX),
            )
            self.assertEqual(
                first_replay["terminal_count"],
                len(POSITIVE_PATH_MATRIX),
            )
            self.assertEqual(first_replay["open_invocation_ids"], [])
            self.assertEqual(
                first_replay["reported_token_totals"],
                POSITIVE_TOKEN_TOTALS,
            )

            file_stats = build_project_stats(work_root)
            file_usage = file_stats["model_invocation_usage"]
            self._assert_positive_usage(file_usage)
            self.assertEqual(file_stats["projection_source"], "files")

            first_rebuild = rebuild_project_projection_db(work_root)
            db_stats = build_project_stats(work_root)
            self.assertEqual(db_stats["projection_source"], "db")
            self.assertEqual(
                db_stats["model_invocation_usage"],
                file_usage,
            )

            run_filtered = build_project_stats(
                work_root,
                filters={"run": POSITIVE_RUN_ID},
            )["model_invocation_usage"]
            self.assertEqual(
                run_filtered["filtered_invocation_digest"],
                file_usage["filtered_invocation_digest"],
            )
            self._assert_positive_usage(run_filtered)

            smoke_filtered = build_project_stats(
                work_root,
                filters={"stage": "development_smoke"},
            )["model_invocation_usage"]
            self.assertEqual(
                smoke_filtered["invocation_count"],
                len(TRACKED_DEVELOPMENT_SMOKES),
            )
            self.assertEqual(
                smoke_filtered["lifecycle_terminal_coverage"],
                _coverage(
                    len(TRACKED_DEVELOPMENT_SMOKES),
                    len(TRACKED_DEVELOPMENT_SMOKES),
                ),
            )
            self.assertEqual(
                smoke_filtered["token_usage_coverage"],
                smoke_filtered["lifecycle_terminal_coverage"],
            )

            second_rebuild = rebuild_project_projection_db(work_root)
            self.assertEqual(
                second_rebuild["invocation_digest"],
                first_rebuild["invocation_digest"],
            )
            self.assertEqual(
                build_project_stats(work_root)["model_invocation_usage"],
                db_stats["model_invocation_usage"],
            )

    def test_negative_recovery_stays_separate_and_never_invents_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_root = root / "negative-work"
            run_dir = work_root / "runs" / NEGATIVE_RUN_ID
            replay_events_path = root / "replay" / "negative-events.jsonl"
            _write_run_identity(work_root, NEGATIVE_RUN_ID)
            path = {
                "path": "crash_after_durable_start",
                "role": "implementation_worker",
                "usage_stage": "implementation_worker",
            }
            lease_id = "LEASE-EXPIRED-BUT-PROCESS-LIVE"
            invocation_id, context = _invocation_context(
                path,
                1,
                run_id=NEGATIVE_RUN_ID,
                lease_id=lease_id,
            )
            lifecycle = InvocationLifecycle(
                run_dir,
                context,
                invocation_id=invocation_id,
                started_at="2026-07-23T01:00:00Z",
            )
            lifecycle.publish_start(_execution_identity(101))
            inflight = {
                "attempt_id": context["attempt_id"],
                "lease_id": lease_id,
                "agent_id": context["agent_id"],
                "lease_expires_at": "2026-07-23T01:00:01Z",
            }

            before = build_project_stats(work_root)[
                "model_invocation_usage"
            ]
            self._assert_negative_usage(
                before,
                lifecycle_covered=0,
                open_invocations=1,
            )

            read_fd, write_fd = os.pipe()
            try:
                live = reconcile_orphaned_invocation(
                    run_dir,
                    inflight,
                    fence_assessor=lambda _start: {
                        "fence_status": "live_pinned",
                        "proof": "pidfd_and_start_ticks_match",
                        "pidfd": read_fd,
                    },
                    service_stopper=lambda _start: self.fail(
                        "live process must not be stopped"
                    ),
                )
            finally:
                os.close(write_fd)
            self.assertEqual(live["reconciliation_status"], "live")
            self.assertFalse(lifecycle.terminal_path.exists())
            self.assertEqual(
                build_project_stats(work_root)["model_invocation_usage"],
                before,
            )

            assessments = iter(
                (
                    {
                        "fence_status": "exact_service_stop_required",
                        "proof": "exact_transient_cgroup_populated",
                    },
                    {
                        "fence_status": "death_proven",
                        "proof": "exact_transient_cgroup_empty",
                    },
                )
            )
            stopped = []
            with mock.patch(
                "agentteam_runtime.model_invocation._utc_now",
                return_value="2026-07-23T01:00:02Z",
            ):
                recovered = reconcile_orphaned_invocation(
                    run_dir,
                    inflight,
                    fence_assessor=lambda _start: next(assessments),
                    service_stopper=lambda start: stopped.append(
                        start["systemd_transient_unit"]
                    )
                    or True,
                )
            self.assertEqual(
                recovered["reconciliation_status"],
                "recovered",
            )
            self.assertEqual(
                recovered["proof"],
                "exact_transient_cgroup_empty",
            )
            self.assertEqual(
                stopped,
                [_execution_identity(101).systemd_transient_unit],
            )
            self.assertTrue(lifecycle.revoked_path.is_file())
            terminal_bytes = lifecycle.terminal_path.read_bytes()
            terminal = json.loads(terminal_bytes)
            self.assertEqual(
                terminal["terminal_status"],
                "recovered_orphan",
            )
            self.assertEqual(terminal["usage_status"], "unavailable")
            self.assertTrue(
                all(terminal[field] is None for field in TOKEN_FIELDS)
            )

            after = build_project_stats(work_root)[
                "model_invocation_usage"
            ]
            self._assert_negative_usage(
                after,
                lifecycle_covered=1,
                open_invocations=0,
            )
            self.assertEqual(
                after["invocation_count"],
                before["invocation_count"],
            )

            replayed_recovery = reconcile_orphaned_invocation(
                run_dir,
                inflight,
                fence_assessor=lambda _start: self.fail(
                    "terminal replay must not reassess or signal"
                ),
                service_stopper=lambda _start: self.fail(
                    "terminal replay must not stop the service"
                ),
            )
            self.assertEqual(
                replayed_recovery["reconciliation_status"],
                "terminal_available",
            )
            self.assertEqual(
                lifecycle.terminal_path.read_bytes(),
                terminal_bytes,
            )
            self.assertEqual(
                len(list(lifecycle.invocation_dir.glob("terminal.json"))),
                1,
            )

            imported = import_model_invocation_lifecycle(
                replay_events_path,
                lifecycle.started_path,
                revoked_path=lifecycle.revoked_path,
                terminal_path=lifecycle.terminal_path,
                actor="phase1-recovery-fixture",
                run_id=NEGATIVE_RUN_ID,
                step_id="P1-06A-NEGATIVE",
                source_root=work_root,
                require_terminal=True,
            )
            self.assertEqual(len(imported), 3)
            first_replay = replay_model_invocation_events(
                replay_events_path
            )
            self.assertEqual(
                import_model_invocation_lifecycle(
                    replay_events_path,
                    lifecycle.started_path,
                    revoked_path=lifecycle.revoked_path,
                    terminal_path=lifecycle.terminal_path,
                    actor="phase1-recovery-fixture",
                    run_id=NEGATIVE_RUN_ID,
                    step_id="P1-06A-NEGATIVE",
                    source_root=work_root,
                    require_terminal=True,
                ),
                [],
            )
            self.assertEqual(
                replay_model_invocation_events(replay_events_path),
                first_replay,
            )
            self.assertEqual(first_replay["invocation_count"], 1)
            self.assertEqual(first_replay["terminal_count"], 1)
            self.assertEqual(first_replay["reported_invocation_count"], 0)
            self.assertEqual(len(first_replay["writer_revocations"]), 1)

            first_rebuild = rebuild_project_projection_db(work_root)
            rebuilt_usage = build_project_stats(work_root)[
                "model_invocation_usage"
            ]
            self.assertEqual(rebuilt_usage, after)
            second_rebuild = rebuild_project_projection_db(work_root)
            self.assertEqual(
                second_rebuild["invocation_digest"],
                first_rebuild["invocation_digest"],
            )
            self.assertEqual(
                build_project_stats(work_root)["model_invocation_usage"],
                rebuilt_usage,
            )

    def test_recovery_fence_matrix_keeps_ambiguous_identity_open(self):
        path = {
            "path": "recovery_fence_matrix",
            "role": "implementation_worker",
            "usage_stage": "implementation_worker",
        }
        _invocation_id, context = _invocation_context(
            path,
            1,
            run_id=NEGATIVE_RUN_ID,
        )
        identity = _execution_identity(201)
        start = {
            **context,
            **{
                field: getattr(identity, field)
                for field in ExecutionGroupIdentity.__dataclass_fields__
            },
        }
        user_service = {
            "InvocationID": start["systemd_user_service_invocation_id"],
            "ControlGroup": start["systemd_user_service_control_group"],
            "KillMode": start["systemd_user_service_kill_mode"],
            "ActiveState": "active",
        }
        transient_service = {
            "InvocationID": start["systemd_transient_invocation_id"],
            "ControlGroup": start["systemd_transient_control_group"],
            "KillMode": start["systemd_transient_kill_mode"],
            "ActiveState": "active",
        }

        changed_boot = assess_execution_group_fence(
            start,
            current_boot_id="87654321-4321-4321-4321-cba987654321",
            current_user_service=None,
            current_manager_identity=None,
            current_transient_service=None,
        )
        self.assertEqual(changed_boot["fence_status"], "death_proven")
        self.assertEqual(changed_boot["proof"], "host_boot_changed")
        self.assertFalse(changed_boot["signal_allowed"])

        enclosing_restart = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service={
                **user_service,
                "InvocationID": "e" * 32,
            },
            current_manager_identity="manager-monotonic:changed",
            current_transient_service=None,
        )
        self.assertEqual(
            enclosing_restart["proof"],
            "enclosing_user_service_restarted",
        )
        self.assertFalse(enclosing_restart["signal_allowed"])

        def reaped_pidfd_open(_pid, _flags):
            raise OSError(errno.ESRCH, "supervisor reaped")

        for populated, expected_status, expected_proof in (
            (
                False,
                "death_proven",
                "exact_transient_cgroup_empty",
            ),
            (
                True,
                "exact_service_stop_required",
                "exact_transient_cgroup_populated",
            ),
        ):
            with self.subTest(populated=populated):
                assessment = assess_execution_group_fence(
                    start,
                    current_boot_id=start["host_boot_id"],
                    current_user_service=user_service,
                    current_manager_identity=start[
                        "systemd_user_manager_identity"
                    ],
                    current_transient_service=transient_service,
                    pidfd_open=reaped_pidfd_open,
                    cgroup_populated=lambda _control_group, value=populated: value,
                )
                self.assertEqual(
                    assessment["fence_status"],
                    expected_status,
                )
                self.assertEqual(assessment["proof"], expected_proof)
                self.assertFalse(assessment["signal_allowed"])

        ambiguous_cases = (
            (
                "manager_changed",
                {
                    "current_user_service": user_service,
                    "current_manager_identity": "manager-monotonic:changed",
                    "current_transient_service": transient_service,
                },
                "user_manager_identity_changed_without_enclosing_restart",
            ),
            (
                "service_identity_changed",
                {
                    "current_user_service": user_service,
                    "current_manager_identity": start[
                        "systemd_user_manager_identity"
                    ],
                    "current_transient_service": {
                        **transient_service,
                        "InvocationID": "d" * 32,
                    },
                },
                "transient_service_identity_mismatch",
            ),
        )
        for label, supplied, proof in ambiguous_cases:
            with self.subTest(label=label):
                assessment = assess_execution_group_fence(
                    start,
                    current_boot_id=start["host_boot_id"],
                    **supplied,
                )
                self.assertEqual(
                    assessment["fence_status"],
                    "open_ambiguous",
                )
                self.assertEqual(assessment["proof"], proof)
                self.assertFalse(assessment["signal_allowed"])

    def _assert_positive_usage(self, usage):
        invocation_count = len(POSITIVE_PATH_MATRIX)
        self.assertEqual(usage["invocation_count"], invocation_count)
        self.assertEqual(
            usage["supported_invocation_count"],
            invocation_count,
        )
        self.assertEqual(
            usage["terminal_invocation_count"],
            invocation_count,
        )
        self.assertEqual(usage["open_invocations"], 0)
        self.assertEqual(
            usage["lifecycle_terminal_coverage"],
            _coverage(invocation_count, invocation_count),
        )
        self.assertEqual(
            usage["token_usage_coverage"],
            _coverage(invocation_count, invocation_count),
        )
        self.assertEqual(
            {
                stage: counts["invocation_count"]
                for stage, counts in usage["stage_breakdown"].items()
            },
            POSITIVE_STAGE_COUNTS,
        )
        self.assertEqual(
            {
                field: usage["reported_token_totals"][field]
                for field in TOKEN_FIELDS
            },
            POSITIVE_TOKEN_TOTALS,
        )
        self.assertEqual(
            usage["reported_token_totals"][
                "contributing_invocation_count"
            ],
            invocation_count,
        )
        self.assertEqual(
            usage["partial_known_token_lower_bounds"],
            {
                **{field: None for field in TOKEN_FIELDS},
                "contributing_invocation_count": 0,
            },
        )
        self.assertEqual(usage["completion_status"], "complete")
        self.assertTrue(usage["benchmark_ready"])

    def _assert_negative_usage(
        self,
        usage,
        *,
        lifecycle_covered,
        open_invocations,
    ):
        self.assertEqual(usage["invocation_count"], 1)
        self.assertEqual(usage["supported_invocation_count"], 1)
        self.assertEqual(usage["open_invocations"], open_invocations)
        self.assertEqual(
            usage["lifecycle_terminal_coverage"],
            _coverage(lifecycle_covered, 1),
        )
        self.assertEqual(
            usage["token_usage_coverage"],
            _coverage(0, 1),
        )
        self.assertEqual(
            {
                field: usage["reported_token_totals"][field]
                for field in TOKEN_FIELDS
            },
            {field: None for field in TOKEN_FIELDS},
        )
        self.assertEqual(
            usage["reported_token_totals"][
                "contributing_invocation_count"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()

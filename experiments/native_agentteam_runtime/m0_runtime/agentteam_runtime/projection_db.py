import json
import hashlib
import os
import re
import sqlite3
from pathlib import Path

from .experiment_results import (
    ExperimentResultError,
    load_experiment_result_bundle,
    load_latest_experiment_recovery_snapshot,
    validate_experiment_recovery_snapshot,
    validate_experiment_result_bundle,
)
from .token_usage import normalize_token_usage, token_usage_from_state

PROJECTION_SCHEMA_VERSION = "agentteam_projection.v6"
PROJECTION_WARNING_UNAVAILABLE = "projection_db_unavailable"
PROJECTION_REBUILD_NEXT_ACTION = "run agentteam db rebuild"
PROJECTION_REBUILD_HINT = "agentteam db rebuild"
_RUN_NAMESPACE_PATTERN = re.compile(r"^v[1-9][0-9]*$")
_INVOCATION_EVENT_TYPES = {
    "model_invocation_started",
    "model_invocation_usage_recorded",
}
_INVOCATION_FILTER_FIELDS = {
    "run": "run_id",
    "run_id": "run_id",
    "run_kind": "run_kind",
    "implementation_run": "implementation_run_id",
    "implementation_run_id": "implementation_run_id",
    "gate_epoch": "gate_epoch",
    "pursue": "pursue_id",
    "pursue_id": "pursue_id",
    "round": "round_index",
    "round_index": "round_index",
    "stage": "usage_stage",
    "usage_stage": "usage_stage",
    "role": "role",
    "task": "task_id",
    "task_id": "task_id",
    "attempt": "attempt_id",
    "attempt_id": "attempt_id",
    "backend": "backend",
    "model": "model",
}
_INVOCATION_COLUMNS = (
    "invocation_id",
    "usage_event_id",
    "start_schema_version",
    "usage_schema_version",
    "project",
    "run_id",
    "run_kind",
    "implementation_run_id",
    "gate_epoch",
    "pursue_id",
    "round_index",
    "taskpack_id",
    "task_id",
    "attempt_id",
    "runtime_execution_session_id",
    "provider_session_id",
    "provider_predecessor_invocation_id",
    "provider_turn_id",
    "provider_predecessor_turn_id",
    "agent_id",
    "role",
    "usage_stage",
    "backend",
    "model",
    "coverage_class",
    "lifecycle_status",
    "terminal_status",
    "usage_status",
    "usage_source",
    "provider_usage_scope",
    "accounting_method",
    "provider_usage_snapshot_json",
    "unavailable_reason",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "started_at",
    "finished_at",
    "wall_time_seconds",
    "source_artifact_path",
    "start_record_sha256",
    "terminal_record_sha256",
    "record_sha256",
)


class ProjectionIntegrityError(RuntimeError):
    """Authoritative invocation files disagree and cannot be projected safely."""


def project_projection_db_path(work_root):
    return Path(work_root).resolve() / "agentteam.db"


def rebuild_project_projection_db(work_root):
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    db_path = project_projection_db_path(work_root)
    temp_path = db_path.with_suffix(".db.tmp")
    projection = _scan_work_root(work_root)
    _write_projection_database(temp_path, projection)
    os.replace(temp_path, db_path)
    return {
        "db_status": "rebuilt",
        "db_path": str(db_path),
        "schema_version": PROJECTION_SCHEMA_VERSION,
        **_projection_counts(projection),
    }


def build_isolated_acceptance_projection(
    work_root,
    acceptance_artifact,
    db_path,
    *,
    filters=None,
):
    """Project one staged acceptance artifact without making it file authority."""
    work_root = Path(work_root).resolve()
    db_path = Path(db_path).resolve()
    if db_path == project_projection_db_path(work_root):
        raise ValueError("isolated projection DB must not replace the project projection DB")
    artifact, artifact_path = _load_explicit_acceptance_artifact(
        acceptance_artifact
    )
    projection = _scan_work_root(
        work_root,
        explicit_acceptance_artifacts=[(artifact, artifact_path)],
    )
    _write_projection_database(db_path, projection)
    invocation_rows = projection["invocations"]
    applied_filters = _normalize_invocation_filters(filters or {})
    filtered_rows = _filter_invocation_rows(invocation_rows, applied_filters)
    return {
        "projection_status": "isolated",
        "projection_source": "files_plus_explicit_staged_acceptance",
        "db_path": str(db_path),
        "schema_version": PROJECTION_SCHEMA_VERSION,
        **_projection_counts(projection),
        "model_invocation_usage": _aggregate_invocation_rows(
            filtered_rows,
            applied_filters=applied_filters,
            all_rows=invocation_rows,
        ),
    }


def project_isolated_acceptance_artifact(
    work_root,
    acceptance_artifact,
    db_path,
    *,
    filters=None,
):
    """Compatibility spelling for the isolated controller validation API."""
    return build_isolated_acceptance_projection(
        work_root,
        acceptance_artifact,
        db_path,
        filters=filters,
    )


def _write_projection_database(db_path, projection):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    try:
        with sqlite3.connect(db_path) as connection:
            _create_projection_schema(connection)
            _write_projection_rows(connection, projection)
    except BaseException:
        if db_path.exists():
            db_path.unlink()
        raise


def _load_explicit_acceptance_artifact(acceptance_artifact):
    if isinstance(acceptance_artifact, dict):
        payload = dict(acceptance_artifact)
        source_path = "<explicit-staged-acceptance>"
    else:
        path = Path(acceptance_artifact).resolve()
        payload = _read_json_if_exists(path)
        source_path = str(path)
        if not isinstance(payload, dict) or not payload:
            raise ProjectionIntegrityError(
                f"staged acceptance artifact is missing or invalid: {path}"
            )
    if payload.get("schema_version") != "model_invocation_live_smoke.v1":
        raise ProjectionIntegrityError(
            "staged acceptance artifact has an invalid schema version"
        )
    return payload, source_path


def check_project_projection_db(work_root):
    work_root = Path(work_root).resolve()
    db_path = project_projection_db_path(work_root)
    expected = _projection_counts(_scan_work_root(work_root))
    if not db_path.exists():
        return _with_projection_contract({
            "check_status": "failed",
            "db_path": str(db_path),
            "schema_version": None,
            "expected": expected,
            "actual": {},
            "mismatches": ["db_missing"],
        })
    try:
        actual = _database_counts(db_path)
        schema_version = _database_schema_version(db_path)
    except (sqlite3.DatabaseError, ProjectionIntegrityError) as exc:
        return _with_projection_contract({
            "check_status": "failed",
            "db_path": str(db_path),
            "schema_version": None,
            "expected": expected,
            "actual": {},
            "mismatches": ["db_unreadable"],
            "error": str(exc),
        })
    mismatch_keys = [
        "runs",
        "taskpacks",
        "events",
        "tasks",
        "evidence_summaries",
        "artifacts",
        "artifact_bytes",
        "artifact_digest",
        "run_stats",
        "follow_up_items",
        "worker_results",
        "integration_outcomes",
        "worker_verification_additions",
        "invocations",
        "invocation_digest",
        "experiment_results",
        "experiment_result_digest",
        "experiment_recovery",
        "experiment_recovery_digest",
    ]
    mismatches = [
        key
        for key in mismatch_keys
        if expected.get(key) != actual.get(key)
    ]
    if schema_version != PROJECTION_SCHEMA_VERSION:
        mismatches.append("schema_version")
    return _with_projection_contract({
        "check_status": "failed" if mismatches else "passed",
        "db_path": str(db_path),
        "schema_version": schema_version,
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
    })


def _with_projection_contract(status):
    status = dict(status)
    projection_status = _projection_status(status)
    status["projection_status"] = projection_status
    status["projection_source"] = "db" if projection_status == "fresh" else "files"
    status["projection_db_path"] = status.get("db_path")
    if projection_status != "fresh":
        status["projection_warning"] = PROJECTION_WARNING_UNAVAILABLE
        status["next_action"] = PROJECTION_REBUILD_NEXT_ACTION
        status["operator_hint"] = PROJECTION_REBUILD_HINT
    return status


def _projection_status(status):
    if status.get("check_status") == "passed":
        return "fresh"
    mismatches = set(status.get("mismatches") or [])
    if "db_missing" in mismatches:
        return "missing"
    if "db_unreadable" in mismatches:
        return "corrupt"
    return "stale"


def _projection_reader_db_metadata(check, db_path):
    db_path = str(db_path)
    return {
        "projection_source": "db",
        "projection_status": check.get("projection_status") or "fresh",
        "projection_db_path": check.get("projection_db_path") or db_path,
        "db_path": db_path,
        "check_status": check.get("check_status"),
        "check": check,
    }


def _projection_reader_fallback_status(check):
    payload = {
        "projection_source": "files",
        "projection_status": check.get("projection_status"),
        "projection_warning": check.get("projection_warning"),
        "projection_db_path": check.get("projection_db_path") or check.get("db_path"),
        "db_path": check.get("db_path") or check.get("projection_db_path"),
        "check_status": check.get("check_status"),
        "fallback_required": True,
        "check": check,
    }
    if check.get("next_action"):
        payload["next_action"] = check["next_action"]
    if check.get("operator_hint"):
        payload["operator_hint"] = check["operator_hint"]
    return payload


def _projection_query_failed_status(check, exc):
    return _with_projection_contract({
        "check_status": "failed",
        "db_path": check.get("db_path") or check.get("projection_db_path"),
        "schema_version": check.get("schema_version"),
        "expected": check.get("expected", {}),
        "actual": check.get("actual", {}),
        "mismatches": ["db_unreadable"],
        "error": str(exc),
    })


def _projection_reader_fallback(check, include_fallback_status):
    if include_fallback_status:
        return _projection_reader_fallback_status(check)
    return None


def read_projected_taskpacks(work_root, *, include_fallback_status=False):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return _projection_reader_fallback(check, include_fallback_status)
    db_path = project_projection_db_path(work_root)
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                """
                select taskpack_id, taskpack_dir, metadata_path, goal, validation_status
                from taskpacks
                order by taskpack_id
                """
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        return _projection_reader_fallback(
            _projection_query_failed_status(check, exc),
            include_fallback_status,
        )
    return {
        **_projection_reader_db_metadata(check, db_path),
        "taskpacks": [
            {
                "taskpack_id": row[0],
                "frozen_dir": row[1],
                "metadata_path": row[2],
                "goal": row[3],
                "validation_status": row[4],
            }
            for row in rows
        ],
    }


def read_projected_run_events(work_root, run_id, *, include_fallback_status=False):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return _projection_reader_fallback(check, include_fallback_status)
    db_path = project_projection_db_path(work_root)
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                """
                select event_json
                from events
                where run_id = ?
                order by sequence
                """,
                (run_id,),
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        return _projection_reader_fallback(
            _projection_query_failed_status(check, exc),
            include_fallback_status,
        )
    events = []
    for row in rows:
        try:
            events.append(json.loads(row[0]))
        except (TypeError, json.JSONDecodeError):
            continue
    return {
        **_projection_reader_db_metadata(check, db_path),
        "events": events,
    }


def read_projected_follow_up_lineage(work_root, *, include_fallback_status=False):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return _projection_reader_fallback(check, include_fallback_status)
    db_path = project_projection_db_path(work_root)
    try:
        with sqlite3.connect(db_path) as connection:
            follow_up_rows = connection.execute(
                """
                select follow_up_id, source_run_id, source_taskpack_id, item_index,
                       objective, queue_source, readiness, source_report_path,
                       goal_memory_path, source_result_status, source_run_outcome,
                       stop_reason, recommended_next_step, suggested_verification,
                       source_evidence_paths_json, blockers_json, token_usage_json,
                       selected_next_goal
                from follow_up_items
                order by goal_memory_path, item_index, follow_up_id
                """
            ).fetchall()
            worker_rows = connection.execute(
                """
                select run_id, task_id, attempt_id, result_status,
                       validation_status, failure_category, patch_path,
                       changed_files_json, operator_summary_json,
                       verification_additions_json, verification_additions_count,
                       evidence_level, evidence_status, trace_carrier_json,
                       missing_evidence_json, source_path, content_size_bytes,
                       content_sha256
                from worker_results
                order by run_id, task_id, attempt_id
                """
            ).fetchall()
            integration_rows = connection.execute(
                """
                select integration_outcome_id, run_id, task_id, attempt_id,
                       queue_item_id, queue_status, integration_status,
                       integration_branch, integration_worktree_path,
                       integration_verification_status,
                       integration_verification_exit_code,
                       integration_verification_additions_status,
                       integration_verification_additions_json,
                       integration_verification_additions_count,
                       integration_commit_status, integration_commit_sha,
                       patch_path, batch_id, source_kinds_json,
                       content_size_bytes, content_sha256
                from integration_outcomes
                order by run_id, task_id, attempt_id, batch_id
                """
            ).fetchall()
            addition_rows = connection.execute(
                """
                select verification_addition_id, run_id, task_id, attempt_id,
                       source_kind, label, command_json, reason,
                       verification_addition_status,
                       verification_addition_exit_code,
                       verification_addition_rejection_reason
                from worker_verification_additions
                order by run_id, task_id, attempt_id, source_kind, label
                """
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        return _projection_reader_fallback(
            _projection_query_failed_status(check, exc),
            include_fallback_status,
        )

    additions = [_verification_addition_payload(row) for row in addition_rows]
    worker_additions = _verification_additions_by_attempt(additions, "worker_output")
    integration_additions = _verification_additions_by_attempt(
        additions,
        "integration_verification",
    )
    return {
        **_projection_reader_db_metadata(check, db_path),
        "follow_up_items": [_follow_up_item_payload(row) for row in follow_up_rows],
        "worker_results": [
            {
                **_worker_result_payload(row),
                "verification_additions": worker_additions.get(
                    (row[0], row[1], row[2]),
                    [],
                ),
            }
            for row in worker_rows
        ],
        "integration_outcomes": [
            {
                **_integration_outcome_payload(row),
                "verification_additions": integration_additions.get(
                    (row[1], row[2], row[3]),
                    [],
                ),
            }
            for row in integration_rows
        ],
        "worker_verification_additions": additions,
    }


def _follow_up_item_payload(row):
    return {
        "follow_up_id": row[0],
        "source_run_id": row[1],
        "source_taskpack_id": row[2],
        "item_index": row[3],
        "objective": row[4],
        "queue_source": row[5],
        "readiness": row[6],
        "source_report_path": row[7],
        "goal_memory_path": row[8],
        "source_result_status": row[9],
        "source_run_outcome": row[10],
        "stop_reason": row[11],
        "recommended_next_step": row[12],
        "suggested_verification": row[13],
        "source_evidence_paths": _json_value(row[14], []),
        "blockers": _json_value(row[15], []),
        "token_usage": _json_value(row[16], {}),
        "selected_next_goal": bool(row[17]),
    }


def _worker_result_payload(row):
    return {
        "run_id": row[0],
        "task_id": row[1],
        "attempt_id": row[2],
        "result_status": row[3],
        "validation_status": row[4],
        "failure_category": row[5],
        "patch_path": row[6],
        "changed_files": _json_value(row[7], []),
        "operator_summary": _json_value(row[8], {}),
        "declared_verification_additions": _json_value(row[9], []),
        "verification_additions_count": row[10],
        "evidence_level": row[11],
        "evidence_status": row[12],
        "trace_carrier": _json_value(row[13], []),
        "missing_evidence": _json_value(row[14], []),
        "source_path": row[15],
        "content_size_bytes": row[16],
        "content_sha256": row[17],
    }


def _integration_outcome_payload(row):
    return {
        "integration_outcome_id": row[0],
        "run_id": row[1],
        "task_id": row[2],
        "attempt_id": row[3],
        "queue_item_id": row[4],
        "queue_status": row[5],
        "integration_status": row[6],
        "integration_branch": row[7],
        "integration_worktree_path": row[8],
        "integration_verification_status": row[9],
        "integration_verification_exit_code": row[10],
        "integration_verification_additions_status": row[11],
        "declared_verification_additions": _json_value(row[12], []),
        "integration_verification_additions_count": row[13],
        "integration_commit_status": row[14],
        "integration_commit_sha": row[15],
        "patch_path": row[16],
        "batch_id": row[17],
        "source_kinds": _json_value(row[18], []),
        "content_size_bytes": row[19],
        "content_sha256": row[20],
    }


def _verification_addition_payload(row):
    return {
        "verification_addition_id": row[0],
        "run_id": row[1],
        "task_id": row[2],
        "attempt_id": row[3],
        "source_kind": row[4],
        "label": row[5],
        "command": _json_value(row[6], []),
        "reason": row[7],
        "verification_addition_status": row[8],
        "verification_addition_exit_code": row[9],
        "verification_addition_rejection_reason": row[10],
    }


def _verification_additions_by_attempt(additions, source_kind):
    grouped = {}
    for addition in additions:
        if addition.get("source_kind") != source_kind:
            continue
        key = (addition.get("run_id"), addition.get("task_id"), addition.get("attempt_id"))
        grouped.setdefault(key, []).append(addition)
    return grouped


def read_projected_run_metadata(work_root, run_id):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return None
    db_path = project_projection_db_path(work_root)
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                """
                select
                    run_id,
                    run_dir,
                    run_status,
                    scheduler_status,
                    event_count,
                    latest_event_sequence,
                    latest_event_type,
                    latest_event_time,
                    state_path,
                    events_path,
                    report_path
                from runs
                where run_id = ?
                """,
                (run_id,),
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    return {
        "projection_source": "db",
        "db_path": str(db_path),
        "check": check,
        "run": {
            "run_id": row[0],
            "run_dir": row[1],
            "run_status": row[2],
            "scheduler_status": row[3],
            "event_count": row[4],
            "latest_event_sequence": row[5],
            "latest_event_type": row[6],
            "latest_event_time": row[7],
            "state_path": row[8],
            "events_path": row[9],
            "report_path": row[10],
        },
    }


def read_projected_experiment_results(work_root):
    """Read sealed experiment bundles from a fresh DB or authoritative files."""
    work_root = Path(work_root).resolve()
    check = check_project_projection_db(work_root)
    if check["check_status"] == "passed":
        try:
            with sqlite3.connect(
                project_projection_db_path(work_root)
            ) as connection:
                rows = connection.execute(
                    """
                    select bundle_sha256, bundle_json
                    from experiment_results
                    order by experiment_run_id
                    """
                ).fetchall()
            return [
                {
                    "bundle_sha256": digest,
                    "bundle": json.loads(bundle_json),
                    "projection_source": "db",
                }
                for digest, bundle_json in rows
            ]
        except (sqlite3.DatabaseError, json.JSONDecodeError):
            pass
    return [
        {
            "bundle_sha256": item["bundle_sha256"],
            "bundle": item["bundle"],
            "projection_source": "files",
        }
        for item in _scan_work_root(work_root)["experiment_results"]
    ]


def read_projected_experiment_recovery(work_root):
    """Read latest recovery snapshots without making SQLite authority."""
    work_root = Path(work_root).resolve()
    check = check_project_projection_db(work_root)
    if check["check_status"] == "passed":
        try:
            with sqlite3.connect(
                project_projection_db_path(work_root)
            ) as connection:
                rows = connection.execute(
                    """
                    select snapshot_sha256, snapshot_json, resumable
                    from experiment_recovery
                    order by experiment_run_id
                    """
                ).fetchall()
            return [
                {
                    "snapshot_sha256": digest,
                    "snapshot": json.loads(snapshot_json),
                    "resumable": bool(resumable),
                    "projection_source": "db",
                }
                for digest, snapshot_json, resumable in rows
            ]
        except (sqlite3.DatabaseError, json.JSONDecodeError):
            pass
    return [
        {
            "snapshot_sha256": item["snapshot_sha256"],
            "snapshot": item["snapshot"],
            "resumable": item["resumable"],
            "projection_source": "files",
        }
        for item in _scan_work_root(work_root)["experiment_recovery"]
    ]


def read_projected_artifact_summary(work_root):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return None
    db_path = project_projection_db_path(work_root)
    try:
        with sqlite3.connect(db_path) as connection:
            artifact_type_rows = connection.execute(
                """
                select artifact_type, count(*), coalesce(sum(size_bytes), 0)
                from artifacts
                group by artifact_type
                order by artifact_type
                """
            ).fetchall()
            retention_rows = connection.execute(
                """
                select retention_policy, count(*), coalesce(sum(size_bytes), 0)
                from artifacts
                group by retention_policy
                order by retention_policy
                """
            ).fetchall()
            stats_rows = connection.execute(
                """
                select run_id, total_tokens, input_tokens, output_tokens,
                       cached_input_tokens, reasoning_tokens, token_usage_status
                from run_stats
                order by run_id
                """
            ).fetchall()
    except sqlite3.DatabaseError:
        return None
    total_artifacts = sum(row[1] for row in artifact_type_rows)
    total_bytes = sum(row[2] for row in artifact_type_rows)
    return {
        "projection_source": "db",
        "db_path": str(db_path),
        "check": check,
        "check_status": check["check_status"],
        "total_artifacts": total_artifacts,
        "total_bytes": total_bytes,
        "artifact_types": {
            row[0]: {"count": row[1], "bytes": row[2]}
            for row in artifact_type_rows
        },
        "retention_policies": {
            row[0]: row[1]
            for row in retention_rows
        },
        "retention_bytes": {
            row[0]: row[2]
            for row in retention_rows
        },
        "run_token_usage": [
            {
                "run_id": row[0],
                "total_tokens": row[1],
                "input_tokens": row[2],
                "output_tokens": row[3],
                "cached_input_tokens": row[4],
                "reasoning_tokens": row[5],
                "token_usage_status": row[6],
            }
            for row in stats_rows
        ],
    }


def read_projected_artifact_retention_plan(work_root, limit=20):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return None
    db_path = project_projection_db_path(work_root)
    limit = max(0, int(limit if limit is not None else 20))
    try:
        with sqlite3.connect(db_path) as connection:
            retention_rows = connection.execute(
                """
                select retention_policy, count(*), coalesce(sum(size_bytes), 0)
                from artifacts
                group by retention_policy
                order by retention_policy
                """
            ).fetchall()
            candidate_total = connection.execute(
                """
                select count(*), coalesce(sum(size_bytes), 0)
                from artifacts
                where retention_policy = 'rebuildable'
                """
            ).fetchone()
            candidate_rows = connection.execute(
                """
                select artifact_type, run_id, taskpack_id, task_id, attempt_id,
                       path, size_bytes, sha256, retention_policy
                from artifacts
                where retention_policy = 'rebuildable'
                order by size_bytes desc, path
                limit ?
                """,
                (limit,),
            ).fetchall()
            validation_rows = connection.execute(
                """
                select artifact_type, run_id, taskpack_id, task_id, attempt_id,
                       path, size_bytes, sha256, retention_policy
                from artifacts
                where retention_policy = 'rebuildable'
                order by size_bytes desc, path
                """
            ).fetchall()
    except sqlite3.DatabaseError:
        return None
    validation = _validate_retention_candidate_rows(validation_rows)
    return {
        "projection_source": "db",
        "plan_status": "ready",
        "db_path": str(db_path),
        "check_status": check["check_status"],
        "deletion_enabled": False,
        "validation_status": validation["validation_status"],
        "validated_candidate_count": validation["validated_candidate_count"],
        "invalid_candidate_count": validation["invalid_candidate_count"],
        "invalid_candidates": validation["invalid_candidates"],
        "candidate_count": candidate_total[0] if candidate_total else 0,
        "candidate_bytes": candidate_total[1] if candidate_total else 0,
        "candidate_limit": limit,
        "retention_policies": {
            row[0]: row[1]
            for row in retention_rows
        },
        "retention_bytes": {
            row[0]: row[2]
            for row in retention_rows
        },
        "protected_explanations": _artifact_retention_explanations(),
        "candidates": [
            {
                "artifact_type": row[0],
                "run_id": row[1],
                "taskpack_id": row[2],
                "task_id": row[3],
                "attempt_id": row[4],
                "path": row[5],
                "size_bytes": row[6],
                "sha256": row[7],
                "retention_policy": row[8],
                "reason": "derived context artifact; listed for planning only, not deletion",
                "validation": _retention_candidate_validation(row),
            }
            for row in candidate_rows
        ],
    }


def _validate_retention_candidate_rows(rows):
    validations = [_retention_candidate_validation(row) for row in rows]
    invalid = [item for item in validations if item["status"] != "passed"]
    return {
        "validation_status": "failed" if invalid else "passed",
        "validated_candidate_count": len(validations),
        "invalid_candidate_count": len(invalid),
        "invalid_candidates": invalid,
    }


def _retention_candidate_validation(row):
    path = Path(row[5])
    expected_size = row[6]
    expected_sha256 = row[7]
    exists = path.is_file()
    actual_size = None
    actual_sha256 = None
    if exists:
        payload = path.read_bytes()
        actual_size = len(payload)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
    size_matches = exists and actual_size == expected_size
    sha256_matches = exists and actual_sha256 == expected_sha256
    status = "passed" if exists and size_matches and sha256_matches else "failed"
    return {
        "status": status,
        "artifact_type": row[0],
        "run_id": row[1],
        "taskpack_id": row[2],
        "task_id": row[3],
        "attempt_id": row[4],
        "path": row[5],
        "exists": exists,
        "size_matches": size_matches,
        "sha256_matches": sha256_matches,
        "expected_size_bytes": expected_size,
        "actual_size_bytes": actual_size,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
    }


def build_project_stats(work_root, *, filters=None, **filter_values):
    work_root = Path(work_root).resolve()
    applied_filters = _normalize_invocation_filters(
        {**(filters or {}), **filter_values}
    )
    check = check_project_projection_db(work_root)
    if check["check_status"] == "passed":
        stats = _project_stats_from_database(
            work_root,
            check,
            applied_filters=applied_filters,
        )
        if stats is not None:
            return stats
    projection = _scan_work_root(work_root)
    return _project_stats_from_projection(
        projection,
        projection_source="files",
        check_status=check.get("check_status"),
        db_path=str(project_projection_db_path(work_root)),
        projection_warning="projection_db_unavailable",
        next_action="run agentteam db rebuild",
        applied_filters=applied_filters,
    )


def _scan_work_root(work_root, *, explicit_acceptance_artifacts=None):
    runs = _scan_runs(work_root / "runs")
    taskpacks = _scan_taskpacks(work_root / "frozen")
    artifacts = _scan_artifacts(work_root, runs, taskpacks)
    worker_results = _scan_worker_results(runs)
    integration_outcomes = _scan_integration_outcomes(runs)
    return {
        "runs": runs,
        "taskpacks": taskpacks,
        "artifacts": artifacts,
        "run_stats": _run_stats(runs, artifacts),
        "follow_up_items": _scan_follow_up_items(work_root, runs),
        "worker_results": worker_results,
        "integration_outcomes": integration_outcomes,
        "worker_verification_additions": _scan_worker_verification_additions(
            worker_results,
            integration_outcomes,
        ),
        "invocations": _scan_invocations(
            work_root,
            runs,
            explicit_acceptance_artifacts=explicit_acceptance_artifacts,
        ),
        "experiment_results": _scan_experiment_results(runs),
        "experiment_recovery": _scan_experiment_recovery(runs),
    }


def _scan_experiment_results(runs):
    results = []
    for run in runs:
        run_dir = Path(run["run_dir"])
        result_dir = run_dir / "results" / "terminal"
        if not result_dir.exists():
            continue
        try:
            sealed = load_experiment_result_bundle(run_dir)
        except ExperimentResultError as exc:
            raise ProjectionIntegrityError(
                f"sealed experiment result is invalid: {run_dir}"
            ) from exc
        bundle = sealed["bundle"]
        results.append(
            {
                "experiment_run_id": bundle["experiment_run_id"],
                "run_dir": str(run_dir),
                "protocol_sha256": bundle["protocol_sha256"],
                "run_manifest_sha256": bundle["run_manifest_sha256"],
                "mode": bundle["mode"],
                "repetition_index": bundle["repetition_index"],
                "terminal_status": bundle["terminal_status"],
                "acceptance_status": bundle["acceptance_result"]["status"],
                "usage_totals": bundle["usage_totals"],
                "usage_coverage": bundle["usage_coverage"],
                "budget_result": bundle["budget_result"],
                "operator_action_counts": bundle[
                    "operator_action_counts"
                ],
                "artifact_bytes_written": bundle[
                    "artifact_bytes_written"
                ],
                "raw_spool_bytes_written": bundle[
                    "raw_spool_bytes_written"
                ],
                "projection_identity_sha256": bundle[
                    "projection_reconciliation"
                ]["identity_sha256"],
                "bundle_sha256": sealed["bundle_sha256"],
                "bundle": bundle,
            }
        )
    return sorted(
        results,
        key=lambda item: item["experiment_run_id"],
    )


def _scan_experiment_recovery(runs):
    snapshots = []
    for run in runs:
        run_dir = Path(run["run_dir"])
        try:
            latest = load_latest_experiment_recovery_snapshot(run_dir)
        except ExperimentResultError as exc:
            raise ProjectionIntegrityError(
                f"experiment recovery authority is invalid: {run_dir}"
            ) from exc
        if latest is None:
            continue
        snapshot = latest["snapshot"]
        snapshots.append(
            {
                "experiment_run_id": snapshot["experiment_run_id"],
                "run_dir": str(run_dir),
                "snapshot_sequence": snapshot["snapshot_sequence"],
                "snapshot_sha256": latest["snapshot_sha256"],
                "controller_status": snapshot["controller_status"],
                "resume_phase": snapshot["resume_phase"],
                "resumable": latest["resumable"],
                "snapshot": snapshot,
            }
        )
    return sorted(
        snapshots,
        key=lambda item: item["experiment_run_id"],
    )


def _scan_runs(runs_root):
    if not runs_root.exists():
        return []
    runs = []
    for run_dir in _projection_run_directories(runs_root):
        events_path = run_dir / "events.jsonl"
        state_path = run_dir / "state" / "two_phase_scheduler_state.json"
        events = _read_jsonl(events_path)
        state = _read_json_if_exists(state_path)
        latest_event = _latest_event(events)
        runs.append(
            {
                "run_id": run_dir.name,
                "run_dir": str(run_dir.resolve()),
                "state_path": str(state_path.resolve()) if state_path.exists() else None,
                "events_path": str(events_path.resolve()) if events_path.exists() else None,
                "report_path": _run_report_path(run_dir),
                "run_status": _run_status(latest_event, state),
                "scheduler_status": state.get("scheduler_status"),
                "event_count": len(events),
                "latest_event_sequence": latest_event.get("sequence") if latest_event else None,
                "latest_event_type": latest_event.get("event_type") if latest_event else None,
                "latest_event_time": latest_event.get("time") if latest_event else None,
                "events": [
                    _event_projection(run_dir.name, event)
                    for event in events
                ],
                "raw_events": events,
                "state": state,
                "tasks": _task_projections(run_dir.name, state, events),
                "evidence_summaries": _evidence_projections(run_dir.name, state, state_path),
                "token_usage": _run_token_usage(events, state),
            }
        )
    return runs


def _projection_run_directories(runs_root):
    candidates = []
    for child in sorted(
        (path for path in Path(runs_root).iterdir() if path.is_dir()),
        key=lambda path: path.name,
    ):
        identity_path = child / "state" / "run_identity.v1.json"
        if (
            _RUN_NAMESPACE_PATTERN.fullmatch(child.name)
            and not identity_path.is_file()
        ):
            candidates.extend(
                sorted(
                    (path for path in child.iterdir() if path.is_dir()),
                    key=lambda path: path.name,
                )
            )
        else:
            candidates.append(child)

    selected_implementations = {}
    passthrough = []
    for run_dir in candidates:
        identity = _read_json_if_exists(
            run_dir / "state" / "run_identity.v1.json"
        )
        sequence = identity.get("creation_sequence")
        if (
            identity.get("schema_version") == "run_identity.v1"
            and identity.get("run_kind") == "implementation"
            and identity.get("run_id") == run_dir.name
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence >= 1
        ):
            current = selected_implementations.get(run_dir.name)
            if (
                current is None
                or sequence > current["creation_sequence"]
            ):
                selected_implementations[run_dir.name] = {
                    "creation_sequence": sequence,
                    "run_dir": run_dir,
                }
            continue
        passthrough.append(run_dir)
    selected = passthrough + [
        item["run_dir"] for item in selected_implementations.values()
    ]
    return sorted(selected, key=lambda path: str(path.resolve()))


def _scan_invocations(
    work_root,
    runs,
    *,
    explicit_acceptance_artifacts=None,
):
    starts = {}
    terminals_by_event = {}
    terminals_by_invocation = {}
    acceptance_metadata = {}

    for run in runs:
        source_path = run.get("events_path") or str(
            Path(run["run_dir"]) / "events.jsonl"
        )
        for event in run.get("raw_events", []):
            event_type = event.get("event_type")
            if event_type not in _INVOCATION_EVENT_TYPES:
                continue
            record = event.get("payload")
            if not isinstance(record, dict):
                continue
            if event_type == "model_invocation_started":
                _register_invocation_start(starts, record, source_path)
            else:
                _register_invocation_terminal(
                    terminals_by_event,
                    terminals_by_invocation,
                    record,
                    source_path,
                )

    for started_path in _authoritative_started_paths(work_root):
        start = _read_json_if_exists(started_path)
        if not start:
            continue
        _register_invocation_start(starts, start, str(started_path.resolve()))
        terminal_path = started_path.with_name("terminal.json")
        terminal = _read_json_if_exists(terminal_path)
        if terminal:
            _register_invocation_terminal(
                terminals_by_event,
                terminals_by_invocation,
                terminal,
                str(terminal_path.resolve()),
            )

    acceptance_artifacts = list(_passed_acceptance_artifacts(work_root))
    acceptance_artifacts.extend(explicit_acceptance_artifacts or [])
    for artifact, artifact_path in acceptance_artifacts:
        start = artifact.get("invocation_start_record")
        terminal = artifact.get("invocation_usage_record")
        if not isinstance(start, dict):
            raise ProjectionIntegrityError(
                f"acceptance artifact has no invocation start: {artifact_path}"
            )
        _validate_acceptance_artifact_correlation(
            artifact,
            start,
            terminal,
            artifact_path,
        )
        _register_invocation_start(starts, start, artifact_path)
        if isinstance(terminal, dict):
            _register_invocation_terminal(
                terminals_by_event,
                terminals_by_invocation,
                terminal,
                artifact_path,
            )
        invocation_id = start.get("invocation_id")
        metadata = {
            key: artifact.get(key)
            for key in (
                "project",
                "run_id",
                "run_kind",
                "taskpack_id",
                "implementation_run_id",
                "gate_epoch",
            )
            if artifact.get(key) is not None
        }
        existing = acceptance_metadata.get(invocation_id)
        if existing is not None and existing != metadata:
            raise ProjectionIntegrityError(
                f"conflicting acceptance metadata for invocation {invocation_id}"
            )
        acceptance_metadata[invocation_id] = metadata

    orphaned = sorted(set(terminals_by_invocation) - set(starts))
    if orphaned:
        raise ProjectionIntegrityError(
            "terminal invocation has no authoritative start: "
            + ", ".join(orphaned)
        )

    run_identities = _scan_run_identities(work_root)
    rows = []
    for invocation_id in sorted(starts):
        start_entry = starts[invocation_id]
        terminal_entry = terminals_by_invocation.get(invocation_id)
        start = start_entry["record"]
        terminal = terminal_entry["record"] if terminal_entry else None
        if terminal:
            _validate_start_terminal_correlation(start, terminal)
        rows.append(
            _invocation_projection_row(
                start,
                terminal,
                source_path=start_entry["source_path"],
                start_sha256=start_entry["sha256"],
                terminal_sha256=(
                    terminal_entry["sha256"] if terminal_entry else None
                ),
                run_identity=run_identities.get(start.get("run_id"), {}),
                acceptance_metadata=acceptance_metadata.get(
                    invocation_id,
                    {},
                ),
            )
        )
    return rows


def _validate_acceptance_artifact_correlation(
    artifact,
    start,
    terminal,
    source_path,
):
    for key in (
        "project",
        "run_id",
        "taskpack_id",
        "implementation_run_id",
        "gate_epoch",
    ):
        artifact_value = artifact.get(key)
        for record in (start, terminal):
            if not isinstance(record, dict):
                continue
            record_value = record.get(key)
            if (
                artifact_value is not None
                and record_value is not None
                and artifact_value != record_value
            ):
                raise ProjectionIntegrityError(
                    f"acceptance artifact correlation differs for {key}: "
                    f"{source_path}"
                )


def _authoritative_started_paths(work_root):
    work_root = Path(work_root).resolve()
    return sorted(
        path
        for path in work_root.rglob("started.json")
        if path.parent.parent.name == "model_invocations"
        and _is_registered_controller_path(path, work_root)
    )


def _is_registered_controller_path(path, work_root):
    relative_parts = path.resolve().relative_to(work_root).parts
    try:
        controller_index = relative_parts.index("controller_invocations")
    except ValueError:
        return True
    candidate = work_root.joinpath(*relative_parts[: controller_index + 1])
    for part in relative_parts[controller_index + 1 : -3]:
        candidate = candidate / part
        if (candidate / "controller_claim.json").is_file():
            return True
    return False


def _passed_acceptance_artifacts(work_root):
    runs_root = Path(work_root).resolve() / "runs"
    for path in _iter_files(runs_root, suffixes={".json"}):
        payload = _read_json_if_exists(path)
        if (
            isinstance(payload, dict)
            and
            payload.get("schema_version") == "model_invocation_live_smoke.v1"
            and payload.get("controller_validation_status") == "passed"
        ):
            yield payload, str(path.resolve())


def _scan_run_identities(work_root):
    identities = {}
    runs_root = Path(work_root).resolve() / "runs"
    if not runs_root.exists():
        return identities
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        identity = _read_json_if_exists(
            run_dir / "state" / "run_identity.v1.json"
        )
        run_id = identity.get("run_id")
        if run_id:
            identities[run_id] = identity
    return identities


def _register_invocation_start(starts, record, source_path):
    record = _canonical_invocation_record(record)
    invocation_id = record.get("invocation_id")
    if not invocation_id:
        raise ProjectionIntegrityError(
            f"invocation start has no invocation_id: {source_path}"
        )
    digest = _record_sha256(record)
    existing = starts.get(invocation_id)
    if existing and existing["sha256"] != digest:
        raise ProjectionIntegrityError(
            f"conflicting starts for invocation {invocation_id}"
        )
    if not existing:
        starts[invocation_id] = {
            "record": dict(record),
            "sha256": digest,
            "source_path": str(source_path),
        }


def _register_invocation_terminal(
    terminals_by_event,
    terminals_by_invocation,
    record,
    source_path,
):
    record = _canonical_invocation_record(record)
    invocation_id = record.get("invocation_id")
    usage_event_id = record.get("usage_event_id")
    if not invocation_id or not usage_event_id:
        raise ProjectionIntegrityError(
            f"invocation terminal is missing identity: {source_path}"
        )
    digest = _record_sha256(record)
    existing_event = terminals_by_event.get(usage_event_id)
    if existing_event and existing_event["sha256"] != digest:
        raise ProjectionIntegrityError(
            f"conflicting terminals for usage event {usage_event_id}"
        )
    existing_invocation = terminals_by_invocation.get(invocation_id)
    if existing_invocation and existing_invocation["sha256"] != digest:
        raise ProjectionIntegrityError(
            f"multiple terminals for invocation {invocation_id}"
        )
    entry = {
        "record": dict(record),
        "sha256": digest,
        "source_path": str(source_path),
    }
    terminals_by_event.setdefault(usage_event_id, entry)
    terminals_by_invocation.setdefault(invocation_id, entry)


def _canonical_invocation_record(record):
    return {
        key: value
        for key, value in record.items()
        if not str(key).startswith("_source_")
    }


def _validate_start_terminal_correlation(start, terminal):
    if start.get("invocation_id") != terminal.get("invocation_id"):
        raise ProjectionIntegrityError("start and terminal invocation IDs differ")
    for key in (
        "project",
        "run_id",
        "pursue_id",
        "round_index",
        "taskpack_id",
        "implementation_run_id",
        "gate_epoch",
        "task_id",
        "attempt_id",
        "runtime_execution_session_id",
        "provider_predecessor_invocation_id",
        "provider_predecessor_turn_id",
        "agent_id",
        "role",
        "usage_stage",
        "backend",
        "model",
        "coverage_class",
        "started_at",
    ):
        start_value = start.get(key)
        terminal_value = terminal.get(key)
        if (
            start_value is not None
            and terminal_value is not None
            and start_value != terminal_value
        ):
            raise ProjectionIntegrityError(
                f"start/terminal correlation differs for {key} "
                f"on invocation {start.get('invocation_id')}"
            )


def _invocation_projection_row(
    start,
    terminal,
    *,
    source_path,
    start_sha256,
    terminal_sha256,
    run_identity,
    acceptance_metadata,
):
    terminal = terminal or {}

    def value(key):
        terminal_value = terminal.get(key)
        return terminal_value if terminal_value is not None else start.get(key)

    run_id = value("run_id") or acceptance_metadata.get("run_id")
    run_kind = (
        acceptance_metadata.get("run_kind")
        or run_identity.get("run_kind")
    )
    implementation_run_id = (
        value("implementation_run_id")
        or acceptance_metadata.get("implementation_run_id")
        or run_identity.get("implementation_run_id")
    )
    gate_epoch = (
        value("gate_epoch")
        if value("gate_epoch") is not None
        else acceptance_metadata.get("gate_epoch", run_identity.get("gate_epoch"))
    )
    if run_kind is None:
        if implementation_run_id is not None and gate_epoch is not None:
            run_kind = "acceptance_evidence"
        else:
            run_kind = "legacy"
    row = {
        "invocation_id": start["invocation_id"],
        "usage_event_id": terminal.get("usage_event_id"),
        "start_schema_version": (
            start.get("start_schema_version")
            or start.get("invocation_schema_version")
        ),
        "usage_schema_version": terminal.get("usage_schema_version"),
        "project": value("project") or acceptance_metadata.get("project"),
        "run_id": run_id,
        "run_kind": run_kind,
        "implementation_run_id": implementation_run_id,
        "gate_epoch": gate_epoch,
        "pursue_id": value("pursue_id"),
        "round_index": value("round_index"),
        "taskpack_id": (
            value("taskpack_id")
            or acceptance_metadata.get("taskpack_id")
            or run_identity.get("taskpack_id")
        ),
        "task_id": value("task_id"),
        "attempt_id": value("attempt_id"),
        "runtime_execution_session_id": value(
            "runtime_execution_session_id"
        ),
        "provider_session_id": terminal.get("provider_session_id"),
        "provider_predecessor_invocation_id": value(
            "provider_predecessor_invocation_id"
        ),
        "provider_turn_id": terminal.get("provider_turn_id"),
        "provider_predecessor_turn_id": value(
            "provider_predecessor_turn_id"
        ),
        "agent_id": value("agent_id"),
        "role": value("role"),
        "usage_stage": value("usage_stage"),
        "backend": value("backend"),
        "model": value("model"),
        "coverage_class": value("coverage_class"),
        "lifecycle_status": "terminal" if terminal else "open",
        "terminal_status": terminal.get("terminal_status"),
        "usage_status": terminal.get("usage_status"),
        "usage_source": terminal.get("usage_source"),
        "provider_usage_scope": terminal.get("provider_usage_scope"),
        "accounting_method": terminal.get("accounting_method"),
        "provider_usage_snapshot_json": (
            _json_dumps(terminal.get("provider_usage_snapshot"))
            if terminal.get("provider_usage_snapshot") is not None
            else None
        ),
        "unavailable_reason": terminal.get("unavailable_reason"),
        "input_tokens": terminal.get("input_tokens"),
        "cached_input_tokens": terminal.get("cached_input_tokens"),
        "output_tokens": terminal.get("output_tokens"),
        "reasoning_tokens": terminal.get("reasoning_tokens"),
        "total_tokens": terminal.get("total_tokens"),
        "started_at": start.get("started_at"),
        "finished_at": terminal.get("finished_at"),
        "wall_time_seconds": terminal.get("wall_time_seconds"),
        "source_artifact_path": source_path,
        "start_record_sha256": start_sha256,
        "terminal_record_sha256": terminal_sha256,
    }
    row["record_sha256"] = _record_sha256(row)
    return row


def _record_sha256(record):
    return hashlib.sha256(
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _scan_taskpacks(frozen_root):
    if not frozen_root.exists():
        return []
    taskpacks = []
    for taskpack_dir in sorted(path for path in frozen_root.iterdir() if path.is_dir()):
        metadata_path = _taskpack_metadata_path(taskpack_dir)
        if metadata_path is None:
            continue
        metadata = _read_json_if_exists(metadata_path)
        taskpack_id = metadata.get("taskpack_id") or metadata.get("id") or taskpack_dir.name
        validation = metadata.get("validation") if isinstance(metadata.get("validation"), dict) else {}
        taskpacks.append(
            {
                "taskpack_id": taskpack_id,
                "taskpack_dir": str(taskpack_dir.resolve()),
                "metadata_path": str(metadata_path.resolve()),
                "goal": metadata.get("goal"),
                "validation_status": validation.get("status") or metadata.get("validation_status"),
            }
        )
    return taskpacks


def _scan_follow_up_items(work_root, runs):
    run_ids = {run["run_id"] for run in runs}
    items = []
    for memory_path in _iter_files(Path(work_root) / "pursue", suffixes={".json"}):
        memory = _read_json_if_exists(memory_path)
        if not isinstance(memory, dict):
            continue
        queue = memory.get("follow_up_queue") if isinstance(memory.get("follow_up_queue"), list) else []
        for index, item in enumerate(queue):
            if not isinstance(item, dict):
                continue
            objective = _text_or_none(item.get("objective"))
            if not objective:
                continue
            source_taskpack_id = (
                _text_or_none(item.get("source_taskpack_id"))
                or _text_or_none(memory.get("latest_taskpack_id"))
            )
            source_run_id = _source_run_id_for_follow_up(
                source_taskpack_id,
                memory,
                run_ids,
            )
            resolved_memory_path = str(memory_path.resolve())
            items.append(
                {
                    "follow_up_id": _stable_id(
                        "follow-up",
                        resolved_memory_path,
                        index,
                        source_taskpack_id,
                        objective,
                    ),
                    "source_run_id": source_run_id,
                    "source_taskpack_id": source_taskpack_id,
                    "item_index": index,
                    "objective": objective,
                    "queue_source": _text_or_none(item.get("source")) or "goal_memory.follow_up_queue",
                    "readiness": _text_or_none(item.get("readiness")),
                    "source_report_path": _text_or_none(item.get("source_report_path")),
                    "goal_memory_path": resolved_memory_path,
                    "source_result_status": _text_or_none(item.get("source_result_status")),
                    "source_run_outcome": _text_or_none(item.get("source_run_outcome")),
                    "stop_reason": _text_or_none(item.get("stop_reason")),
                    "recommended_next_step": _text_or_none(item.get("recommended_next_step")),
                    "suggested_verification": _text_or_none(item.get("suggested_verification")),
                    "source_evidence_paths": _list_value(item.get("source_evidence_paths")),
                    "blockers": _list_value(item.get("blockers")),
                    "token_usage": _dict_value(item.get("token_usage")),
                    "selected_next_goal": index == 0,
                }
            )
    return sorted(
        items,
        key=lambda item: (
            item.get("goal_memory_path") or "",
            item.get("item_index", 0),
            item.get("objective") or "",
        ),
    )


def _source_run_id_for_follow_up(source_taskpack_id, memory, run_ids):
    if source_taskpack_id in run_ids:
        return source_taskpack_id
    latest = _text_or_none(memory.get("latest_taskpack_id"))
    if latest in run_ids:
        return latest
    for run_id in memory.get("latest_run_ids") or []:
        if run_id in run_ids:
            return run_id
    return source_taskpack_id


def _scan_worker_results(runs):
    rows = []
    for run in runs:
        run_dir = Path(run["run_dir"])
        codex_results = _codex_results_by_attempt(run_dir)
        seen_attempts = set()
        for index, step in enumerate(run.get("state", {}).get("steps", [])):
            if not isinstance(step, dict):
                continue
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            attempt_id = (
                _text_or_none(result.get("attempt_id"))
                or _text_or_none(step.get("attempt_id"))
                or f"{_text_or_none(step.get('step_id')) or 'step'}-{index}"
            )
            seen_attempts.add(attempt_id)
            rows.append(
                _worker_result_row(
                    run,
                    step,
                    result,
                    codex_results.get(attempt_id, {}),
                    attempt_id,
                )
            )
        for attempt_id, codex in codex_results.items():
            if attempt_id in seen_attempts:
                continue
            rows.append(
                _worker_result_row(
                    run,
                    {},
                    {},
                    codex,
                    attempt_id,
                )
            )
    return sorted(rows, key=lambda item: (item["run_id"], item.get("task_id") or "", item["attempt_id"]))


def _worker_result_row(run, step, result, codex_result, attempt_id):
    codex_payload = codex_result.get("payload") if isinstance(codex_result, dict) else {}
    if not isinstance(codex_payload, dict):
        codex_payload = {}
    runtime_output = result.get("runtime_output") if isinstance(result.get("runtime_output"), dict) else {}
    if not runtime_output:
        runtime_output = codex_payload.get("output") if isinstance(codex_payload.get("output"), dict) else {}
    operator_summary = (
        runtime_output.get("operator_summary")
        if isinstance(runtime_output.get("operator_summary"), dict)
        else {}
    )
    verification_additions = _list_value(runtime_output.get("verification_additions"))
    task_id = (
        _text_or_none(result.get("task_id"))
        or _text_or_none(step.get("task_id"))
        or _task_id_from_attempt_id(attempt_id)
    )
    row_payload = {
        "run_id": run["run_id"],
        "task_id": task_id,
        "attempt_id": attempt_id,
        "result_status": (
            _text_or_none(codex_payload.get("result_status"))
            or _text_or_none(result.get("result_status"))
            or _text_or_none(result.get("runtime_result_status"))
        ),
        "validation_status": _text_or_none(result.get("validation_status")),
        "failure_category": _text_or_none(result.get("failure_category")),
        "patch_path": _text_or_none(result.get("patch_path")),
        "changed_files": _list_value(result.get("changed_files"))
        or _list_value(codex_payload.get("changed_files")),
        "operator_summary": operator_summary,
        "verification_additions": verification_additions,
        "verification_additions_count": len(verification_additions),
        "evidence_level": _text_or_none(result.get("evidence_level")),
        "evidence_status": _text_or_none(result.get("evidence_status")),
        "trace_carrier": _list_value(result.get("trace_carrier")),
        "missing_evidence": _list_value(result.get("missing_evidence")),
        "source_path": codex_result.get("path") or run.get("state_path"),
    }
    content = _row_content_metadata(row_payload)
    return {**row_payload, **content}


def _codex_results_by_attempt(run_dir):
    results = {}
    for path in _iter_files(Path(run_dir) / "codex_results", suffixes={".json"}):
        payload = _read_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        attempt_id = _attempt_id_from_codex_result_path(path)
        if not attempt_id:
            continue
        results[attempt_id] = {
            "path": str(path.resolve()),
            "payload": payload,
        }
    return results


def _scan_integration_outcomes(runs):
    records = {}
    for run in runs:
        run_dir = Path(run["run_dir"])
        for step in run.get("state", {}).get("steps", []):
            if not isinstance(step, dict):
                continue
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            if _has_integration_fields(result):
                _merge_integration_record(
                    records,
                    run["run_id"],
                    result,
                    "state_step_result",
                )
        integration_queue = _read_json_if_exists(run_dir / "state" / "integration_queue.json")
        for item in (
            integration_queue.get("items", []) if isinstance(integration_queue, dict) else []
        ):
            if isinstance(item, dict):
                _merge_integration_record(records, run["run_id"], item, "integration_queue")
        integration_batches = _read_json_if_exists(run_dir / "state" / "integration_batches.json")
        for batch in (
            integration_batches.get("items", [])
            if isinstance(integration_batches, dict)
            else []
        ):
            if isinstance(batch, dict):
                _merge_integration_batch_records(records, run["run_id"], batch)
        for event in run.get("raw_events", []):
            if not isinstance(event, dict) or not str(event.get("event_type", "")).startswith("integration_"):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            _merge_integration_record(records, run["run_id"], payload, f"event:{event.get('event_type')}")
    rows = []
    for record in records.values():
        record["source_kinds"] = sorted(record.get("source_kinds", set()))
        record["integration_verification_additions"] = _list_value(
            record.get("integration_verification_additions")
        )
        record["integration_verification_additions_count"] = len(
            record["integration_verification_additions"]
        )
        record["integration_outcome_id"] = _stable_id(
            "integration",
            record.get("run_id"),
            record.get("task_id"),
            record.get("attempt_id"),
            record.get("batch_id"),
        )
        content = _row_content_metadata(record)
        rows.append({**record, **content})
    return sorted(
        rows,
        key=lambda item: (
            item["run_id"],
            item.get("task_id") or "",
            item.get("attempt_id") or "",
            item.get("batch_id") or "",
        ),
    )


def _merge_integration_batch_records(records, run_id, batch):
    queue_item_ids = batch.get("applied_queue_item_ids") or batch.get("queue_item_ids") or []
    if not queue_item_ids:
        _merge_integration_record(records, run_id, batch, "integration_batch")
        return
    for queue_item_id in queue_item_ids:
        task_id, attempt_id = _task_attempt_from_queue_item_id(queue_item_id)
        payload = {
            **batch,
            "queue_item_id": queue_item_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "batch_id": batch.get("batch_id"),
            "integration_verification_status": batch.get("verification_status"),
            "integration_verification_exit_code": batch.get("verification_exit_code"),
            "integration_commit_status": batch.get("batch_commit_status"),
            "integration_commit_sha": batch.get("batch_commit_sha"),
        }
        _merge_integration_record(records, run_id, payload, "integration_batch")


def _merge_integration_record(records, run_id, payload, source_kind):
    if not isinstance(payload, dict):
        return
    queue_item_id = _text_or_none(payload.get("queue_item_id"))
    task_id = _text_or_none(payload.get("task_id"))
    attempt_id = _text_or_none(payload.get("attempt_id"))
    if (not task_id or not attempt_id) and queue_item_id:
        parsed_task_id, parsed_attempt_id = _task_attempt_from_queue_item_id(queue_item_id)
        task_id = task_id or parsed_task_id
        attempt_id = attempt_id or parsed_attempt_id
    attempt_id = attempt_id or _text_or_none(payload.get("batch_id")) or "unknown"
    key = (run_id, task_id, attempt_id, _text_or_none(payload.get("batch_id")))
    record = records.setdefault(
        key,
        {
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "batch_id": _text_or_none(payload.get("batch_id")),
            "source_kinds": set(),
            "integration_verification_additions": [],
        },
    )
    record["source_kinds"].add(source_kind)
    for target, source in [
        ("queue_item_id", "queue_item_id"),
        ("queue_status", "queue_status"),
        ("queue_status", "integration_queue_status"),
        ("integration_status", "integration_status"),
        ("integration_branch", "integration_branch"),
        ("integration_worktree_path", "integration_worktree_path"),
        ("integration_verification_status", "integration_verification_status"),
        ("integration_verification_exit_code", "integration_verification_exit_code"),
        (
            "integration_verification_additions_status",
            "integration_verification_additions_status",
        ),
        ("integration_commit_status", "integration_commit_status"),
        ("integration_commit_sha", "integration_commit_sha"),
        ("patch_path", "patch_path"),
    ]:
        value = payload.get(source)
        if value not in (None, "", []):
            record[target] = value
    additions = _list_value(payload.get("integration_verification_additions"))
    if additions:
        record["integration_verification_additions"] = additions


def _has_integration_fields(payload):
    return any(
        payload.get(key) not in (None, "", [], "not_requested", "not_queued")
        for key in [
            "integration_queue_status",
            "integration_status",
            "integration_verification_status",
            "integration_verification_additions_status",
            "integration_commit_status",
        ]
    )


def _scan_worker_verification_additions(worker_results, integration_outcomes):
    rows = []
    for result in worker_results:
        for index, addition in enumerate(result.get("verification_additions", [])):
            rows.append(
                _verification_addition_row(
                    result,
                    addition,
                    index,
                    source_kind="worker_output",
                )
            )
    for outcome in integration_outcomes:
        for index, addition in enumerate(outcome.get("integration_verification_additions", [])):
            rows.append(
                _verification_addition_row(
                    outcome,
                    addition,
                    index,
                    source_kind="integration_verification",
                )
            )
    return sorted(
        rows,
        key=lambda item: (
            item["run_id"],
            item.get("task_id") or "",
            item.get("attempt_id") or "",
            item["source_kind"],
            item.get("label") or "",
        ),
    )


def _verification_addition_row(source, addition, index, *, source_kind):
    addition = addition if isinstance(addition, dict) else {}
    command = addition.get("command") if isinstance(addition.get("command"), list) else []
    label = _text_or_none(addition.get("label")) or f"verification-addition-{index + 1}"
    return {
        "verification_addition_id": _stable_id(
            "verification-addition",
            source_kind,
            source.get("run_id"),
            source.get("task_id"),
            source.get("attempt_id"),
            index,
            label,
            command,
        ),
        "run_id": source["run_id"],
        "task_id": source.get("task_id"),
        "attempt_id": source.get("attempt_id"),
        "source_kind": source_kind,
        "label": label,
        "command": command,
        "reason": _text_or_none(addition.get("reason")),
        "verification_addition_status": _text_or_none(
            addition.get("verification_addition_status")
        ),
        "verification_addition_exit_code": addition.get("verification_addition_exit_code"),
        "verification_addition_rejection_reason": _text_or_none(
            addition.get("verification_addition_rejection_reason")
        ),
    }


def _create_projection_schema(connection):
    connection.execute("pragma journal_mode=delete")
    connection.execute(
        """
        create table if not exists schema_info(
            key text primary key,
            value text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists runs(
            run_id text primary key,
            run_dir text not null,
            run_status text,
            scheduler_status text,
            event_count integer not null,
            latest_event_sequence integer,
            latest_event_type text,
            latest_event_time text,
            state_path text,
            events_path text,
            report_path text
        )
        """
    )
    connection.execute(
        """
        create table if not exists taskpacks(
            taskpack_id text primary key,
            taskpack_dir text not null,
            metadata_path text,
            goal text,
            validation_status text
        )
        """
    )
    connection.execute(
        """
        create table if not exists events(
            run_id text not null,
            sequence integer not null,
            event_id text,
            event_type text,
            task_id text,
            attempt_id text,
            lease_id text,
            step_id text,
            time text,
            payload_json text,
            event_json text,
            primary key(run_id, sequence)
        )
        """
    )
    connection.execute(
        """
        create table if not exists tasks(
            run_id text not null,
            task_id text not null,
            task_status text,
            backlog_status text,
            primary key(run_id, task_id)
        )
        """
    )
    connection.execute(
        """
        create table if not exists evidence_summaries(
            run_id text not null,
            task_id text,
            attempt_id text,
            evidence_level text,
            evidence_status text,
            trace_carrier_json text,
            missing_evidence_json text,
            source_path text,
            content_size_bytes integer,
            content_sha256 text
        )
        """
    )
    connection.execute(
        """
        create table if not exists artifacts(
            artifact_id text primary key,
            artifact_type text not null,
            run_id text,
            taskpack_id text,
            task_id text,
            attempt_id text,
            path text not null,
            size_bytes integer not null,
            sha256 text not null,
            retention_policy text not null,
            authority text not null,
            source text,
            mtime_ns integer
        )
        """
    )
    connection.execute(
        """
        create table if not exists run_stats(
            run_id text primary key,
            task_count integer not null,
            event_count integer not null,
            evidence_summary_count integer not null,
            artifact_count integer not null,
            artifact_bytes integer not null,
            token_usage_status text,
            reported_attempt_count integer,
            unreported_attempt_count integer,
            input_tokens integer,
            output_tokens integer,
            total_tokens integer,
            cached_input_tokens integer,
            reasoning_tokens integer
        )
        """
    )
    connection.execute(
        """
        create table if not exists follow_up_items(
            follow_up_id text primary key,
            source_run_id text,
            source_taskpack_id text,
            item_index integer not null,
            objective text not null,
            queue_source text,
            readiness text,
            source_report_path text,
            goal_memory_path text,
            source_result_status text,
            source_run_outcome text,
            stop_reason text,
            recommended_next_step text,
            suggested_verification text,
            source_evidence_paths_json text,
            blockers_json text,
            token_usage_json text,
            selected_next_goal integer not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists worker_results(
            run_id text not null,
            task_id text,
            attempt_id text not null,
            result_status text,
            validation_status text,
            failure_category text,
            patch_path text,
            changed_files_json text,
            operator_summary_json text,
            verification_additions_json text,
            verification_additions_count integer not null,
            evidence_level text,
            evidence_status text,
            trace_carrier_json text,
            missing_evidence_json text,
            source_path text,
            content_size_bytes integer,
            content_sha256 text,
            primary key(run_id, attempt_id)
        )
        """
    )
    connection.execute(
        """
        create table if not exists integration_outcomes(
            integration_outcome_id text primary key,
            run_id text not null,
            task_id text,
            attempt_id text,
            queue_item_id text,
            queue_status text,
            integration_status text,
            integration_branch text,
            integration_worktree_path text,
            integration_verification_status text,
            integration_verification_exit_code integer,
            integration_verification_additions_status text,
            integration_verification_additions_json text,
            integration_verification_additions_count integer not null,
            integration_commit_status text,
            integration_commit_sha text,
            patch_path text,
            batch_id text,
            source_kinds_json text,
            content_size_bytes integer,
            content_sha256 text
        )
        """
    )
    connection.execute(
        """
        create table if not exists worker_verification_additions(
            verification_addition_id text primary key,
            run_id text not null,
            task_id text,
            attempt_id text,
            source_kind text not null,
            label text,
            command_json text,
            reason text,
            verification_addition_status text,
            verification_addition_exit_code integer,
            verification_addition_rejection_reason text
        )
        """
    )
    connection.execute(
        """
        create table if not exists experiment_results(
            experiment_run_id text primary key,
            run_dir text not null,
            protocol_sha256 text not null,
            run_manifest_sha256 text not null,
            mode text not null,
            repetition_index integer not null,
            terminal_status text not null,
            acceptance_status text not null,
            input_tokens integer,
            output_tokens integer,
            total_tokens integer,
            covered_invocations integer,
            total_invocations integer,
            usage_coverage_status text,
            budget_result_json text not null,
            expected_operator_actions integer not null,
            corrective_interventions integer not null,
            decision_escalations integer not null,
            artifact_bytes_written integer not null,
            raw_spool_bytes_written integer not null,
            projection_identity_sha256 text not null,
            bundle_sha256 text not null,
            bundle_json text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists experiment_recovery(
            experiment_run_id text primary key,
            run_dir text not null,
            snapshot_sequence integer not null,
            snapshot_sha256 text not null,
            controller_status text not null,
            resume_phase text not null,
            resumable integer not null,
            snapshot_json text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists invocations(
            invocation_id text primary key,
            usage_event_id text unique,
            start_schema_version text,
            usage_schema_version text,
            project text,
            run_id text,
            run_kind text,
            implementation_run_id text,
            gate_epoch integer,
            pursue_id text,
            round_index integer,
            taskpack_id text,
            task_id text,
            attempt_id text,
            runtime_execution_session_id text,
            provider_session_id text,
            provider_predecessor_invocation_id text,
            provider_turn_id text,
            provider_predecessor_turn_id text,
            agent_id text,
            role text,
            usage_stage text,
            backend text,
            model text,
            coverage_class text,
            lifecycle_status text not null,
            terminal_status text,
            usage_status text,
            usage_source text,
            provider_usage_scope text,
            accounting_method text,
            provider_usage_snapshot_json text,
            unavailable_reason text,
            input_tokens integer,
            cached_input_tokens integer,
            output_tokens integer,
            reasoning_tokens integer,
            total_tokens integer,
            started_at text,
            finished_at text,
            wall_time_seconds real,
            source_artifact_path text not null,
            start_record_sha256 text not null,
            terminal_record_sha256 text,
            record_sha256 text not null
        )
        """
    )
    connection.execute(
        """
        create index if not exists invocations_query_dimensions_idx
        on invocations(
            run_id, run_kind, usage_stage, role, task_id, attempt_id,
            backend, model, pursue_id, round_index
        )
        """
    )
    connection.execute(
        """
        create index if not exists invocations_implementation_lineage_idx
        on invocations(implementation_run_id, run_kind, run_id)
        """
    )
    connection.execute(
        """
        create index if not exists invocations_gate_epoch_idx
        on invocations(gate_epoch, implementation_run_id)
        """
    )
    connection.execute(
        """
        create index if not exists invocations_provider_lineage_idx
        on invocations(
            provider_session_id, provider_predecessor_invocation_id,
            provider_turn_id
        )
        """
    )
    connection.execute(
        """
        insert or replace into schema_info(key, value) values('schema_version', ?)
        """,
        (PROJECTION_SCHEMA_VERSION,),
    )


def _write_projection_rows(connection, projection):
    runs = projection["runs"]
    taskpacks = projection["taskpacks"]
    artifacts = projection["artifacts"]
    run_stats = projection["run_stats"]
    connection.executemany(
        """
        insert into runs(
            run_id,
            run_dir,
            run_status,
            scheduler_status,
            event_count,
            latest_event_sequence,
            latest_event_type,
            latest_event_time,
            state_path,
            events_path,
            report_path
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run["run_id"],
                run["run_dir"],
                run["run_status"],
                run["scheduler_status"],
                run["event_count"],
                run["latest_event_sequence"],
                run["latest_event_type"],
                run["latest_event_time"],
                run["state_path"],
                run["events_path"],
                run["report_path"],
            )
            for run in runs
        ],
    )
    connection.executemany(
        """
        insert into taskpacks(
            taskpack_id,
            taskpack_dir,
            metadata_path,
            goal,
            validation_status
        ) values(?, ?, ?, ?, ?)
        """,
        [
            (
                taskpack["taskpack_id"],
                taskpack["taskpack_dir"],
                taskpack["metadata_path"],
                taskpack["goal"],
                taskpack["validation_status"],
            )
            for taskpack in taskpacks
        ],
    )
    connection.executemany(
        """
        insert into events(
            run_id,
            sequence,
            event_id,
            event_type,
            task_id,
            attempt_id,
            lease_id,
            step_id,
            time,
            payload_json,
            event_json
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [event for run in runs for event in run["events"]],
    )
    connection.executemany(
        """
        insert into tasks(
            run_id,
            task_id,
            task_status,
            backlog_status
        ) values(?, ?, ?, ?)
        """,
        [task for run in runs for task in run["tasks"]],
    )
    connection.executemany(
        """
        insert into evidence_summaries(
            run_id,
            task_id,
            attempt_id,
            evidence_level,
            evidence_status,
            trace_carrier_json,
            missing_evidence_json,
            source_path,
            content_size_bytes,
            content_sha256
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            evidence
            for run in runs
            for evidence in run["evidence_summaries"]
        ],
    )
    connection.executemany(
        """
        insert into artifacts(
            artifact_id,
            artifact_type,
            run_id,
            taskpack_id,
            task_id,
            attempt_id,
            path,
            size_bytes,
            sha256,
            retention_policy,
            authority,
            source,
            mtime_ns
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [artifact for artifact in artifacts],
    )
    connection.executemany(
        """
        insert into run_stats(
            run_id,
            task_count,
            event_count,
            evidence_summary_count,
            artifact_count,
            artifact_bytes,
            token_usage_status,
            reported_attempt_count,
            unreported_attempt_count,
            input_tokens,
            output_tokens,
            total_tokens,
            cached_input_tokens,
            reasoning_tokens
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [stats for stats in run_stats],
    )
    connection.executemany(
        """
        insert into follow_up_items(
            follow_up_id,
            source_run_id,
            source_taskpack_id,
            item_index,
            objective,
            queue_source,
            readiness,
            source_report_path,
            goal_memory_path,
            source_result_status,
            source_run_outcome,
            stop_reason,
            recommended_next_step,
            suggested_verification,
            source_evidence_paths_json,
            blockers_json,
            token_usage_json,
            selected_next_goal
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["follow_up_id"],
                item.get("source_run_id"),
                item.get("source_taskpack_id"),
                item["item_index"],
                item["objective"],
                item.get("queue_source"),
                item.get("readiness"),
                item.get("source_report_path"),
                item.get("goal_memory_path"),
                item.get("source_result_status"),
                item.get("source_run_outcome"),
                item.get("stop_reason"),
                item.get("recommended_next_step"),
                item.get("suggested_verification"),
                _json_dumps(item.get("source_evidence_paths", [])),
                _json_dumps(item.get("blockers", [])),
                _json_dumps(item.get("token_usage", {})),
                1 if item.get("selected_next_goal") else 0,
            )
            for item in projection["follow_up_items"]
        ],
    )
    connection.executemany(
        """
        insert into worker_results(
            run_id,
            task_id,
            attempt_id,
            result_status,
            validation_status,
            failure_category,
            patch_path,
            changed_files_json,
            operator_summary_json,
            verification_additions_json,
            verification_additions_count,
            evidence_level,
            evidence_status,
            trace_carrier_json,
            missing_evidence_json,
            source_path,
            content_size_bytes,
            content_sha256
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["run_id"],
                item.get("task_id"),
                item["attempt_id"],
                item.get("result_status"),
                item.get("validation_status"),
                item.get("failure_category"),
                item.get("patch_path"),
                _json_dumps(item.get("changed_files", [])),
                _json_dumps(item.get("operator_summary", {})),
                _json_dumps(item.get("verification_additions", [])),
                item.get("verification_additions_count", 0),
                item.get("evidence_level"),
                item.get("evidence_status"),
                _json_dumps(item.get("trace_carrier", [])),
                _json_dumps(item.get("missing_evidence", [])),
                item.get("source_path"),
                item.get("content_size_bytes"),
                item.get("content_sha256"),
            )
            for item in projection["worker_results"]
        ],
    )
    connection.executemany(
        """
        insert into integration_outcomes(
            integration_outcome_id,
            run_id,
            task_id,
            attempt_id,
            queue_item_id,
            queue_status,
            integration_status,
            integration_branch,
            integration_worktree_path,
            integration_verification_status,
            integration_verification_exit_code,
            integration_verification_additions_status,
            integration_verification_additions_json,
            integration_verification_additions_count,
            integration_commit_status,
            integration_commit_sha,
            patch_path,
            batch_id,
            source_kinds_json,
            content_size_bytes,
            content_sha256
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["integration_outcome_id"],
                item["run_id"],
                item.get("task_id"),
                item.get("attempt_id"),
                item.get("queue_item_id"),
                item.get("queue_status"),
                item.get("integration_status"),
                item.get("integration_branch"),
                item.get("integration_worktree_path"),
                item.get("integration_verification_status"),
                item.get("integration_verification_exit_code"),
                item.get("integration_verification_additions_status"),
                _json_dumps(item.get("integration_verification_additions", [])),
                item.get("integration_verification_additions_count", 0),
                item.get("integration_commit_status"),
                item.get("integration_commit_sha"),
                item.get("patch_path"),
                item.get("batch_id"),
                _json_dumps(item.get("source_kinds", [])),
                item.get("content_size_bytes"),
                item.get("content_sha256"),
            )
            for item in projection["integration_outcomes"]
        ],
    )
    connection.executemany(
        """
        insert into worker_verification_additions(
            verification_addition_id,
            run_id,
            task_id,
            attempt_id,
            source_kind,
            label,
            command_json,
            reason,
            verification_addition_status,
            verification_addition_exit_code,
            verification_addition_rejection_reason
        ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["verification_addition_id"],
                item["run_id"],
                item.get("task_id"),
                item.get("attempt_id"),
                item["source_kind"],
                item.get("label"),
                _json_dumps(item.get("command", [])),
                item.get("reason"),
                item.get("verification_addition_status"),
                item.get("verification_addition_exit_code"),
                item.get("verification_addition_rejection_reason"),
            )
            for item in projection["worker_verification_additions"]
        ],
    )
    connection.executemany(
        """
        insert into experiment_results(
            experiment_run_id, run_dir, protocol_sha256,
            run_manifest_sha256, mode, repetition_index,
            terminal_status, acceptance_status, input_tokens,
            output_tokens, total_tokens, covered_invocations,
            total_invocations, usage_coverage_status, budget_result_json,
            expected_operator_actions, corrective_interventions,
            decision_escalations, artifact_bytes_written,
            raw_spool_bytes_written, projection_identity_sha256,
            bundle_sha256, bundle_json
        ) values(
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?
        )
        """,
        [
            (
                item["experiment_run_id"],
                item["run_dir"],
                item["protocol_sha256"],
                item["run_manifest_sha256"],
                item["mode"],
                item["repetition_index"],
                item["terminal_status"],
                item["acceptance_status"],
                item["usage_totals"].get("input_tokens"),
                item["usage_totals"].get("output_tokens"),
                item["usage_totals"].get("total_tokens"),
                item["usage_coverage"].get("covered_invocations"),
                item["usage_coverage"].get("total_invocations"),
                item["usage_coverage"].get("status"),
                _json_dumps(item["budget_result"]),
                item["operator_action_counts"][
                    "expected_operator_action"
                ],
                item["operator_action_counts"][
                    "corrective_intervention"
                ],
                item["operator_action_counts"]["decision_escalation"],
                item["artifact_bytes_written"],
                item["raw_spool_bytes_written"],
                item["projection_identity_sha256"],
                item["bundle_sha256"],
                _json_dumps(item["bundle"]),
            )
            for item in projection["experiment_results"]
        ],
    )
    connection.executemany(
        """
        insert into experiment_recovery(
            experiment_run_id, run_dir, snapshot_sequence, snapshot_sha256,
            controller_status, resume_phase, resumable, snapshot_json
        ) values(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["experiment_run_id"],
                item["run_dir"],
                item["snapshot_sequence"],
                item["snapshot_sha256"],
                item["controller_status"],
                item["resume_phase"],
                1 if item["resumable"] else 0,
                _json_dumps(item["snapshot"]),
            )
            for item in projection["experiment_recovery"]
        ],
    )
    connection.executemany(
        f"""
        insert into invocations({", ".join(_INVOCATION_COLUMNS)})
        values({", ".join("?" for _ in _INVOCATION_COLUMNS)})
        """,
        [
            tuple(item.get(column) for column in _INVOCATION_COLUMNS)
            for item in projection["invocations"]
        ],
    )


def _projection_counts(projection):
    evidence_counts = {}
    for run in projection["runs"]:
        for evidence in run["evidence_summaries"]:
            status = evidence[4]
            if status:
                evidence_counts[status] = evidence_counts.get(status, 0) + 1
    return {
        "runs": len(projection["runs"]),
        "taskpacks": len(projection["taskpacks"]),
        "events": sum(len(run["events"]) for run in projection["runs"]),
        "tasks": sum(len(run["tasks"]) for run in projection["runs"]),
        "evidence_summaries": sum(
            len(run["evidence_summaries"])
            for run in projection["runs"]
        ),
        "artifacts": len(projection["artifacts"]),
        "artifact_bytes": sum(artifact[7] for artifact in projection["artifacts"]),
        "artifact_digest": _artifact_digest(projection["artifacts"]),
        "run_stats": len(projection["run_stats"]),
        "follow_up_items": len(projection["follow_up_items"]),
        "worker_results": len(projection["worker_results"]),
        "integration_outcomes": len(projection["integration_outcomes"]),
        "worker_verification_additions": len(
            projection["worker_verification_additions"]
        ),
        "invocations": len(projection["invocations"]),
        "invocation_digest": _invocation_digest(projection["invocations"]),
        "experiment_results": len(projection["experiment_results"]),
        "experiment_result_digest": _experiment_result_digest(
            projection["experiment_results"]
        ),
        "experiment_recovery": len(projection["experiment_recovery"]),
        "experiment_recovery_digest": _experiment_recovery_digest(
            projection["experiment_recovery"]
        ),
        "evidence": evidence_counts,
    }


def _database_counts(db_path):
    with sqlite3.connect(db_path) as connection:
        return {
            "runs": _table_count(connection, "runs"),
            "taskpacks": _table_count(connection, "taskpacks"),
            "events": _table_count(connection, "events"),
            "tasks": _table_count(connection, "tasks"),
            "evidence_summaries": _table_count(connection, "evidence_summaries"),
            "artifacts": _table_count(connection, "artifacts"),
            "artifact_bytes": _database_artifact_bytes(connection),
            "artifact_digest": _database_artifact_digest(connection),
            "run_stats": _table_count(connection, "run_stats"),
            "follow_up_items": _table_count(connection, "follow_up_items"),
            "worker_results": _table_count(connection, "worker_results"),
            "integration_outcomes": _table_count(connection, "integration_outcomes"),
            "worker_verification_additions": _table_count(
                connection,
                "worker_verification_additions",
            ),
            "invocations": _table_count(connection, "invocations"),
            "invocation_digest": _database_invocation_digest(connection),
            "experiment_results": _table_count(
                connection,
                "experiment_results",
            ),
            "experiment_result_digest": (
                _database_experiment_result_digest(connection)
            ),
            "experiment_recovery": _table_count(
                connection,
                "experiment_recovery",
            ),
            "experiment_recovery_digest": (
                _database_experiment_recovery_digest(connection)
            ),
            "evidence": _database_evidence_counts(connection),
        }


def _database_schema_version(db_path):
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "select value from schema_info where key='schema_version'"
        ).fetchone()
    return row[0] if row else None


def _table_count(connection, table_name):
    try:
        return connection.execute(f"select count(*) from {table_name}").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise


def _database_evidence_counts(connection):
    rows = connection.execute(
        """
        select evidence_status, count(*)
        from evidence_summaries
        where evidence_status is not null and evidence_status != ''
        group by evidence_status
        """
    ).fetchall()
    return {status: count for status, count in rows}


def _database_artifact_bytes(connection):
    row = connection.execute(
        "select coalesce(sum(size_bytes), 0) from artifacts"
    ).fetchone()
    return row[0] if row else 0


def _database_artifact_digest(connection):
    rows = connection.execute(
        """
        select artifact_id, artifact_type, run_id, taskpack_id, task_id,
               attempt_id, path, size_bytes, sha256, retention_policy, authority,
               source, mtime_ns
        from artifacts
        order by path, artifact_type, artifact_id
        """
    ).fetchall()
    return _artifact_digest(rows)


def _database_invocation_digest(connection):
    try:
        rows = _read_invocation_rows_from_connection(connection)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        rows = []
    return _invocation_digest(rows)


def _database_experiment_result_digest(connection):
    try:
        rows = connection.execute(
            """
            select experiment_run_id, protocol_sha256, run_manifest_sha256,
                   mode, repetition_index, terminal_status, acceptance_status,
                   input_tokens, output_tokens, total_tokens,
                   covered_invocations, total_invocations,
                   usage_coverage_status, budget_result_json,
                   expected_operator_actions, corrective_interventions,
                   decision_escalations, artifact_bytes_written,
                   raw_spool_bytes_written, projection_identity_sha256,
                   bundle_sha256, bundle_json
            from experiment_results
            order by experiment_run_id
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        rows = []
    projected = []
    for row in rows:
        try:
            bundle = json.loads(row[21])
            validate_experiment_result_bundle(bundle)
            budget = json.loads(row[13])
        except (json.JSONDecodeError, ExperimentResultError) as exc:
            raise ProjectionIntegrityError(
                "experiment result projection payload is invalid"
            ) from exc
        expected = (
            bundle["experiment_run_id"],
            bundle["protocol_sha256"],
            bundle["run_manifest_sha256"],
            bundle["mode"],
            bundle["repetition_index"],
            bundle["terminal_status"],
            bundle["acceptance_result"]["status"],
            bundle["usage_totals"].get("input_tokens"),
            bundle["usage_totals"].get("output_tokens"),
            bundle["usage_totals"].get("total_tokens"),
            bundle["usage_coverage"].get("covered_invocations"),
            bundle["usage_coverage"].get("total_invocations"),
            bundle["usage_coverage"].get("status"),
            bundle["budget_result"],
            bundle["operator_action_counts"][
                "expected_operator_action"
            ],
            bundle["operator_action_counts"]["corrective_intervention"],
            bundle["operator_action_counts"]["decision_escalation"],
            bundle["artifact_bytes_written"],
            bundle["raw_spool_bytes_written"],
            bundle["projection_reconciliation"]["identity_sha256"],
        )
        actual = (*row[:13], budget, *row[14:20])
        if actual != expected:
            raise ProjectionIntegrityError(
                "experiment result projection columns disagree with bundle"
            )
        projected.append(
            {
                "experiment_run_id": row[0],
                "bundle_sha256": row[20],
                "projection_identity_sha256": row[19],
                "bundle": bundle,
            }
        )
    return _experiment_result_digest(projected)


def _database_experiment_recovery_digest(connection):
    try:
        rows = connection.execute(
            """
            select experiment_run_id, snapshot_sequence, snapshot_sha256,
                   snapshot_json
            from experiment_recovery
            order by experiment_run_id
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        rows = []
    projected = []
    for run_id, sequence, snapshot_sha256, snapshot_json in rows:
        try:
            snapshot = json.loads(snapshot_json)
            validate_experiment_recovery_snapshot(snapshot)
        except (json.JSONDecodeError, ExperimentResultError) as exc:
            raise ProjectionIntegrityError(
                "experiment recovery projection payload is invalid"
            ) from exc
        if (
            snapshot["experiment_run_id"] != run_id
            or snapshot["snapshot_sequence"] != sequence
        ):
            raise ProjectionIntegrityError(
                "experiment recovery projection columns disagree with snapshot"
            )
        projected.append(
            {
                "experiment_run_id": run_id,
                "snapshot_sequence": sequence,
                "snapshot_sha256": snapshot_sha256,
                "snapshot": snapshot,
            }
        )
    return _experiment_recovery_digest(projected)


def _project_stats_from_database(work_root, check, *, applied_filters):
    db_path = project_projection_db_path(work_root)
    try:
        with sqlite3.connect(db_path) as connection:
            counts = _database_counts(db_path)
            artifact_type_rows = connection.execute(
                """
                select artifact_type, count(*), coalesce(sum(size_bytes), 0)
                from artifacts
                group by artifact_type
                order by artifact_type
                """
            ).fetchall()
            retention_rows = connection.execute(
                """
                select retention_policy, count(*), coalesce(sum(size_bytes), 0)
                from artifacts
                group by retention_policy
                order by retention_policy
                """
            ).fetchall()
            token_rows = connection.execute(
                """
                select token_usage_status, reported_attempt_count,
                       unreported_attempt_count, input_tokens, output_tokens,
                       total_tokens, cached_input_tokens, reasoning_tokens
                from run_stats
                order by run_id
                """
            ).fetchall()
            invocation_rows = _read_invocation_rows_from_connection(connection)
    except sqlite3.DatabaseError:
        return None
    return _project_stats_payload(
        projection_source="db",
        check_status=check.get("check_status"),
        db_path=str(db_path),
        counts=counts,
        artifact_types=_group_rows_to_count_bytes(artifact_type_rows),
        retention_policies=_group_rows_to_count_bytes(retention_rows),
        token_usage=_aggregate_token_usage_from_rows(token_rows),
        invocation_usage=_aggregate_invocation_rows(
            _filter_invocation_rows(invocation_rows, applied_filters),
            applied_filters=applied_filters,
            all_rows=invocation_rows,
        ),
    )


def _artifact_retention_explanations():
    return [
        {
            "retention_policy": "authoritative",
            "reason": "events, state, reports, patches, and frozen taskpacks are audit records and remain protected",
        },
        {
            "retention_policy": "protected",
            "reason": "active, nonterminal, or policy-pinned artifacts are not cleanup candidates",
        },
        {
            "retention_policy": "rebuildable",
            "reason": "derived role/repo context artifacts may become future cleanup candidates; M42 only lists them",
        },
    ]


def _project_stats_from_projection(
    projection,
    *,
    projection_source,
    check_status,
    db_path,
    projection_warning=None,
    next_action=None,
    applied_filters=None,
):
    counts = _projection_counts(projection)
    return _project_stats_payload(
        projection_source=projection_source,
        check_status=check_status,
        db_path=db_path,
        counts=counts,
        artifact_types=_artifact_count_bytes_by_index(projection["artifacts"], 1),
        retention_policies=_artifact_count_bytes_by_index(projection["artifacts"], 9),
        token_usage=_aggregate_token_usage_from_rows(
            [
                (
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                )
                for row in projection["run_stats"]
            ]
        ),
        invocation_usage=_aggregate_invocation_rows(
            _filter_invocation_rows(
                projection["invocations"],
                applied_filters or {},
            ),
            applied_filters=applied_filters or {},
            all_rows=projection["invocations"],
        ),
        projection_warning=projection_warning,
        next_action=next_action,
    )


def _project_stats_payload(
    *,
    projection_source,
    check_status,
    db_path,
    counts,
    artifact_types,
    retention_policies,
    token_usage,
    invocation_usage,
    projection_warning=None,
    next_action=None,
):
    payload = {
        "stats_status": "ok",
        "projection_source": projection_source,
        "check_status": check_status,
        "db_path": db_path,
        "runs": counts.get("runs", 0),
        "taskpacks": counts.get("taskpacks", 0),
        "events": counts.get("events", 0),
        "tasks": counts.get("tasks", 0),
        "evidence_summaries": counts.get("evidence_summaries", 0),
        "follow_up_items": counts.get("follow_up_items", 0),
        "worker_results": counts.get("worker_results", 0),
        "integration_outcomes": counts.get("integration_outcomes", 0),
        "worker_verification_additions": counts.get(
            "worker_verification_additions",
            0,
        ),
        "invocations": counts.get("invocations", 0),
        "invocation_digest": counts.get("invocation_digest"),
        "evidence": counts.get("evidence", {}),
        "artifacts": {
            "total_count": counts.get("artifacts", 0),
            "total_bytes": counts.get("artifact_bytes", 0),
            "by_type": artifact_types,
            "by_retention": retention_policies,
        },
        "token_usage": token_usage,
        "legacy_task_token_usage": {
            "accounting_scope": "legacy_task_results",
            "benchmark_counted": False,
            "usage": token_usage,
        },
        "model_invocation_usage": invocation_usage,
        "invocation_count": invocation_usage.get("invocation_count", 0),
        "open_invocations": invocation_usage.get("open_invocations", 0),
        "lifecycle_terminal_coverage": invocation_usage.get(
            "lifecycle_terminal_coverage"
        ),
        "token_usage_coverage": invocation_usage.get(
            "token_usage_coverage"
        ),
        "reported_token_totals": invocation_usage.get(
            "reported_token_totals"
        ),
        "partial_known_token_lower_bounds": invocation_usage.get(
            "partial_known_token_lower_bounds"
        ),
        "invocation_breakdowns": invocation_usage.get("breakdowns", {}),
    }
    if projection_warning:
        payload["projection_warning"] = projection_warning
    if next_action:
        payload["next_action"] = next_action
    return payload


def _read_invocation_rows_from_connection(connection):
    rows = connection.execute(
        f"""
        select {", ".join(_INVOCATION_COLUMNS)}
        from invocations
        order by invocation_id
        """
    ).fetchall()
    return [
        dict(zip(_INVOCATION_COLUMNS, row))
        for row in rows
    ]


def _normalize_invocation_filters(filters):
    normalized = {}
    for raw_key, raw_value in dict(filters or {}).items():
        if raw_value is None or raw_value == "":
            continue
        key = _INVOCATION_FILTER_FIELDS.get(raw_key)
        if key is None:
            raise ValueError(f"unsupported invocation stats filter: {raw_key}")
        values = (
            list(raw_value)
            if isinstance(raw_value, (list, tuple, set))
            else [raw_value]
        )
        if key in {"gate_epoch", "round_index"}:
            values = [int(value) for value in values]
        existing = normalized.setdefault(key, [])
        for value in values:
            if value not in existing:
                existing.append(value)
    return {
        key: values
        for key, values in sorted(normalized.items())
    }


def _filter_invocation_rows(rows, filters):
    return [
        row
        for row in rows
        if all(row.get(key) in values for key, values in filters.items())
    ]


def _aggregate_invocation_rows(rows, *, applied_filters, all_rows):
    rows = list(rows)
    all_rows = list(all_rows)
    summary = _aggregate_invocation_rows_base(rows)
    legacy_supported_count = sum(
        1
        for row in rows
        if row.get("run_kind") == "legacy"
        and row.get("coverage_class") == "supported_model_invocation"
    )
    if legacy_supported_count:
        summary["benchmark_ready"] = False
        if summary.get("completion_status") == "complete":
            summary["completion_status"] = "legacy_non_benchmark"
    summary.update(
        {
            "authority": "authoritative_lifecycle_files_and_canonical_events",
            "authority_invocation_count": len(all_rows),
            "authority_invocation_digest": _invocation_digest(all_rows),
            "filtered_invocation_digest": _invocation_digest(rows),
            "applied_filters": applied_filters,
            "legacy_supported_invocation_count": legacy_supported_count,
            "legacy_aggregate": {
                "accounting_scope": "legacy_task_results",
                "benchmark_counted": False,
                "explanation": (
                    "legacy task-result totals remain visible but do not count "
                    "as invocation benchmark coverage"
                ),
            },
        }
    )
    dimensions = {
        "run": "run_id",
        "run_kind": "run_kind",
        "implementation_run": "implementation_run_id",
        "gate_epoch": "gate_epoch",
        "stage": "usage_stage",
        "role": "role",
        "round": "round_index",
        "model": "model",
        "task": "task_id",
        "attempt": "attempt_id",
    }
    breakdowns = {
        name: _invocation_breakdown(rows, field)
        for name, field in dimensions.items()
    }
    summary["breakdowns"] = breakdowns
    for name, breakdown in breakdowns.items():
        summary[f"{name}_breakdown"] = breakdown
    return summary


def _aggregate_invocation_rows_base(rows):
    # Imported lazily because operator_report uses projection readers.
    from .operator_report import aggregate_model_invocation_usage

    events = []
    for row in rows:
        start = {
            key: row.get(key)
            for key in (
                "invocation_id",
                "coverage_class",
                "usage_stage",
                "task_id",
                "attempt_id",
                "role",
                "backend",
                "model",
            )
        }
        events.append(
            {
                "event_type": "model_invocation_started",
                "source_event_id": row.get("invocation_id"),
                "payload": start,
            }
        )
        if row.get("lifecycle_status") != "terminal":
            continue
        terminal = {
            key: row.get(key)
            for key in (
                "usage_event_id",
                "invocation_id",
                "coverage_class",
                "usage_stage",
                "task_id",
                "attempt_id",
                "role",
                "backend",
                "model",
                "terminal_status",
                "usage_status",
                "provider_usage_scope",
                "unavailable_reason",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        }
        events.append(
            {
                "event_type": "model_invocation_usage_recorded",
                "source_event_id": row.get("usage_event_id"),
                "payload": terminal,
            }
        )
    summary = aggregate_model_invocation_usage(events)
    if summary is not None:
        return summary
    empty_totals = {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "contributing_invocation_count": 0,
    }
    empty_coverage = {
        "covered": 0,
        "total": 0,
        "percent": None,
        "status": "not_applicable",
    }
    return {
        "summary_schema_version": "model_invocation_usage_summary.v1",
        "summary_status": "empty",
        "accounting_scope": "full_project_canonical_invocations",
        "benchmark_counted": True,
        "invocation_count": 0,
        "terminal_invocation_count": 0,
        "supported_invocation_count": 0,
        "open_invocations": 0,
        "open_supported_invocations": 0,
        "usage_status_counts": {
            "reported": 0,
            "partial": 0,
            "unavailable": 0,
            "not_applicable": 0,
        },
        "terminal_status_counts": {},
        "stage_breakdown": {},
        "lifecycle_terminal_coverage": dict(empty_coverage),
        "token_usage_coverage": dict(empty_coverage),
        "reported_totals_scope": "not_applicable",
        "reported_token_totals": dict(empty_totals),
        "partial_known_token_lower_bounds": dict(empty_totals),
        "observed_token_lower_bound": {
            key: None
            for key in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        },
        "reason_counts": {"partial": [], "unavailable": []},
        "completion_status": "not_applicable",
        "benchmark_ready": False,
        "integrity_conflict_count": 0,
    }


def _invocation_breakdown(rows, field):
    grouped = {}
    for row in rows:
        raw_key = row.get(field)
        key = str(raw_key) if raw_key is not None else "unknown"
        grouped.setdefault(key, []).append(row)
    result = {}
    for key in sorted(grouped):
        summary = _aggregate_invocation_rows_base(grouped[key])
        result[key] = {
            field: None if key == "unknown" else grouped[key][0].get(field),
            "invocation_count": summary["invocation_count"],
            "supported_invocation_count": summary["supported_invocation_count"],
            "terminal_invocation_count": summary["terminal_invocation_count"],
            "open_invocations": summary["open_invocations"],
            "usage_status_counts": summary["usage_status_counts"],
            "lifecycle_terminal_coverage": summary[
                "lifecycle_terminal_coverage"
            ],
            "token_usage_coverage": summary["token_usage_coverage"],
            "reported_token_totals": summary["reported_token_totals"],
            "partial_known_token_lower_bounds": summary[
                "partial_known_token_lower_bounds"
            ],
        }
    return result


def _group_rows_to_count_bytes(rows):
    return {
        row[0]: {
            "count": row[1],
            "bytes": row[2],
        }
        for row in rows
    }


def _artifact_count_bytes_by_index(artifacts, index):
    grouped = {}
    for artifact in artifacts:
        key = artifact[index]
        grouped.setdefault(key, {"count": 0, "bytes": 0})
        grouped[key]["count"] += 1
        grouped[key]["bytes"] += artifact[7]
    return dict(sorted(grouped.items()))


def _aggregate_token_usage_from_rows(rows):
    rows = list(rows)
    total_tokens = _sum_optional(row[5] for row in rows)
    input_tokens = _sum_optional(row[3] for row in rows)
    output_tokens = _sum_optional(row[4] for row in rows)
    cached_input_tokens = _sum_optional(row[6] for row in rows)
    reasoning_tokens = _sum_optional(row[7] for row in rows)
    reported_attempt_count = _sum_ints(row[1] for row in rows)
    unreported_attempt_count = _sum_ints(row[2] for row in rows)
    if total_tokens is None and input_tokens is None and output_tokens is None:
        usage_status = "unavailable"
    elif unreported_attempt_count:
        usage_status = "partial"
    else:
        usage_status = "reported"
    return {
        "usage_status": usage_status,
        "reported_attempt_count": reported_attempt_count,
        "unreported_attempt_count": unreported_attempt_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def _sum_optional(values):
    numbers = [
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return sum(numbers) if numbers else None


def _sum_ints(values):
    return sum(
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    )


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_value(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _text_or_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list_value(value):
    return value if isinstance(value, list) else []


def _dict_value(value):
    return value if isinstance(value, dict) else {}


def _stable_id(*parts):
    return hashlib.sha256(
        _json_dumps([str(part) for part in parts]).encode("utf-8")
    ).hexdigest()


def _row_content_metadata(payload):
    content = _json_dumps(payload).encode("utf-8")
    return {
        "content_size_bytes": len(content),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }


def _latest_event(events):
    if not events:
        return None
    return max(events, key=lambda event: event.get("sequence", 0))


def _run_status(latest_event, state):
    payload = latest_event.get("payload") if isinstance(latest_event, dict) else {}
    if isinstance(payload, dict) and payload.get("run_status"):
        return payload["run_status"]
    return state.get("scheduler_status")


def _run_report_path(run_dir):
    for candidate in [
        run_dir / "reports" / "final_report.json",
        run_dir / "reports" / "final_report.md",
    ]:
        if candidate.exists():
            return str(candidate.resolve())
    return None


def _taskpack_metadata_path(taskpack_dir):
    for name in ["taskpack.json", "manifest.json", "taskpack.yaml", "taskpack.yml"]:
        candidate = taskpack_dir / name
        if candidate.exists():
            return candidate
    return None


def _event_projection(run_id, event):
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return (
        run_id,
        int(event.get("sequence", 0)),
        event.get("event_id"),
        event.get("event_type"),
        payload.get("task_id"),
        payload.get("attempt_id"),
        payload.get("lease_id"),
        event.get("step_id"),
        event.get("time"),
        json.dumps(payload, sort_keys=True),
        json.dumps(event, sort_keys=True),
    )


def _task_projections(run_id, state, events):
    tasks = {}
    for item in state.get("backlog", {}).get("items", []):
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        tasks[item["task_id"]] = (
            run_id,
            item["task_id"],
            item.get("task_status"),
            item.get("backlog_status"),
        )
    for event in events:
        if event.get("event_type") != "backlog_updated":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = payload.get("task_id")
        if not task_id or task_id in tasks:
            continue
        tasks[task_id] = (
            run_id,
            task_id,
            payload.get("task_status"),
            payload.get("backlog_status"),
        )
    return [tasks[task_id] for task_id in sorted(tasks)]


def _scan_artifacts(work_root, runs, taskpacks):
    artifacts = []
    seen = set()
    for run in runs:
        run_id = run["run_id"]
        run_dir = Path(run["run_dir"])
        _append_artifact(
            artifacts,
            seen,
            run_dir / "events.jsonl",
            "event_log",
            run_id=run_id,
            retention_policy="authoritative",
            authority="file",
            source="run",
        )
        for path in _iter_files(run_dir / "reports"):
            _append_artifact(
                artifacts,
                seen,
                path,
                "report",
                run_id=run_id,
                retention_policy="authoritative",
                authority="file",
                source="run",
            )
        for path in _iter_files(run_dir / "state", suffixes={".json"}):
            _append_artifact(
                artifacts,
                seen,
                path,
                "state_snapshot",
                run_id=run_id,
                retention_policy="authoritative",
                authority="file",
                source="run",
            )
        for path in _iter_files(run_dir / "steps", suffixes={".patch", ".diff"}):
            task_id, attempt_id = _task_attempt_from_patch_path(path)
            _append_artifact(
                artifacts,
                seen,
                path,
                "patch",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                retention_policy="authoritative",
                authority="file",
                source="run",
            )
        for path in _iter_files(run_dir / "patches", suffixes={".patch", ".diff"}):
            task_id, attempt_id = _task_attempt_from_patch_path(path)
            _append_artifact(
                artifacts,
                seen,
                path,
                "patch",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                retention_policy="authoritative",
                authority="file",
                source="run",
            )
        for path in _iter_files(run_dir / "role_contexts", suffixes={".json"}):
            task_id, attempt_id = _task_attempt_from_context_path(path)
            _append_artifact(
                artifacts,
                seen,
                path,
                "role_context",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                retention_policy="rebuildable",
                authority="derived",
                source="run",
            )
        for path in _iter_files(run_dir / "repo_contexts", suffixes={".json"}):
            task_id, attempt_id = _task_attempt_from_context_path(path)
            _append_artifact(
                artifacts,
                seen,
                path,
                "repo_context",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                retention_policy="rebuildable",
                authority="derived",
                source="run",
            )
        for path in _iter_files(run_dir / "codex_results", suffixes={".json"}):
            task_id, attempt_id = _task_attempt_from_codex_result_path(path)
            _append_artifact(
                artifacts,
                seen,
                path,
                "worker_result",
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                retention_policy="authoritative",
                authority="file",
                source="run",
            )
    for path in _iter_files(work_root / "pursue", suffixes={".json"}):
        _append_artifact(
            artifacts,
            seen,
            path,
            "goal_memory",
            retention_policy="authoritative",
            authority="file",
            source="pursue",
        )
    for taskpack in taskpacks:
        taskpack_dir = Path(taskpack["taskpack_dir"])
        for path in _iter_files(taskpack_dir):
            _append_artifact(
                artifacts,
                seen,
                path,
                "taskpack",
                taskpack_id=taskpack["taskpack_id"],
                retention_policy="authoritative",
                authority="file",
                source="taskpack",
            )
    return sorted(artifacts, key=lambda artifact: (artifact[6], artifact[1], artifact[0]))


def _append_artifact(
    artifacts,
    seen,
    path,
    artifact_type,
    *,
    run_id=None,
    taskpack_id=None,
    task_id=None,
    attempt_id=None,
    retention_policy,
    authority,
    source,
):
    row = _artifact_row(
        path,
        artifact_type,
        run_id=run_id,
        taskpack_id=taskpack_id,
        task_id=task_id,
        attempt_id=attempt_id,
        retention_policy=retention_policy,
        authority=authority,
        source=source,
    )
    if row is None:
        return
    key = (row[1], row[6])
    if key in seen:
        return
    seen.add(key)
    artifacts.append(row)


def _artifact_row(
    path,
    artifact_type,
    *,
    run_id=None,
    taskpack_id=None,
    task_id=None,
    attempt_id=None,
    retention_policy,
    authority,
    source,
):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    payload = path.read_bytes()
    resolved = str(path.resolve())
    artifact_id = hashlib.sha256(
        f"{artifact_type}\0{resolved}".encode("utf-8")
    ).hexdigest()
    stat = path.stat()
    return (
        artifact_id,
        artifact_type,
        run_id,
        taskpack_id,
        task_id,
        attempt_id,
        resolved,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        retention_policy,
        authority,
        source,
        stat.st_mtime_ns,
    )


def _iter_files(root, suffixes=None):
    root = Path(root)
    if not root.exists():
        return []
    suffixes = {suffix.lower() for suffix in suffixes} if suffixes else None
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if suffixes and path.suffix.lower() not in suffixes:
            continue
        files.append(path)
    return sorted(files)


def _task_attempt_from_context_path(path):
    stem = Path(path).stem
    attempt_id = stem.rsplit("-", 1)[0] if "-" in stem else None
    return _task_id_from_attempt_id(attempt_id), attempt_id


def _task_attempt_from_codex_result_path(path):
    attempt_id = _attempt_id_from_codex_result_path(path)
    return _task_id_from_attempt_id(attempt_id), attempt_id


def _attempt_id_from_codex_result_path(path):
    stem = Path(path).stem
    prefix = "codex_result_"
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return stem or None


def _task_attempt_from_queue_item_id(queue_item_id):
    text = str(queue_item_id or "")
    if ":" not in text:
        attempt_id = text or None
        return _task_id_from_attempt_id(attempt_id), attempt_id
    task_id, attempt_id = text.split(":", 1)
    return task_id or _task_id_from_attempt_id(attempt_id), attempt_id or None


def _task_attempt_from_patch_path(path):
    path = Path(path)
    attempt_id = None
    task_id = None
    for parent in [path.parent, *path.parents]:
        name = parent.name
        if name.startswith("STEP-"):
            parts = name.split("-", 2)
            if len(parts) == 3:
                task_id = parts[2]
                break
    if "-ATTEMPT-" in path.stem:
        attempt_id = path.stem
        task_id = task_id or _task_id_from_attempt_id(attempt_id)
    return task_id, attempt_id


def _task_id_from_attempt_id(attempt_id):
    if not attempt_id or "-ATTEMPT-" not in attempt_id:
        return None
    return attempt_id.split("-ATTEMPT-", 1)[0]


def _run_stats(runs, artifacts):
    rows = []
    for run in runs:
        run_id = run["run_id"]
        run_artifacts = [
            artifact
            for artifact in artifacts
            if artifact[2] == run_id or artifact[3] == run_id
        ]
        usage = run.get("token_usage") if isinstance(run.get("token_usage"), dict) else {}
        rows.append(
            (
                run_id,
                len(run["tasks"]),
                run["event_count"],
                len(run["evidence_summaries"]),
                len(run_artifacts),
                sum(artifact[7] for artifact in run_artifacts),
                usage.get("usage_status"),
                usage.get("reported_attempt_count"),
                usage.get("unreported_attempt_count"),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("total_tokens"),
                usage.get("cached_input_tokens"),
                usage.get("reasoning_tokens"),
            )
        )
    return rows


def _run_token_usage(events, state):
    for event in sorted(events, key=lambda item: item.get("sequence", 0), reverse=True):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        report = payload.get("operator_report") if isinstance(payload.get("operator_report"), dict) else {}
        raw_usage = report.get("token_usage")
        normalized = normalize_token_usage(raw_usage)
        if normalized:
            raw = raw_usage if isinstance(raw_usage, dict) else {}
            return {
                "usage_status": raw.get("usage_status") or "reported",
                "reported_attempt_count": raw.get("reported_attempt_count"),
                "unreported_attempt_count": raw.get("unreported_attempt_count"),
                **normalized,
            }
    return token_usage_from_state(state)


def _artifact_digest(artifacts):
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda row: (row[6], row[1], row[0])):
        digest.update(json.dumps(artifact, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _invocation_digest(invocations):
    digest = hashlib.sha256()
    for invocation in sorted(
        invocations,
        key=lambda row: row.get("invocation_id") or "",
    ):
        digest.update(
            json.dumps(
                {
                    column: invocation.get(column)
                    for column in _INVOCATION_COLUMNS
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _experiment_result_digest(results):
    digest = hashlib.sha256()
    for item in sorted(
        results,
        key=lambda value: value["experiment_run_id"],
    ):
        digest.update(
            _json_dumps(
                {
                    "experiment_run_id": item["experiment_run_id"],
                    "bundle_sha256": item["bundle_sha256"],
                    "projection_identity_sha256": item[
                        "projection_identity_sha256"
                    ],
                    "bundle": item["bundle"],
                }
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _experiment_recovery_digest(snapshots):
    digest = hashlib.sha256()
    for item in sorted(
        snapshots,
        key=lambda value: value["experiment_run_id"],
    ):
        digest.update(
            _json_dumps(
                {
                    "experiment_run_id": item["experiment_run_id"],
                    "snapshot_sequence": item["snapshot_sequence"],
                    "snapshot_sha256": item["snapshot_sha256"],
                    "snapshot": item["snapshot"],
                }
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _evidence_projections(run_id, state, state_path):
    rows = []
    for step in state.get("steps", []):
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        if not any(
            result.get(key)
            for key in [
                "evidence_level",
                "evidence_status",
                "trace_carrier",
                "missing_evidence",
            ]
        ):
            continue
        row_payload = {
            "run_id": run_id,
            "task_id": step.get("task_id") or result.get("task_id"),
            "attempt_id": result.get("attempt_id"),
            "evidence_level": result.get("evidence_level"),
            "evidence_status": result.get("evidence_status"),
            "trace_carrier": result.get("trace_carrier", []),
            "missing_evidence": result.get("missing_evidence", []),
        }
        content = json.dumps(row_payload, sort_keys=True).encode("utf-8")
        rows.append(
            (
                row_payload["run_id"],
                row_payload["task_id"],
                row_payload["attempt_id"],
                row_payload["evidence_level"],
                row_payload["evidence_status"],
                json.dumps(row_payload["trace_carrier"], sort_keys=True),
                json.dumps(row_payload["missing_evidence"], sort_keys=True),
                str(Path(state_path).resolve()) if Path(state_path).exists() else None,
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
        )
    return rows

import json
import hashlib
import os
import sqlite3
from pathlib import Path

from .token_usage import normalize_token_usage, token_usage_from_state

PROJECTION_SCHEMA_VERSION = "agentteam_projection.v3"


def project_projection_db_path(work_root):
    return Path(work_root).resolve() / "agentteam.db"


def rebuild_project_projection_db(work_root):
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    db_path = project_projection_db_path(work_root)
    temp_path = db_path.with_suffix(".db.tmp")
    projection = _scan_work_root(work_root)
    if temp_path.exists():
        temp_path.unlink()
    try:
        with sqlite3.connect(temp_path) as connection:
            _create_projection_schema(connection)
            _write_projection_rows(connection, projection)
        os.replace(temp_path, db_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return {
        "db_status": "rebuilt",
        "db_path": str(db_path),
        "schema_version": PROJECTION_SCHEMA_VERSION,
        **_projection_counts(projection),
    }


def check_project_projection_db(work_root):
    work_root = Path(work_root).resolve()
    db_path = project_projection_db_path(work_root)
    expected = _projection_counts(_scan_work_root(work_root))
    if not db_path.exists():
        return {
            "check_status": "failed",
            "db_path": str(db_path),
            "schema_version": None,
            "expected": expected,
            "actual": {},
            "mismatches": ["db_missing"],
        }
    try:
        actual = _database_counts(db_path)
        schema_version = _database_schema_version(db_path)
    except sqlite3.DatabaseError as exc:
        return {
            "check_status": "failed",
            "db_path": str(db_path),
            "schema_version": None,
            "expected": expected,
            "actual": {},
            "mismatches": ["db_unreadable"],
            "error": str(exc),
        }
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
    ]
    mismatches = [
        key
        for key in mismatch_keys
        if expected.get(key) != actual.get(key)
    ]
    if schema_version != PROJECTION_SCHEMA_VERSION:
        mismatches.append("schema_version")
    return {
        "check_status": "failed" if mismatches else "passed",
        "db_path": str(db_path),
        "schema_version": schema_version,
        "expected": expected,
        "actual": actual,
        "mismatches": mismatches,
    }


def read_projected_taskpacks(work_root):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return None
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
    except sqlite3.DatabaseError:
        return None
    return {
        "projection_source": "db",
        "db_path": str(db_path),
        "check": check,
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


def read_projected_run_events(work_root, run_id):
    check = check_project_projection_db(work_root)
    if check["check_status"] != "passed":
        return None
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
    except sqlite3.DatabaseError:
        return None
    events = []
    for row in rows:
        try:
            events.append(json.loads(row[0]))
        except (TypeError, json.JSONDecodeError):
            continue
    return {
        "projection_source": "db",
        "db_path": str(db_path),
        "check": check,
        "events": events,
    }


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
    except sqlite3.DatabaseError:
        return None
    return {
        "projection_source": "db",
        "plan_status": "ready",
        "db_path": str(db_path),
        "check_status": check["check_status"],
        "deletion_enabled": False,
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
            }
            for row in candidate_rows
        ],
    }


def build_project_stats(work_root):
    work_root = Path(work_root).resolve()
    check = check_project_projection_db(work_root)
    if check["check_status"] == "passed":
        stats = _project_stats_from_database(work_root, check)
        if stats is not None:
            return stats
    projection = _scan_work_root(work_root)
    return _project_stats_from_projection(
        projection,
        projection_source="files",
        check_status=check.get("check_status"),
        db_path=str(project_projection_db_path(work_root)),
    )


def _scan_work_root(work_root):
    runs = _scan_runs(work_root / "runs")
    taskpacks = _scan_taskpacks(work_root / "frozen")
    artifacts = _scan_artifacts(work_root, runs, taskpacks)
    return {
        "runs": runs,
        "taskpacks": taskpacks,
        "artifacts": artifacts,
        "run_stats": _run_stats(runs, artifacts),
    }


def _scan_runs(runs_root):
    if not runs_root.exists():
        return []
    runs = []
    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
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
                "tasks": _task_projections(run_dir.name, state, events),
                "evidence_summaries": _evidence_projections(run_dir.name, state, state_path),
                "token_usage": _run_token_usage(events, state),
            }
        )
    return runs


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
            "evidence": _database_evidence_counts(connection),
        }


def _database_schema_version(db_path):
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "select value from schema_info where key='schema_version'"
        ).fetchone()
    return row[0] if row else None


def _table_count(connection, table_name):
    return connection.execute(f"select count(*) from {table_name}").fetchone()[0]


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


def _project_stats_from_database(work_root, check):
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


def _project_stats_from_projection(projection, *, projection_source, check_status, db_path):
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
):
    return {
        "stats_status": "ok",
        "projection_source": projection_source,
        "check_status": check_status,
        "db_path": db_path,
        "runs": counts.get("runs", 0),
        "taskpacks": counts.get("taskpacks", 0),
        "events": counts.get("events", 0),
        "tasks": counts.get("tasks", 0),
        "evidence_summaries": counts.get("evidence_summaries", 0),
        "evidence": counts.get("evidence", {}),
        "artifacts": {
            "total_count": counts.get("artifacts", 0),
            "total_bytes": counts.get("artifact_bytes", 0),
            "by_type": artifact_types,
            "by_retention": retention_policies,
        },
        "token_usage": token_usage,
    }


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

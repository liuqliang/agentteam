import json
import os
import sqlite3
from pathlib import Path


PROJECTION_SCHEMA_VERSION = "agentteam_projection.v2"


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
    mismatches = [
        key
        for key in ["runs", "taskpacks", "events", "tasks", "evidence_summaries"]
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


def _scan_work_root(work_root):
    return {
        "runs": _scan_runs(work_root / "runs"),
        "taskpacks": _scan_taskpacks(work_root / "frozen"),
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
                "evidence_summaries": _evidence_projections(run_dir.name, state),
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
            missing_evidence_json text
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
            missing_evidence_json
        ) values(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            evidence
            for run in runs
            for evidence in run["evidence_summaries"]
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


def _evidence_projections(run_id, state):
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
        rows.append(
            (
                run_id,
                step.get("task_id") or result.get("task_id"),
                result.get("attempt_id"),
                result.get("evidence_level"),
                result.get("evidence_status"),
                json.dumps(result.get("trace_carrier", []), sort_keys=True),
                json.dumps(result.get("missing_evidence", []), sort_keys=True),
            )
        )
    return rows

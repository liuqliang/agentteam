import json
from pathlib import Path

from .two_phase_scheduler import _operator_report_from_state
from .token_usage import aggregate_token_usage, format_token_usage


TERMINAL_EVENT_TYPES = {
    "run_completed",
    "run_failed",
    "run_timed_out",
    "run_stopped",
}


def build_run_completion_report(run_dir, project=None, write_files=True):
    run_dir = Path(run_dir).resolve()
    events = _read_jsonl_if_exists(run_dir / "events.jsonl")
    terminal_event = _latest_terminal_event(events)
    state = _read_json_if_exists(run_dir / "state" / "two_phase_scheduler_state.json")
    if not state:
        state = _read_json_if_exists(run_dir / "state" / "scheduler_state.json")

    payload = terminal_event.get("payload", {}) if terminal_event else {}
    operator_report = payload.get("operator_report") if isinstance(payload, dict) else None
    if not isinstance(operator_report, dict):
        operator_report = _operator_report_from_state(state) if isinstance(state, dict) else {}
    if not isinstance(operator_report, dict):
        operator_report = {}
    token_usage = operator_report.get("token_usage")
    if not isinstance(token_usage, dict):
        task_reports = operator_report.get("task_reports", [])
        if not isinstance(task_reports, list):
            task_reports = []
        token_usage = aggregate_token_usage(
            [task.get("token_usage") for task in task_reports if isinstance(task, dict)],
            expected_count=len(task_reports),
        )

    report = {
        "report_status": "ready",
        "project": project or "unknown",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "terminal_event_type": terminal_event.get("event_type") if terminal_event else None,
        "run_status": _run_status(payload, state),
        "scheduler_status": _scheduler_status(payload, state),
        "task_count": operator_report.get("task_count", 0),
        "blocked_count": operator_report.get("blocked_count", 0),
        "token_usage": token_usage,
        "operator_report": operator_report,
        "report_path": str(run_dir / "reports" / "final_report.md"),
        "report_json_path": str(run_dir / "reports" / "final_report.json"),
    }
    if write_files:
        _write_report_files(report)
    return report


def render_run_completion_report(report):
    lines = [
        "# AgentTeam Run Report",
        "",
        f"Project: {report.get('project') or 'unknown'}",
        f"Run: {report.get('run_id') or 'unknown'}",
        f"Status: {report.get('run_status') or 'unknown'}",
        f"Scheduler: {report.get('scheduler_status') or 'unknown'}",
        f"Run dir: {report.get('run_dir') or 'unknown'}",
    ]
    terminal_event_type = report.get("terminal_event_type")
    if terminal_event_type:
        lines.append(f"Terminal event: {terminal_event_type}")
    lines.extend(
        [
            "",
            "## Summary",
            f"- Tasks reported: {report.get('task_count', 0)}",
            f"- Blocked tasks: {report.get('blocked_count', 0)}",
            f"- {format_token_usage(report.get('token_usage'))}",
        ]
    )

    task_reports = (
        report.get("operator_report", {}).get("task_reports", [])
        if isinstance(report.get("operator_report"), dict)
        else []
    )
    if not task_reports:
        lines.extend(
            [
                "",
                "## Task Reports",
                "- No operator task reports were found in this run.",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(["", "## Task Reports"])
    for task in task_reports:
        if not isinstance(task, dict):
            continue
        lines.extend(
            [
                "",
                f"### {task.get('task_id') or 'unknown'}",
                f"- Status: {task.get('status') or 'unknown'}",
            ]
        )
        _extend_bullets(lines, "What changed", task.get("what_changed"))
        _extend_bullets(lines, "Changed files", task.get("changed_files"))
        _extend_bullets(lines, "Verification", task.get("verification"))
        if task.get("integration"):
            lines.append(f"- Integration: {task['integration']}")
        if task.get("merge_recommendation"):
            lines.append(f"- Merge: {task['merge_recommendation']}")
        if isinstance(task.get("token_usage"), dict):
            lines.append(f"- {format_token_usage(task.get('token_usage'), label='Tokens')}")
        _extend_bullets(lines, "Next steps", task.get("next_steps"))
    return "\n".join(lines) + "\n"


def concise_report_lines(report, max_tasks=3):
    lines = [
        f"final report: {report.get('report_path') or 'unknown'}",
        (
            "summary: "
            f"status={report.get('run_status') or 'unknown'} "
            f"tasks={report.get('task_count', 0)} "
            f"blocked={report.get('blocked_count', 0)}"
        ),
    ]
    token_usage = report.get("token_usage")
    if isinstance(token_usage, dict):
        lines.append(format_token_usage(token_usage, label="tokens"))
    task_reports = (
        report.get("operator_report", {}).get("task_reports", [])
        if isinstance(report.get("operator_report"), dict)
        else []
    )
    for task in task_reports[:max_tasks]:
        if not isinstance(task, dict):
            continue
        lines.append(
            f"task {task.get('task_id') or 'unknown'}: {task.get('status') or 'unknown'}"
        )
        changed = _text_items(task.get("what_changed"))
        if changed:
            lines.append(f"changed: {changed[0]}")
        next_steps = _text_items(task.get("next_steps"))
        if next_steps:
            lines.append(f"next: {next_steps[0]}")
    return lines


def _write_report_files(report):
    report_path = Path(report["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_run_completion_report(report), encoding="utf-8")
    json_path = Path(report["report_json_path"])
    json_payload = {key: value for key, value in report.items() if key != "markdown"}
    json_path.write_text(json.dumps(json_payload, sort_keys=True), encoding="utf-8")


def _latest_terminal_event(events):
    for event in reversed(events):
        if event.get("event_type") in TERMINAL_EVENT_TYPES:
            return event
    return None


def _run_status(payload, state):
    if isinstance(payload, dict) and payload.get("run_status"):
        return payload["run_status"]
    if isinstance(state, dict) and state.get("scheduler_status"):
        return state["scheduler_status"]
    return "unknown"


def _scheduler_status(payload, state):
    if isinstance(payload, dict) and payload.get("scheduler_status"):
        return payload["scheduler_status"]
    if isinstance(state, dict) and state.get("scheduler_status"):
        return state["scheduler_status"]
    return "unknown"


def _extend_bullets(lines, heading, values):
    items = _text_items(values)
    if not items:
        return
    lines.append(f"- {heading}:")
    lines.extend(f"  - {item}" for item in items)


def _text_items(values):
    if values is None:
        return []
    if isinstance(values, list):
        return [str(item) for item in values if item is not None and str(item)]
    if isinstance(values, tuple):
        return [str(item) for item in values if item is not None and str(item)]
    return [str(values)] if str(values) else []


def _read_jsonl_if_exists(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

import json
from pathlib import Path

from .completion_summary import build_completion_summary, compact_text_items
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
    task_reports = (
        operator_report.get("task_reports", [])
        if isinstance(operator_report.get("task_reports"), list)
        else []
    )
    blocked_count = _effective_blocked_count(
        operator_report.get("blocked_count", 0),
        task_reports,
    )
    integration_baseline = _integration_baseline_summary(run_dir, state)
    run_status = _run_status(payload, state)
    scheduler_status = _scheduler_status(payload, state)
    run_outcome = _run_outcome(run_status, blocked_count)

    report = {
        "report_status": "ready",
        "project": project or "unknown",
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "terminal_event_type": terminal_event.get("event_type") if terminal_event else None,
        "run_status": run_status,
        "run_outcome": run_outcome,
        "scheduler_status": scheduler_status,
        "task_count": operator_report.get("task_count", 0),
        "blocked_count": blocked_count,
        "token_usage": token_usage,
        "completion_summary": build_completion_summary(
            run_id=run_dir.name,
            run_status=run_status,
            task_count=operator_report.get("task_count", 0),
            blocked_count=blocked_count,
            task_reports=task_reports,
            integration_baseline=integration_baseline,
        ),
        "pursue_recap": find_pursue_recap_for_run(run_dir),
        "integration_baseline": integration_baseline,
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
        f"Outcome: {report.get('run_outcome') or report.get('run_status') or 'unknown'}",
        f"Scheduler: {report.get('scheduler_status') or 'unknown'}",
        f"Run dir: {report.get('run_dir') or 'unknown'}",
    ]
    baseline = report.get("integration_baseline") if isinstance(report.get("integration_baseline"), dict) else {}
    if baseline.get("branch"):
        lines.append(f"Integration baseline: {baseline['branch']}")
        lines.append(f"Baseline head: {baseline.get('head_sha') or 'unknown'}")
        if baseline.get("worktree_path"):
            lines.append(f"Baseline worktree: {baseline['worktree_path']}")
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
    pursue_recap = report.get("pursue_recap") if isinstance(report.get("pursue_recap"), dict) else {}
    if pursue_recap:
        lines.extend(["", "## Pursue Recap"])
        lines.append(f"- Pursue: {pursue_recap.get('pursue_id') or 'unknown'}")
        lines.append(f"- Stop reason: {pursue_recap.get('stop_reason') or 'unknown'}")
        lines.append(
            "- Rounds: "
            f"{pursue_recap.get('rounds_completed', 0)}/{pursue_recap.get('max_rounds', 0)}"
        )
        if pursue_recap.get("latest_taskpack_id"):
            lines.append(f"- Latest taskpack: {pursue_recap['latest_taskpack_id']}")
        if pursue_recap.get("latest_report_path"):
            lines.append(f"- Latest report: {pursue_recap['latest_report_path']}")
        if pursue_recap.get("operator_next_action"):
            lines.append(f"- Next action: {pursue_recap['operator_next_action']}")
        _extend_pursue_round_recap_lines(lines, pursue_recap.get("latest_round_recap"))
    summary = report.get("completion_summary") if isinstance(report.get("completion_summary"), dict) else {}
    if summary:
        lines.extend(["", "## Operator Summary"])
        if summary.get("status_line"):
            lines.append(f"- Status: {summary['status_line']}")
        _extend_summary_item(lines, "中文简报", summary.get("chinese_operator_brief"))
        _extend_summary_item(lines, "What changed", summary.get("what_changed"))
        _extend_summary_item(lines, "Changed files", summary.get("changed_files"))
        _extend_summary_item(lines, "Verification", summary.get("verification"))
        if summary.get("integration"):
            lines.append(f"- Integration: {summary['integration']}")
        evidence_status = summary.get("evidence_status_counts")
        if isinstance(evidence_status, dict) and any(evidence_status.values()):
            lines.append(
                "- Evidence status: "
                + ", ".join(
                    f"{status}={evidence_status.get(status, 0)}"
                    for status in ["complete", "incomplete", "blocked", "escalated"]
                    if evidence_status.get(status, 0)
                )
            )
        if summary.get("integration_recommendation"):
            lines.append(f"- Integration recommendation: {summary['integration_recommendation']}")
        review_gate = summary.get("review_gate")
        if isinstance(review_gate, dict) and review_gate:
            lines.append("- Review gate:")
            for key in [
                "status",
                "integration_branch",
                "base_head",
                "baseline_head",
                "integration_worktree",
                "report_command",
                "paths_command",
                "diff_command",
                "integrate_command",
                "operator_note",
            ]:
                value = review_gate.get(key)
                if value:
                    lines.append(f"  - {key}: {value}")
        _extend_summary_item(lines, "Next", summary.get("next_steps"))
        _extend_summary_item(lines, "Evidence gaps", summary.get("evidence_gaps"))

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
        if task.get("evidence_status"):
            lines.append(
                f"- Evidence: {task.get('evidence_level') or 'unknown'} "
                f"{task['evidence_status']}"
            )
        if task.get("missing_evidence"):
            _extend_bullets(lines, "Missing evidence", task.get("missing_evidence"))
        if task.get("merge_recommendation"):
            lines.append(f"- Merge: {task['merge_recommendation']}")
        if task.get("agentteam_target_review_required"):
            lines.append(
                "- AgentTeam target review: source merge, push, and release activation require operator review."
            )
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
    pursue_recap = report.get("pursue_recap") if isinstance(report.get("pursue_recap"), dict) else {}
    if pursue_recap:
        lines.append(
            "pursue: "
            f"{pursue_recap.get('pursue_id') or 'unknown'} "
            f"stopped because {pursue_recap.get('stop_reason') or 'unknown'}"
        )
        lines.append(
            "pursue_rounds: "
            f"{pursue_recap.get('rounds_completed', 0)}/{pursue_recap.get('max_rounds', 0)}"
        )
        if pursue_recap.get("latest_taskpack_id"):
            lines.append(f"pursue_latest_taskpack: {pursue_recap['latest_taskpack_id']}")
        if pursue_recap.get("operator_next_action"):
            lines.append(f"pursue_next_action: {pursue_recap['operator_next_action']}")
        lines.extend(_concise_pursue_round_recap_lines(pursue_recap.get("latest_round_recap")))
    summary = report.get("completion_summary") if isinstance(report.get("completion_summary"), dict) else {}
    for brief_line in _text_items(summary.get("chinese_operator_brief"))[:3]:
        lines.append(f"中文简报: {brief_line}")
    for digest_line in _text_items(summary.get("operator_digest"))[:6]:
        lines.append(f"中文工作汇报: {digest_line}")
    changed = compact_text_items(summary.get("what_changed"))
    if changed:
        lines.append(f"changed: {changed}")
    changed_files = compact_text_items(summary.get("changed_files"))
    if changed_files:
        lines.append(f"changed_files: {changed_files}")
    verification = compact_text_items(summary.get("verification"))
    if verification:
        lines.append(f"verification: {verification}")
    if summary.get("integration"):
        lines.append(f"integration: {summary['integration']}")
    evidence_status = summary.get("evidence_status_counts")
    if isinstance(evidence_status, dict):
        nonzero = [
            f"{status}={evidence_status.get(status, 0)}"
            for status in ["complete", "incomplete", "blocked", "escalated"]
            if evidence_status.get(status, 0)
        ]
        if nonzero:
            lines.append(f"evidence_status: {', '.join(nonzero)}")
    if summary.get("integration_recommendation"):
        lines.append(f"integration_recommendation: {summary['integration_recommendation']}")
    follow_up = summary.get("follow_up_recommendation")
    if isinstance(follow_up, dict) and follow_up.get("action"):
        lines.append(f"follow_up: {follow_up['action']}")
        command = follow_up.get("next_command") or follow_up.get("integrate_command") or follow_up.get("report_command")
        if command:
            lines.append(f"follow_up_command: {command}")
    review_gate = summary.get("review_gate")
    if isinstance(review_gate, dict) and review_gate:
        lines.append(f"review_gate: {review_gate.get('status') or 'review_gate_required'}")
        for label, key in [
            ("report", "report_command"),
            ("paths", "paths_command"),
            ("diff", "diff_command"),
            ("integrate", "integrate_command"),
        ]:
            command = review_gate.get(key)
            if command:
                lines.append(f"review_{label}: {command}")
    next_step = compact_text_items(summary.get("next_steps"))
    if next_step:
        lines.append(f"next: {next_step}")
    evidence_gap = compact_text_items(summary.get("evidence_gaps"))
    if evidence_gap:
        lines.append(f"evidence_gap: {evidence_gap}")
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
        if task.get("agentteam_target_review_required"):
            lines.append(
                "agentteam_target_review: source merge, push, and release activation require operator review"
            )
    return lines


def _extend_pursue_round_recap_lines(lines, latest_round_recap):
    if not isinstance(latest_round_recap, dict) or not latest_round_recap:
        return
    result = latest_round_recap.get("run_outcome") or latest_round_recap.get("result_status")
    if result:
        lines.append(f"- Latest result: {result}")
    evidence_paths = _evidence_path_texts(latest_round_recap.get("evidence_paths"))
    if evidence_paths:
        lines.append(f"- Evidence: {evidence_paths[0]}")
    blockers = _text_items(latest_round_recap.get("blockers"))
    if blockers:
        lines.append(f"- Blockers: {'; '.join(blockers[:3])}")
    token_usage = latest_round_recap.get("token_usage")
    if isinstance(token_usage, dict):
        lines.append(f"- {format_token_usage(token_usage)}")
    if latest_round_recap.get("recommended_next_step"):
        lines.append(f"- Recommended next step: {latest_round_recap['recommended_next_step']}")


def _concise_pursue_round_recap_lines(latest_round_recap):
    if not isinstance(latest_round_recap, dict) or not latest_round_recap:
        return []
    lines = []
    result = latest_round_recap.get("run_outcome") or latest_round_recap.get("result_status")
    if result:
        lines.append(f"pursue_latest_result: {result}")
    evidence_paths = _evidence_path_texts(latest_round_recap.get("evidence_paths"))
    if evidence_paths:
        lines.append(f"pursue_evidence_path: {evidence_paths[0]}")
    token_usage = latest_round_recap.get("token_usage")
    if isinstance(token_usage, dict):
        lines.append(format_token_usage(token_usage, label="pursue_token_usage"))
    if latest_round_recap.get("recommended_next_step"):
        lines.append(f"pursue_next_step: {latest_round_recap['recommended_next_step']}")
    return lines


def _evidence_path_texts(values):
    paths = []
    seen = set()
    for item in values or []:
        path = item.get("path") if isinstance(item, dict) else item
        text = str(path or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        paths.append(text)
    return paths


def _write_report_files(report):
    report_path = Path(report["report_path"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_run_completion_report(report), encoding="utf-8")
    json_path = Path(report["report_json_path"])
    json_payload = {key: value for key, value in report.items() if key != "markdown"}
    json_path.write_text(json.dumps(json_payload, sort_keys=True), encoding="utf-8")


def _effective_blocked_count(blocked_count, task_reports):
    if blocked_count:
        return blocked_count
    return sum(1 for task in task_reports if _task_needs_operator_review(task))


def _task_needs_operator_review(task):
    status = str(task.get("status") or "").lower()
    integration = str(task.get("integration") or "").lower()
    return (
        "blocked" in status
        or "rejected" in status
        or "failed" in status
        or "timed_out" in status
        or "timed out" in status
        or integration.startswith("failed")
    )


def _run_outcome(run_status, blocked_count):
    if blocked_count and run_status in {"completed", "idle", "stopped"}:
        return "completed_with_review_required"
    if blocked_count:
        return "review_required"
    return run_status or "unknown"


def _latest_terminal_event(events):
    for event in reversed(events):
        if event.get("event_type") in TERMINAL_EVENT_TYPES:
            return event
    return None


def find_pursue_recap_for_run(run_dir):
    run_dir = Path(run_dir).resolve()
    recap_root = _pursue_recap_root_for_run(run_dir)
    if not recap_root.exists():
        return {}
    candidates = []
    for path in recap_root.glob("*.json"):
        try:
            recap = json.loads(path.read_text(encoding="utf-8"))
            modified = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(recap, dict) or not _pursue_recap_mentions_run(recap, run_dir.name):
            continue
        recap = dict(recap)
        recap["recap_path"] = str(path.resolve())
        recap = _augment_pursue_recap_with_goal_memory(recap)
        candidates.append((modified, path.name, recap))
    if not candidates:
        return {}
    return sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]


def _augment_pursue_recap_with_goal_memory(recap):
    if not isinstance(recap, dict):
        return {}
    memory_path = recap.get("goal_memory_path")
    if not memory_path:
        return recap
    memory = _read_json_if_exists(memory_path)
    if not isinstance(memory, dict) or not memory:
        return recap
    enriched = dict(recap)
    latest_round_recap = memory.get("latest_round_recap")
    if isinstance(latest_round_recap, dict) and latest_round_recap:
        enriched.setdefault("latest_round_recap", latest_round_recap)
    latest_queue = memory.get("follow_up_queue")
    if isinstance(latest_queue, list) and latest_queue and "latest_follow_up_queue" not in enriched:
        enriched["latest_follow_up_queue"] = {
            "queue_status": "ready",
            "item_count": len(latest_queue),
            "next_goal": latest_queue[0].get("objective") if isinstance(latest_queue[0], dict) else None,
        }
    return enriched


def _pursue_recap_root_for_run(run_dir):
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent / "pursue"
    return run_dir.parent / "pursue"


def _pursue_recap_mentions_run(recap, run_id):
    if recap.get("latest_taskpack_id") == run_id:
        return True
    runs = recap.get("runs")
    if isinstance(runs, list):
        for item in runs:
            if isinstance(item, dict) and item.get("taskpack_id") == run_id:
                return True
    return False


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


def _integration_baseline_summary(run_dir, state):
    baseline = state.get("integration_baseline") if isinstance(state, dict) else {}
    if not isinstance(baseline, dict):
        baseline = {}
    worktree_path = baseline.get("integration_baseline_worktree_path")
    fallback_worktree = (run_dir / "integration-baseline").resolve()
    if not worktree_path and fallback_worktree.exists():
        worktree_path = str(fallback_worktree)
    branch = baseline.get("integration_baseline_branch")
    if not branch and worktree_path:
        branch = f"agentteam/run/{run_dir.name}/integration"
    return {
        "branch": branch,
        "worktree_path": worktree_path,
        "worktree_exists": Path(worktree_path).exists() if worktree_path else False,
        "base_sha": _integration_base_sha_from_state(state),
        "head_sha": baseline.get("integration_baseline_head_sha"),
    }


def _integration_base_sha_from_state(state):
    steps = state.get("steps") if isinstance(state, dict) else []
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if isinstance(result, dict) and result.get("integration_base_sha"):
            return result["integration_base_sha"]
    return None


def _extend_summary_item(lines, heading, values):
    items = _text_items(values)
    if not items:
        return
    if len(items) == 1:
        lines.append(f"- {heading}: {items[0]}")
        return
    lines.append(f"- {heading}:")
    lines.extend(f"  - {item}" for item in items)


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


def _first_text(values):
    items = _text_items(values)
    return items[0] if items else None


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

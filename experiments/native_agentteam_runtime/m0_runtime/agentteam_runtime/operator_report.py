import json
from pathlib import Path

from .completion_summary import build_completion_summary, compact_text_items
from .projection_db import project_projection_db_path, read_projected_follow_up_lineage
from .two_phase_scheduler import _operator_report_from_state
from .token_usage import aggregate_token_usage, format_token_usage


TERMINAL_EVENT_TYPES = {
    "run_completed",
    "run_failed",
    "run_timed_out",
    "run_stopped",
}

MODEL_INVOCATION_START_EVENT_TYPE = "model_invocation_started"
MODEL_INVOCATION_USAGE_EVENT_TYPE = "model_invocation_usage_recorded"
MODEL_INVOCATION_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
MODEL_INVOCATION_USAGE_STATUSES = (
    "reported",
    "partial",
    "unavailable",
    "not_applicable",
)


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
    if isinstance(state, dict):
        blocked_count = max(
            blocked_count,
            _operator_report_from_state(state).get("blocked_count", 0),
        )
    integration_baseline = _integration_baseline_summary(run_dir, state)
    worker_diagnostics = _worker_diagnostics_for_run(run_dir)
    run_status = _run_status(payload, state)
    scheduler_status = _scheduler_status(payload, state)
    run_outcome = _run_outcome(run_status, blocked_count)
    model_invocation_usage = aggregate_model_invocation_usage(events)
    if (
        isinstance(model_invocation_usage, dict)
        and model_invocation_usage.get("completion_status")
        == "blocked_open_invocations"
    ):
        run_outcome = "blocked_open_invocations"

    pursue_recap = (
        _projected_pursue_recap_for_run(run_dir)
        or find_pursue_recap_for_run(run_dir)
    )
    pursue_recap = _augment_pursue_recap_with_structured_evidence(pursue_recap)

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
        "model_invocation_usage": model_invocation_usage,
        "legacy_task_token_usage": {
            "accounting_scope": "legacy_task_results",
            "benchmark_counted": False,
            "usage": token_usage,
        },
        "completion_summary": build_completion_summary(
            run_id=run_dir.name,
            run_status=run_status,
            task_count=operator_report.get("task_count", 0),
            blocked_count=blocked_count,
            task_reports=task_reports,
            integration_baseline=integration_baseline,
        ),
        "pursue_recap": pursue_recap,
        "integration_baseline": integration_baseline,
        "worker_diagnostics": worker_diagnostics,
        "operator_report": operator_report,
        "report_path": str(run_dir / "reports" / "final_report.md"),
        "report_json_path": str(run_dir / "reports" / "final_report.json"),
    }
    if write_files:
        _write_report_files(report)
    return report


def aggregate_model_invocation_usage(events):
    starts = {}
    terminals_by_event_id = {}
    integrity_conflicts = 0
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if event_type not in {
            MODEL_INVOCATION_START_EVENT_TYPE,
            MODEL_INVOCATION_USAGE_EVENT_TYPE,
        }:
            continue
        record = _model_invocation_record(event)
        if not isinstance(record, dict):
            continue
        invocation_id = record.get("invocation_id")
        if not invocation_id:
            continue
        if event_type == MODEL_INVOCATION_START_EVENT_TYPE:
            previous = starts.get(invocation_id)
            if previous is not None and previous != record:
                integrity_conflicts += 1
                continue
            starts.setdefault(invocation_id, record)
            continue
        usage_event_id = record.get("usage_event_id") or event.get("source_event_id")
        if not usage_event_id:
            continue
        previous = terminals_by_event_id.get(usage_event_id)
        if previous is not None and previous != record:
            integrity_conflicts += 1
            continue
        terminals_by_event_id.setdefault(usage_event_id, record)

    if not starts:
        return None

    terminals = {}
    for terminal in terminals_by_event_id.values():
        invocation_id = terminal.get("invocation_id")
        previous = terminals.get(invocation_id)
        if previous is not None and previous != terminal:
            integrity_conflicts += 1
            continue
        terminals.setdefault(invocation_id, terminal)

    usage_status_counts = {
        status: 0 for status in MODEL_INVOCATION_USAGE_STATUSES
    }
    terminal_status_counts = {}
    stage_breakdown = {}
    reason_counts = {"partial": {}, "unavailable": {}}
    reported_records = []
    eligible_partial_records = []
    supported_count = 0
    supported_terminal_count = 0
    supported_reported_count = 0
    open_invocation_ids = []

    for invocation_id, start in sorted(starts.items()):
        terminal = terminals.get(invocation_id)
        coverage_class = start.get("coverage_class")
        supported = coverage_class == "supported_model_invocation"
        if supported:
            supported_count += 1
        if terminal is None:
            open_invocation_ids.append(invocation_id)
        elif supported:
            supported_terminal_count += 1

        usage_status = terminal.get("usage_status") if terminal else "open"
        if usage_status in usage_status_counts:
            usage_status_counts[usage_status] += 1
        if terminal:
            terminal_status = terminal.get("terminal_status") or "unknown"
            terminal_status_counts[terminal_status] = (
                terminal_status_counts.get(terminal_status, 0) + 1
            )
        if supported and usage_status == "reported":
            supported_reported_count += 1
            reported_records.append(terminal)
        elif usage_status == "partial":
            _add_usage_reason(reason_counts["partial"], terminal)
            if (
                supported
                and terminal.get("provider_usage_scope") == "invocation"
                and any(
                    _is_nonnegative_token_count(terminal.get(field))
                    for field in MODEL_INVOCATION_TOKEN_FIELDS
                )
            ):
                eligible_partial_records.append(terminal)
        elif usage_status == "unavailable":
            _add_usage_reason(reason_counts["unavailable"], terminal)

        stage = (
            start.get("usage_stage")
            or (terminal.get("usage_stage") if terminal else None)
            or "unknown"
        )
        stage_summary = stage_breakdown.setdefault(
            stage,
            {
                "invocation_count": 0,
                "reported": 0,
                "partial": 0,
                "unavailable": 0,
                "not_applicable": 0,
                "open": 0,
            },
        )
        stage_summary["invocation_count"] += 1
        if usage_status in stage_summary:
            stage_summary[usage_status] += 1

    reported_totals = _sum_model_invocation_token_fields(reported_records)
    partial_lower_bounds = _sum_model_invocation_token_fields(
        eligible_partial_records
    )
    observed_lower_bound = _add_model_invocation_token_totals(
        reported_totals,
        partial_lower_bounds,
    )
    lifecycle_coverage = _model_invocation_coverage(
        supported_terminal_count,
        supported_count,
    )
    token_coverage = _model_invocation_coverage(
        supported_reported_count,
        supported_count,
    )
    open_supported_count = sum(
        1
        for invocation_id in open_invocation_ids
        if starts[invocation_id].get("coverage_class")
        == "supported_model_invocation"
    )
    if integrity_conflicts:
        completion_status = "blocked_integrity_conflict"
    elif open_invocation_ids:
        completion_status = "blocked_open_invocations"
    else:
        completion_status = "complete"
    if supported_count == 0:
        reported_totals_scope = "not_applicable"
    elif supported_reported_count == supported_count:
        reported_totals_scope = "all_supported_invocations"
    else:
        reported_totals_scope = "reported_subset"

    return {
        "summary_schema_version": "model_invocation_usage_summary.v1",
        "summary_status": "available",
        "accounting_scope": "full_run_canonical_invocations",
        "benchmark_counted": True,
        "invocation_count": len(starts),
        "terminal_invocation_count": sum(
            1 for invocation_id in starts if invocation_id in terminals
        ),
        "supported_invocation_count": supported_count,
        "open_invocations": len(open_invocation_ids),
        "open_supported_invocations": open_supported_count,
        "usage_status_counts": usage_status_counts,
        "terminal_status_counts": dict(sorted(terminal_status_counts.items())),
        "stage_breakdown": dict(sorted(stage_breakdown.items())),
        "lifecycle_terminal_coverage": lifecycle_coverage,
        "token_usage_coverage": token_coverage,
        "reported_totals_scope": reported_totals_scope,
        "reported_token_totals": {
            **reported_totals,
            "contributing_invocation_count": len(reported_records),
        },
        "partial_known_token_lower_bounds": {
            **partial_lower_bounds,
            "contributing_invocation_count": len(eligible_partial_records),
        },
        "observed_token_lower_bound": observed_lower_bound,
        "reason_counts": {
            status: _bounded_usage_reason_counts(counts)
            for status, counts in reason_counts.items()
        },
        "completion_status": completion_status,
        "benchmark_ready": (
            supported_count > 0
            and supported_terminal_count == supported_count
            and supported_reported_count == supported_count
            and not open_invocation_ids
            and not integrity_conflicts
        ),
        "integrity_conflict_count": integrity_conflicts,
    }


def compact_model_invocation_usage_lines(summary):
    if not isinstance(summary, dict) or summary.get("summary_status") != "available":
        return []
    counts = (
        summary.get("usage_status_counts")
        if isinstance(summary.get("usage_status_counts"), dict)
        else {}
    )
    lifecycle = _coverage_text(summary.get("lifecycle_terminal_coverage"))
    token_coverage = _coverage_text(summary.get("token_usage_coverage"))
    lines = [
        (
            "Model invocations: "
            f"total={summary.get('invocation_count', 0)} "
            f"supported={summary.get('supported_invocation_count', 0)} "
            f"reported={counts.get('reported', 0)} "
            f"partial={counts.get('partial', 0)} "
            f"unavailable={counts.get('unavailable', 0)} "
            f"not_applicable={counts.get('not_applicable', 0)} "
            f"open={summary.get('open_invocations', 0)}"
        ),
        (
            "Invocation coverage: "
            f"lifecycle={lifecycle} token_usage={token_coverage} "
            f"completion={summary.get('completion_status') or 'unknown'}"
        ),
        _compact_invocation_tokens_line(
            "Reported tokens",
            summary.get("reported_token_totals"),
            qualifier=(
                f"exact; scope={summary.get('reported_totals_scope') or 'unknown'}"
            ),
        ),
        _compact_invocation_tokens_line(
            "Partial known token lower bounds",
            summary.get("partial_known_token_lower_bounds"),
            qualifier="invocation-scoped only",
        ),
        _compact_invocation_tokens_line(
            "Observed token lower bound",
            summary.get("observed_token_lower_bound"),
            qualifier="not a benchmark total",
        ),
    ]
    stages = summary.get("stage_breakdown")
    if isinstance(stages, dict) and stages:
        lines.append(
            "Invocation stages: "
            + ", ".join(
                f"{stage}={details.get('invocation_count', 0)}"
                for stage, details in sorted(stages.items())
                if isinstance(details, dict)
            )
        )
    reason_text = _compact_usage_reasons(summary.get("reason_counts"))
    if reason_text:
        lines.append(f"Usage reasons: {reason_text}")
    return lines


def _model_invocation_record(event):
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    for key in ("record", "model_invocation", "model_invocation_usage"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def _add_usage_reason(counts, terminal):
    reason = terminal.get("unavailable_reason") or "unspecified"
    reason = " ".join(str(reason).split())
    if len(reason) > 80:
        reason = reason[:77] + "..."
    counts[reason] = counts.get(reason, 0) + 1


def _bounded_usage_reason_counts(counts, limit=3):
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    selected = [
        {"reason": reason, "count": count}
        for reason, count in ordered[:limit]
    ]
    omitted_count = sum(count for _reason, count in ordered[limit:])
    if omitted_count:
        selected.append({"reason": "other", "count": omitted_count})
    return selected


def _sum_model_invocation_token_fields(records):
    totals = {}
    for field in MODEL_INVOCATION_TOKEN_FIELDS:
        values = [
            record.get(field)
            for record in records
            if isinstance(record, dict)
            and _is_nonnegative_token_count(record.get(field))
        ]
        totals[field] = sum(values) if values else None
    return totals


def _add_model_invocation_token_totals(exact_totals, partial_totals):
    combined = {}
    for field in MODEL_INVOCATION_TOKEN_FIELDS:
        known = [
            totals.get(field)
            for totals in (exact_totals, partial_totals)
            if _is_nonnegative_token_count(totals.get(field))
        ]
        combined[field] = sum(known) if known else None
    return combined


def _is_nonnegative_token_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _model_invocation_coverage(covered, total):
    return {
        "covered": covered,
        "total": total,
        "percent": round((covered * 100.0) / total, 2) if total else None,
        "status": (
            "not_applicable"
            if total == 0
            else "complete"
            if covered == total
            else "partial"
        ),
    }


def _coverage_text(coverage):
    if not isinstance(coverage, dict):
        return "unavailable"
    total = coverage.get("total")
    covered = coverage.get("covered")
    if total == 0:
        return "not_applicable"
    percent = coverage.get("percent")
    if covered is None or total is None or percent is None:
        return "unavailable"
    return f"{covered}/{total} ({percent:g}%)"


def _compact_invocation_tokens_line(label, totals, qualifier):
    totals = totals if isinstance(totals, dict) else {}
    parts = []
    for field, display in (
        ("total_tokens", "total"),
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cached_input_tokens", "cached"),
        ("reasoning_tokens", "reasoning"),
    ):
        value = totals.get(field)
        if _is_nonnegative_token_count(value):
            parts.append(f"{display}={value}")
    contributing = totals.get("contributing_invocation_count")
    if _is_nonnegative_token_count(contributing):
        parts.append(f"invocations={contributing}")
    values = " ".join(parts) if parts else "unavailable"
    return f"{label} ({qualifier}): {values}"


def _compact_usage_reasons(reason_counts):
    if not isinstance(reason_counts, dict):
        return None
    sections = []
    for status in ("partial", "unavailable"):
        values = reason_counts.get(status)
        if not isinstance(values, list) or not values:
            continue
        items = []
        for item in values:
            if not isinstance(item, dict):
                continue
            items.append(
                f"{item.get('reason') or 'unspecified'}={item.get('count', 0)}"
            )
        if items:
            sections.append(f"{status}[{', '.join(items)}]")
    return "; ".join(sections) if sections else None


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
        ]
    )
    invocation_usage = report.get("model_invocation_usage")
    if isinstance(invocation_usage, dict):
        lines.extend(
            f"- {line}"
            for line in compact_model_invocation_usage_lines(invocation_usage)
        )
    lines.append(
        f"- {format_token_usage(report.get('token_usage'))} "
        "(legacy task-result aggregate; not benchmark-counted)"
    )
    pursue_recap = report.get("pursue_recap") if isinstance(report.get("pursue_recap"), dict) else {}
    if pursue_recap:
        lines.extend(["", "## Pursue Recap"])
        lines.append(f"- Pursue: {pursue_recap.get('pursue_id') or 'unknown'}")
        if pursue_recap.get("projection_source"):
            lines.append(f"- Projection source: {pursue_recap['projection_source']}")
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
        _extend_projected_handoff_lines(lines, pursue_recap)
        lines.extend(
            _structured_pursue_evidence_report_lines(
                pursue_recap.get("structured_evidence")
            )
        )
    summary = report.get("completion_summary") if isinstance(report.get("completion_summary"), dict) else {}
    if summary or pursue_recap:
        _extend_chinese_work_report_lines(lines, report, summary, pursue_recap)
    if summary:
        lines.extend(["", "## Operator Summary"])
        if summary.get("status_line"):
            lines.append(f"- Status: {summary['status_line']}")
        _extend_summary_item(lines, "中文简报", summary.get("chinese_operator_brief"))
        _extend_summary_item(lines, "Why", summary.get("why"))
        _extend_summary_item(lines, "What changed", summary.get("what_changed"))
        _extend_summary_item(lines, "Changed files", summary.get("changed_files"))
        _extend_summary_item(lines, "Verification", summary.get("verification"))
        _extend_summary_item(lines, "Risks", summary.get("risks"))
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
                "gate_epoch",
                "gate_id",
                "integration_branch",
                "base_head",
                "baseline_head",
                "historical_scheduler_head",
                "integration_worktree",
                "integration_head_relation",
                "commit_field",
                "report_paths",
                "expected_report_paths",
                "report_command",
                "paths_command",
                "diff_command",
                "approval_command",
                "validated_approval_identity",
                "integrate_command",
                "repair_action",
                "operator_note",
            ]:
                value = review_gate.get(key)
                if value:
                    if isinstance(value, list):
                        lines.append(f"  - {key}:")
                        lines.extend(f"    - {item}" for item in value)
                    else:
                        lines.append(f"  - {key}: {value}")
        _extend_summary_item(lines, "Next", summary.get("next_steps"))
        _extend_summary_item(lines, "Evidence gaps", summary.get("evidence_gaps"))

    _extend_worker_diagnostic_lines(lines, report.get("worker_diagnostics"))

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
        _extend_bullets(lines, "Why", task.get("why"))
        _extend_bullets(lines, "What changed", task.get("what_changed"))
        _extend_bullets(lines, "Changed files", task.get("changed_files"))
        _extend_bullets(lines, "Verification", task.get("verification"))
        _extend_bullets(lines, "Risks", task.get("risks"))
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
            lines.append(
                f"- {format_token_usage(task.get('token_usage'), label='Tokens')} "
                "(legacy task-result; not benchmark-counted)"
            )
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
    task_reports = (
        report.get("operator_report", {}).get("task_reports", [])
        if isinstance(report.get("operator_report"), dict)
        else []
    )
    token_usage = report.get("token_usage")
    invocation_usage = report.get("model_invocation_usage")
    if isinstance(invocation_usage, dict):
        lines.extend(compact_model_invocation_usage_lines(invocation_usage))
    if isinstance(token_usage, dict):
        lines.append(
            format_token_usage(token_usage, label="legacy_task_tokens")
            + " (not benchmark-counted)"
        )
    else:
        lines.append("legacy_task_tokens: unavailable (not benchmark-counted)")
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
        lines.extend(_concise_projected_handoff_lines(pursue_recap))
        lines.extend(
            _concise_structured_pursue_evidence_lines(
                pursue_recap.get("structured_evidence")
            )
        )
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
    completeness_gaps = []
    if not (
        _text_items(summary.get("chinese_operator_brief"))
        or _text_items(summary.get("operator_digest"))
    ):
        completeness_gaps.append("chinese_operator_report")
    if not compact_text_items(summary.get("changed_files")):
        completeness_gaps.append("changed_files")
    if not compact_text_items(summary.get("verification")):
        completeness_gaps.append("verification")
    if not (
        isinstance(follow_up, dict)
        and (follow_up.get("action") or follow_up.get("next_command"))
    ):
        completeness_gaps.append("follow_up")
    if _agentteam_target_review_required(task_reports) and not (
        isinstance(review_gate, dict) and review_gate
    ):
        completeness_gaps.append("review_gate")
    if completeness_gaps:
        lines.append(f"report_completeness: missing={', '.join(completeness_gaps)}")
    next_step = compact_text_items(summary.get("next_steps"))
    if next_step:
        lines.append(f"next: {next_step}")
    evidence_gap = compact_text_items(summary.get("evidence_gaps"))
    if evidence_gap:
        lines.append(f"evidence_gap: {evidence_gap}")
    worker_diagnostics = report.get("worker_diagnostics")
    if isinstance(worker_diagnostics, dict) and worker_diagnostics:
        pool_status = worker_diagnostics.get("pool_diagnostic_status") or "unknown"
        count_text = _diagnostic_counts_text(
            worker_diagnostics.get("diagnostic_worker_counts")
        )
        line = f"worker_diagnostics: pool={pool_status}"
        if count_text:
            line += f" states={count_text}"
        lines.append(line)
        workers = worker_diagnostics.get("workers")
        if isinstance(workers, list):
            for worker in workers[:max_tasks]:
                if not isinstance(worker, dict):
                    continue
                worker_id = (
                    worker.get("worker_agent_id")
                    or worker.get("worker_id")
                    or "unknown-worker"
                )
                diagnostic_state = worker.get("worker_diagnostic_state") or "unknown"
                worker_status = worker.get("worker_status") or "unknown"
                line = (
                    f"worker {worker_id}: diagnostic={diagnostic_state} "
                    f"status={worker_status}"
                )
                if worker.get("heartbeat_progress_summary"):
                    line += f" progress={worker['heartbeat_progress_summary']}"
                lines.append(line)
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


def _agentteam_target_review_required(task_reports):
    for task in task_reports:
        if not isinstance(task, dict):
            continue
        if task.get("agentteam_target_review_required"):
            return True
        if "agentteam_target_review_gate" in _text_items(task.get("required_deliverables")):
            return True
    return False


def _extend_chinese_work_report_lines(lines, report, summary, pursue_recap):
    digest = _text_items(summary.get("operator_digest")) if isinstance(summary, dict) else []
    has_recap = isinstance(pursue_recap, dict) and bool(pursue_recap)
    if not digest and not has_recap and not isinstance(report.get("token_usage"), dict):
        return
    lines.extend(["", "## 中文工作汇报"])
    if has_recap:
        rounds_completed = pursue_recap.get("rounds_completed")
        max_rounds = pursue_recap.get("max_rounds")
        if rounds_completed is not None or max_rounds is not None:
            lines.append(
                f"- 当前轮次：{_count_or_unknown(rounds_completed)}/{_count_or_unknown(max_rounds)}"
            )
        if pursue_recap.get("stop_reason"):
            lines.append(f"- 停止原因：{pursue_recap['stop_reason']}")
        next_step = _pursue_recap_next_step(pursue_recap)
        if next_step:
            lines.append(f"- 下一步：{next_step}")
        queue = pursue_recap.get("latest_follow_up_queue")
        if isinstance(queue, dict) and queue.get("item_count"):
            queue_status = queue.get("queue_status") or "unknown"
            lines.append(
                f"- 投影队列：{queue_status}，{queue['item_count']} 个 follow-up"
            )
            if queue.get("next_goal"):
                lines.append(f"- 投影下一项：{queue['next_goal']}")
    lines.extend(f"- {item}" for item in digest)
    lines.append(
        f"- {format_token_usage(report.get('token_usage'))} "
        "(legacy task-result aggregate; not benchmark-counted)"
    )


def _pursue_recap_next_step(pursue_recap):
    latest_round_recap = pursue_recap.get("latest_round_recap")
    if isinstance(latest_round_recap, dict):
        next_step = _first_text(latest_round_recap.get("recommended_next_step"))
        if next_step:
            return next_step
    return _first_text(pursue_recap.get("operator_next_action"))


def _count_or_unknown(value):
    if value is None:
        return "unknown"
    return value


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
        lines.append(
            f"- {format_token_usage(token_usage)} "
            "(legacy task-result aggregate; not benchmark-counted)"
        )
    if latest_round_recap.get("recommended_next_step"):
        lines.append(f"- Recommended next step: {latest_round_recap['recommended_next_step']}")


def _extend_projected_handoff_lines(lines, pursue_recap):
    queue = pursue_recap.get("latest_follow_up_queue")
    if isinstance(queue, dict) and queue.get("item_count"):
        status = queue.get("queue_status") or "unknown"
        lines.append(f"- Follow-up queue: {status}, items={queue['item_count']}")
        if queue.get("next_goal"):
            lines.append(f"- Next queued goal: {queue['next_goal']}")
    handoff = pursue_recap.get("projection_review_handoff")
    if not isinstance(handoff, dict) or not handoff:
        return
    if handoff.get("source_report_path"):
        lines.append(f"- Source report: {handoff['source_report_path']}")
    evidence_paths = _evidence_path_texts(handoff.get("evidence_paths"))
    if evidence_paths:
        lines.append(f"- Handoff evidence: {evidence_paths[0]}")
    result_parts = []
    if handoff.get("worker_result_status"):
        result_parts.append(f"worker={handoff['worker_result_status']}")
    if handoff.get("worker_evidence_status"):
        result_parts.append(f"evidence={handoff['worker_evidence_status']}")
    if handoff.get("integration_status"):
        result_parts.append(f"integration={handoff['integration_status']}")
    if handoff.get("integration_verification_status"):
        result_parts.append(
            f"integration_verification={handoff['integration_verification_status']}"
        )
    if result_parts:
        lines.append(f"- Handoff status: {', '.join(result_parts)}")
    addition_text = _verification_additions_text(handoff.get("verification_additions"))
    if addition_text:
        lines.append(f"- Verification additions: {addition_text}")


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
        lines.append(
            format_token_usage(token_usage, label="pursue_token_usage")
            + " (legacy task-result aggregate; not benchmark-counted)"
        )
    if latest_round_recap.get("recommended_next_step"):
        lines.append(f"pursue_next_step: {latest_round_recap['recommended_next_step']}")
    return lines


def _concise_projected_handoff_lines(pursue_recap):
    lines = []
    queue = pursue_recap.get("latest_follow_up_queue")
    if isinstance(queue, dict) and queue.get("item_count"):
        lines.append(
            "pursue_queue: "
            f"{queue.get('queue_status') or 'unknown'} items={queue['item_count']}"
        )
        if queue.get("next_goal"):
            lines.append(f"pursue_queue_next_goal: {queue['next_goal']}")
    handoff = pursue_recap.get("projection_review_handoff")
    if not isinstance(handoff, dict) or not handoff:
        return lines
    if handoff.get("source_report_path"):
        lines.append(f"pursue_source_report: {handoff['source_report_path']}")
    addition_text = _verification_additions_text(handoff.get("verification_additions"))
    if addition_text:
        lines.append(f"pursue_verification_additions: {addition_text}")
    return lines


def _augment_pursue_recap_with_structured_evidence(pursue_recap):
    if not isinstance(pursue_recap, dict) or not pursue_recap:
        return pursue_recap
    structured_evidence = _structured_pursue_evidence(pursue_recap)
    if not structured_evidence:
        return pursue_recap
    enriched = dict(pursue_recap)
    enriched["structured_evidence"] = structured_evidence
    return enriched


def _structured_pursue_evidence(pursue_recap):
    latest_round = pursue_recap.get("latest_round_recap")
    if not isinstance(latest_round, dict):
        latest_round = {}
    queue = pursue_recap.get("latest_follow_up_queue")
    if not isinstance(queue, dict):
        queue = {}
    handoff = pursue_recap.get("projection_review_handoff")
    if not isinstance(handoff, dict):
        handoff = {}
    evidence_paths = _evidence_path_items(
        latest_round.get("evidence_paths") or handoff.get("evidence_paths")
    )
    return _compact_dict(
        {
            "schema_version": "pursue_structured_evidence.v1",
            "projection_source": pursue_recap.get("projection_source"),
            "projection_status": pursue_recap.get("projection_status"),
            "projection_db_path": pursue_recap.get("projection_db_path"),
            "latest_taskpack_id": pursue_recap.get("latest_taskpack_id")
            or latest_round.get("taskpack_id"),
            "source_report_path": pursue_recap.get("latest_report_path")
            or handoff.get("source_report_path")
            or latest_round.get("report_path"),
            "goal_memory_path": pursue_recap.get("goal_memory_path"),
            "previous_result_status": latest_round.get("result_status"),
            "previous_run_outcome": latest_round.get("run_outcome"),
            "stop_reason": pursue_recap.get("stop_reason")
            or latest_round.get("stop_reason"),
            "previous_blockers": _text_items(latest_round.get("blockers")),
            "previous_evidence_paths": evidence_paths,
            "selected_next_goal": queue.get("next_goal"),
            "queue_status": queue.get("queue_status"),
            "queue_item_count": queue.get("item_count"),
            "recommended_next_step": latest_round.get("recommended_next_step"),
            "suggested_verification": latest_round.get("suggested_verification"),
            "worker_result_status": handoff.get("worker_result_status"),
            "worker_evidence_status": handoff.get("worker_evidence_status"),
            "integration_status": handoff.get("integration_status"),
            "integration_verification_status": handoff.get(
                "integration_verification_status"
            ),
            "verification_additions": (
                handoff.get("verification_additions")
                if isinstance(handoff.get("verification_additions"), list)
                else None
            ),
        }
    )


def _structured_pursue_evidence_report_lines(structured_evidence):
    if not isinstance(structured_evidence, dict) or not structured_evidence:
        return []
    parts = []
    for key in [
        "previous_result_status",
        "previous_run_outcome",
        "stop_reason",
        "queue_status",
        "queue_item_count",
        "selected_next_goal",
    ]:
        value = structured_evidence.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={value}")
    lines = []
    if parts:
        lines.append(f"- Structured evidence: {', '.join(parts)}")
    path_parts = []
    for label, key in [
        ("source_report", "source_report_path"),
        ("goal_memory", "goal_memory_path"),
    ]:
        value = structured_evidence.get(key)
        if value:
            path_parts.append(f"{label}={value}")
    evidence_paths = _evidence_path_texts(
        structured_evidence.get("previous_evidence_paths")
    )
    if evidence_paths:
        path_parts.append(f"evidence={evidence_paths[0]}")
    if path_parts:
        lines.append(f"- Structured evidence paths: {', '.join(path_parts)}")
    verification_parts = []
    for key in [
        "suggested_verification",
        "worker_evidence_status",
        "integration_verification_status",
    ]:
        value = structured_evidence.get(key)
        if value:
            verification_parts.append(f"{key}={value}")
    addition_text = _verification_additions_text(
        structured_evidence.get("verification_additions")
    )
    if addition_text:
        verification_parts.append(f"verification_additions={addition_text}")
    if verification_parts:
        lines.append(f"- Structured verification: {', '.join(verification_parts)}")
    return lines


def _concise_structured_pursue_evidence_lines(structured_evidence):
    if not isinstance(structured_evidence, dict) or not structured_evidence:
        return []
    parts = []
    for key in ["previous_result_status", "stop_reason", "selected_next_goal"]:
        value = structured_evidence.get(key)
        if value:
            parts.append(f"{key}={value}")
    if not parts:
        return []
    return [f"pursue_structured_evidence: {', '.join(parts)}"]


def _extend_worker_diagnostic_lines(lines, worker_diagnostics):
    if not isinstance(worker_diagnostics, dict) or not worker_diagnostics:
        return
    lines.extend(["", "## Worker Diagnostics"])
    pool_status = worker_diagnostics.get("pool_diagnostic_status") or "unknown"
    registry_status = worker_diagnostics.get("registry_status") or "unknown"
    lines.append(f"- Pool diagnostic: {pool_status}")
    lines.append(f"- Registry status: {registry_status}")
    count_text = _diagnostic_counts_text(
        worker_diagnostics.get("diagnostic_worker_counts")
    )
    if count_text:
        lines.append(f"- Diagnostic states: {count_text}")
    if worker_diagnostics.get("registry_path"):
        lines.append(f"- Registry path: {worker_diagnostics['registry_path']}")
    workers = worker_diagnostics.get("workers")
    if not isinstance(workers, list) or not workers:
        lines.append("- Workers: none")
        return
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        worker_id = worker.get("worker_agent_id") or worker.get("worker_id") or "unknown-worker"
        details = [
            f"diagnostic={worker.get('worker_diagnostic_state') or 'unknown'}",
            f"status={worker.get('worker_status') or 'unknown'}",
        ]
        if worker.get("last_activity"):
            details.append(f"activity={worker['last_activity']}")
        if worker.get("last_poll_status"):
            details.append(f"poll={worker['last_poll_status']}")
        if worker.get("heartbeat_age_seconds") is not None:
            details.append(f"heartbeat_age_seconds={worker['heartbeat_age_seconds']}")
        if worker.get("heartbeat_stale_after_seconds") is not None:
            details.append(
                f"stale_after_seconds={worker['heartbeat_stale_after_seconds']}"
            )
        if worker.get("heartbeat_task_id"):
            details.append(f"task={worker['heartbeat_task_id']}")
        if worker.get("heartbeat_result_status"):
            details.append(f"result={worker['heartbeat_result_status']}")
        if worker.get("heartbeat_progress_summary"):
            details.append(f"progress={worker['heartbeat_progress_summary']}")
        if worker.get("heartbeat_path"):
            details.append(f"heartbeat_path={worker['heartbeat_path']}")
        lines.append(f"- {worker_id}: {' '.join(details)}")


def _worker_diagnostics_for_run(run_dir):
    for path in [
        run_dir / "state" / "worker_process_registry.json",
        run_dir / "state" / "worker_registry.json",
    ]:
        registry = _read_json_if_exists(path)
        if not isinstance(registry, dict) or not registry:
            continue
        workers = registry.get("workers")
        if not isinstance(workers, list):
            workers = []
        return {
            "registry_path": str(path),
            "registry_status": registry.get("registry_status"),
            "pool_diagnostic_status": registry.get("pool_diagnostic_status"),
            "diagnostic_worker_counts": (
                registry.get("diagnostic_worker_counts")
                if isinstance(registry.get("diagnostic_worker_counts"), dict)
                else {}
            ),
            "worker_count": registry.get("worker_count", len(workers)),
            "workers": [worker for worker in workers if isinstance(worker, dict)],
        }
    return {}


def _diagnostic_counts_text(counts):
    if not isinstance(counts, dict):
        return ""
    states = [
        "idle",
        "processing",
        "processed",
        "processing_stale",
        "no_heartbeat",
        "exited",
    ]
    parts = [
        f"{state}={counts.get(state, 0)}"
        for state in states
        if counts.get(state, 0)
    ]
    extra_states = sorted(
        state
        for state, count in counts.items()
        if state not in states and count
    )
    parts.extend(f"{state}={counts[state]}" for state in extra_states)
    return ", ".join(parts)


def _evidence_path_items(values):
    items = []
    seen = set()
    for item in values or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            if not path or path in seen:
                continue
            seen.add(path)
            compact_item = {"path": path}
            item_type = str(item.get("type") or "").strip()
            if item_type:
                compact_item["type"] = item_type
            items.append(compact_item)
            continue
        path = str(item or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        items.append({"path": path})
    return items


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


def _verification_additions_text(values):
    parts = []
    seen = set()
    for item in values or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("verification_addition_id") or "").strip()
        if not label:
            continue
        status = str(
            item.get("verification_addition_status")
            or item.get("status")
            or "unknown"
        ).strip()
        text = f"{label}={status}"
        if text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return ", ".join(parts)


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


def _projected_pursue_recap_for_run(run_dir):
    run_dir = Path(run_dir).resolve()
    work_root = _work_root_for_run(run_dir)
    if not project_projection_db_path(work_root).exists():
        return {}
    lineage = read_projected_follow_up_lineage(
        work_root,
        include_fallback_status=True,
    )
    if not isinstance(lineage, dict) or lineage.get("projection_source") != "db":
        return {}
    items = lineage.get("follow_up_items")
    if not isinstance(items, list) or not items:
        return {}
    item = _projected_follow_up_item_for_run(items, run_dir.name)
    if not item:
        return {}
    return _projected_pursue_recap_from_follow_up_item(
        lineage,
        items,
        item,
        run_dir.name,
    )


def _projected_follow_up_item_for_run(items, run_id):
    candidates = [
        item
        for item in items
        if isinstance(item, dict)
        and (
            item.get("source_run_id") == run_id
            or item.get("source_taskpack_id") == run_id
        )
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            not bool(item.get("selected_next_goal")),
            _int_or_zero(item.get("item_index")),
            item.get("follow_up_id") or "",
        ),
    )[0]


def _projected_pursue_recap_from_follow_up_item(lineage, items, item, run_id):
    taskpack_id = item.get("source_taskpack_id") or item.get("source_run_id") or run_id
    goal_memory_path = item.get("goal_memory_path")
    recap = _compact_dict(
        {
            "projection_source": "db",
            "projection_status": lineage.get("projection_status"),
            "projection_db_path": (
                lineage.get("projection_db_path") or lineage.get("db_path")
            ),
            "pursue_id": _pursue_id_from_goal_memory_path(goal_memory_path) or taskpack_id,
            "latest_taskpack_id": taskpack_id,
            "latest_report_path": item.get("source_report_path"),
            "goal_memory_path": goal_memory_path,
            "stop_reason": item.get("stop_reason"),
            "operator_next_action": (
                f"agentteam report --taskpack {taskpack_id}" if taskpack_id else None
            ),
            "latest_round_recap": _projected_round_recap(item, taskpack_id),
            "latest_follow_up_queue": _projected_follow_up_queue(items, item),
            "projection_review_handoff": _projected_review_handoff(
                lineage,
                item,
                taskpack_id,
            ),
        }
    )
    return recap


def _projected_round_recap(item, taskpack_id):
    return _compact_dict(
        {
            "taskpack_id": taskpack_id,
            "result_status": item.get("source_result_status"),
            "run_outcome": item.get("source_run_outcome"),
            "stop_reason": item.get("stop_reason"),
            "recommended_next_step": item.get("recommended_next_step") or item.get("objective"),
            "suggested_verification": item.get("suggested_verification"),
            "evidence_paths": item.get("source_evidence_paths"),
            "blockers": item.get("blockers"),
            "token_usage": item.get("token_usage"),
        }
    )


def _projected_follow_up_queue(items, selected_item):
    goal_memory_path = selected_item.get("goal_memory_path")
    queue_items = [
        item
        for item in items
        if isinstance(item, dict) and item.get("goal_memory_path") == goal_memory_path
    ]
    if not queue_items:
        return {}
    queue_items = sorted(
        queue_items,
        key=lambda item: (
            _int_or_zero(item.get("item_index")),
            item.get("follow_up_id") or "",
        ),
    )
    return _compact_dict(
        {
            "queue_status": "ready",
            "item_count": len(queue_items),
            "next_goal": queue_items[0].get("objective"),
            "items": [_projected_follow_up_queue_item(item) for item in queue_items],
        }
    )


def _projected_follow_up_queue_item(item):
    return _compact_dict(
        {
            "follow_up_id": item.get("follow_up_id"),
            "objective": item.get("objective"),
            "source": item.get("queue_source") or "goal_memory.follow_up_queue",
            "source_taskpack_id": item.get("source_taskpack_id"),
            "source_report_path": item.get("source_report_path"),
            "goal_memory_path": item.get("goal_memory_path"),
            "readiness": item.get("readiness"),
            "source_result_status": item.get("source_result_status"),
            "source_run_outcome": item.get("source_run_outcome"),
            "stop_reason": item.get("stop_reason"),
            "recommended_next_step": item.get("recommended_next_step"),
            "suggested_verification": item.get("suggested_verification"),
            "source_evidence_paths": item.get("source_evidence_paths"),
            "blockers": item.get("blockers"),
            "token_usage": item.get("token_usage"),
        }
    )


def _projected_review_handoff(lineage, item, taskpack_id):
    worker_result = _latest_projected_worker_result(lineage, taskpack_id)
    integration_outcome = _latest_projected_integration_outcome(lineage, taskpack_id)
    verification_additions = []
    if isinstance(worker_result, dict):
        verification_additions.extend(worker_result.get("verification_additions") or [])
    if isinstance(integration_outcome, dict):
        verification_additions.extend(integration_outcome.get("verification_additions") or [])
    return _compact_dict(
        {
            "source_report_path": item.get("source_report_path"),
            "evidence_paths": item.get("source_evidence_paths"),
            "worker_result_status": (
                worker_result.get("result_status")
                if isinstance(worker_result, dict)
                else None
            ),
            "worker_evidence_status": (
                worker_result.get("evidence_status")
                if isinstance(worker_result, dict)
                else None
            ),
            "integration_status": (
                integration_outcome.get("integration_status")
                if isinstance(integration_outcome, dict)
                else None
            ),
            "integration_verification_status": (
                integration_outcome.get("integration_verification_status")
                if isinstance(integration_outcome, dict)
                else None
            ),
            "verification_additions": verification_additions[:5],
        }
    )


def _latest_projected_worker_result(lineage, taskpack_id):
    return _latest_projected_lineage_row(
        lineage.get("worker_results"),
        taskpack_id,
        "result_status",
    )


def _latest_projected_integration_outcome(lineage, taskpack_id):
    return _latest_projected_lineage_row(
        lineage.get("integration_outcomes"),
        taskpack_id,
        "integration_status",
    )


def _latest_projected_lineage_row(rows, taskpack_id, status_key):
    if not isinstance(rows, list) or not taskpack_id:
        return {}
    candidates = [
        row
        for row in rows
        if isinstance(row, dict)
        and (
            row.get("run_id") == taskpack_id
            or row.get("task_id") == taskpack_id
            or row.get("attempt_id") == taskpack_id
        )
    ]
    if not candidates:
        return {}
    return sorted(
        candidates,
        key=lambda row: (
            row.get(status_key) or "",
            row.get("attempt_id") or "",
            row.get("task_id") or "",
            row.get("run_id") or "",
        ),
    )[-1]


def _pursue_id_from_goal_memory_path(goal_memory_path):
    if not goal_memory_path:
        return None
    stem = Path(goal_memory_path).stem
    suffix = "-goal-memory"
    if stem.endswith(suffix):
        return stem[: -len(suffix)]
    return stem


def _work_root_for_run(run_dir):
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent
    return run_dir.parent


def _int_or_zero(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _compact_dict(payload):
    return {
        key: value
        for key, value in payload.items()
        if value is not None and value != "" and value != [] and value != {}
    }


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

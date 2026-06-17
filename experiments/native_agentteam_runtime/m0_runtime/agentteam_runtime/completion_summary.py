from .operator_brief import build_chinese_next_step_rationale, build_chinese_operator_brief


def build_completion_summary(
    run_id,
    run_status,
    task_count,
    blocked_count,
    task_reports,
    integration_baseline=None,
):
    task_reports = [task for task in task_reports if isinstance(task, dict)]
    blocked_count = _effective_blocked_count(blocked_count, task_reports)
    integration_baseline = integration_baseline if isinstance(integration_baseline, dict) else {}
    what_changed = _unique_limited(
        item
        for task in task_reports
        for item in _text_items(task.get("what_changed"))
    )
    changed_files = _unique_limited(
        [
            item
            for task in task_reports
            for item in _text_items(task.get("changed_files"))
        ],
        limit=12,
    )
    verification = _unique_limited(
        item
        for task in task_reports
        for item in _text_items(task.get("verification"))
    )
    measured_results = _unique_limited(
        item
        for task in task_reports
        for item in (
            _text_items(task.get("measured_result"))
            + _text_items(task.get("measured_results"))
        )
    )
    why = _unique_limited(
        item
        for task in task_reports
        for item in (
            _text_items(task.get("why"))
            + _text_items(task.get("why_changed"))
            + _text_items(task.get("rationale"))
            + _text_items(task.get("goal_alignment"))
        )
    )
    risks = _unique_limited(
        item
        for task in task_reports
        for item in (
            _text_items(task.get("risks"))
            + _text_items(task.get("risk"))
            + _text_items(task.get("risk_summary"))
            + _text_items(task.get("missing_evidence"))
        )
    )
    next_steps = _unique_limited(
        item
        for task in task_reports
        for item in _text_items(task.get("next_steps"))
    )
    merge_recommendations = _unique_limited(
        task.get("merge_recommendation")
        for task in task_reports
        if task.get("merge_recommendation")
    )
    if not what_changed and not task_reports:
        what_changed = ["No task-level operator report was found in this run."]
    integration = _completion_integration(task_reports)
    changed_files_note = _changed_files_note(task_reports, changed_files)
    evidence_gaps = _completion_evidence_gaps(
        what_changed=what_changed,
        changed_files=changed_files,
        verification=verification,
        integration=integration,
        task_reports=task_reports,
    )
    evidence_status_counts = _evidence_status_counts(task_reports)
    summary = {
        "status_line": _completion_status_line(run_status, task_count, blocked_count),
        "what_changed": what_changed,
        "changed_files": changed_files,
        "changed_files_note": changed_files_note,
        "changed_files_note_zh": _changed_files_note_zh(changed_files_note),
        "verification": verification,
        "measured_results": measured_results,
        "why": why,
        "risks": risks,
        "integration": integration,
        "evidence_status_counts": evidence_status_counts,
        "integration_recommendation": _integration_recommendation(
            run_id,
            blocked_count,
            integration_baseline,
            merge_recommendations,
        ),
        "next_steps": next_steps,
        "merge_recommendations": merge_recommendations,
        "evidence_gaps": evidence_gaps,
    }
    summary["follow_up_recommendation"] = _follow_up_recommendation(
        run_id,
        run_status,
        blocked_count,
        integration_baseline,
        summary,
    )
    summary["review_gate"] = _review_gate_guidance(
        run_id,
        integration_baseline,
        summary["follow_up_recommendation"],
    )
    summary["risks"] = _completion_risks(
        summary.get("risks"),
        summary.get("evidence_gaps"),
        summary.get("review_gate"),
    )
    summary["operator_digest"] = _completion_operator_digest(summary)
    summary["chinese_operator_brief"] = build_chinese_operator_brief(
        run_id=run_id,
        run_status=run_status,
        task_count=task_count,
        blocked_count=blocked_count,
        completion_summary=summary,
    )
    return summary


def extend_completion_summary_lines(lines, summary):
    if not isinstance(summary, dict) or not summary:
        return
    lines.append("Completion summary:")
    if summary.get("status_line"):
        lines.append(f"Status: {summary['status_line']}")
    _extend_section(lines, "中文简报:", summary.get("chinese_operator_brief"))
    _extend_section(lines, "中文工作汇报:", summary.get("operator_digest"))
    _extend_section(lines, "Why:", summary.get("why"))
    _extend_section(lines, "What changed:", summary.get("what_changed"))
    _extend_section(lines, "Changed files:", summary.get("changed_files"))
    if not summary.get("changed_files") and summary.get("changed_files_note"):
        lines.append(f"Changed files: {summary['changed_files_note']}")
    _extend_section(lines, "Verification:", summary.get("verification"))
    _extend_section(lines, "Risks:", summary.get("risks"))
    if summary.get("integration"):
        lines.append(f"Integration: {summary['integration']}")
    if summary.get("integration_recommendation"):
        lines.append(f"Integration recommendation: {summary['integration_recommendation']}")
    _extend_follow_up_recommendation(lines, summary.get("follow_up_recommendation"))
    _extend_review_gate(lines, summary.get("review_gate"))
    _extend_section(lines, "Next:", summary.get("next_steps"))
    _extend_section(lines, "Evidence gaps:", summary.get("evidence_gaps"))
    evidence_status_counts = summary.get("evidence_status_counts")
    if isinstance(evidence_status_counts, dict) and any(evidence_status_counts.values()):
        lines.append("Evidence status:")
        for status in ["complete", "incomplete", "blocked", "escalated"]:
            count = evidence_status_counts.get(status, 0)
            if count:
                lines.append(f"- {status}: {count}")


def _completion_operator_digest(summary):
    digest = []
    _append_digest_item(digest, "为什么", summary.get("why"))
    _append_digest_item(digest, "做了什么", summary.get("what_changed"))
    _append_digest_item(
        digest,
        "涉及文件",
        (
            summary.get("changed_files")
            or summary.get("changed_files_note_zh")
            or summary.get("changed_files_note")
        ),
    )
    _append_digest_item(digest, "验证结果", summary.get("verification"))
    _append_digest_item(digest, "实际结果", summary.get("measured_results"))
    _append_digest_item(digest, "风险", summary.get("risks"))
    merge = summary.get("merge_recommendations") or summary.get("integration_recommendation")
    _append_digest_item(digest, "合并建议", merge)
    _append_digest_item(digest, "下一步", summary.get("next_steps"))
    next_step_rationale = build_chinese_next_step_rationale(summary, max_items=1)
    if next_step_rationale:
        digest.append(next_step_rationale)
    _append_digest_item(digest, "证据缺口", summary.get("evidence_gaps"))
    return digest


def compact_text_items(values, limit=3):
    items = _text_items(values)
    if not items:
        return None
    selected = items[:limit]
    text = "；".join(selected)
    omitted_count = len(items) - len(selected)
    if omitted_count > 0:
        text += f"；等 {omitted_count} 项"
    return text


def _follow_up_recommendation(run_id, run_status, blocked_count, integration_baseline, summary):
    next_step = _first_text(summary.get("next_steps"))
    has_integration = bool(integration_baseline.get("branch"))
    if blocked_count:
        return {
            "action": "review_blocker",
            "reason": "A blocked or failed task needs operator review before follow-up work.",
            "report_command": f"agentteam report --taskpack {run_id}",
        }
    if has_integration and next_step:
        return {
            "action": "integrate_then_next",
            "reason": "Accepted changes have an integration baseline and the worker recommended a next step.",
            "integrate_command": f"agentteam integrate --taskpack {run_id}",
            "next_command": f'agentteam next --from-taskpack {run_id} --goal "{_quote_goal(next_step)}"',
        }
    if has_integration:
        return {
            "action": "integrate",
            "reason": "Accepted changes have an integration baseline ready for operator review.",
            "integrate_command": f"agentteam integrate --taskpack {run_id}",
        }
    if next_step and run_status in {"completed", "idle"}:
        return {
            "action": "next",
            "reason": "The run completed with a recommended next implementation step.",
            "next_command": f'agentteam next --from-taskpack {run_id} --goal "{_quote_goal(next_step)}"',
        }
    return {
        "action": "review_report",
        "reason": "No safe automatic follow-up was inferred from the structured report.",
        "report_command": f"agentteam report --taskpack {run_id}",
    }


def _extend_follow_up_recommendation(lines, recommendation):
    if not isinstance(recommendation, dict) or not recommendation:
        return
    lines.append("Follow-up recommendation:")
    for key in ["action", "reason", "integrate_command", "next_command", "report_command"]:
        value = recommendation.get(key)
        if value:
            lines.append(f"- {key}: {value}")


def _review_gate_guidance(run_id, integration_baseline, recommendation):
    if not isinstance(recommendation, dict):
        return {}
    if recommendation.get("action") not in {"integrate", "integrate_then_next"}:
        return {}
    if not isinstance(integration_baseline, dict):
        return {}
    branch = integration_baseline.get("branch")
    if not branch:
        return {}
    worktree = integration_baseline.get("worktree_path")
    base_sha = integration_baseline.get("base_sha")
    baseline_head = integration_baseline.get("head_sha")
    gate = {
        "status": "review_gate_required",
        "integration_branch": branch,
        "base_head": base_sha or "unknown",
        "baseline_head": baseline_head or "unknown",
        "integration_worktree": worktree or "unknown",
        "report_command": f"agentteam report --taskpack {run_id}",
        "paths_command": f"agentteam paths --taskpack {run_id}",
        "integrate_command": recommendation.get("integrate_command")
        or f"agentteam integrate --taskpack {run_id}",
        "operator_note": (
            "Review report, paths, and diff before integrating; source merge, "
            "push, and release activation remain operator decisions."
        ),
    }
    if worktree and base_sha and baseline_head:
        gate["diff_command"] = f"git -C {worktree} diff --stat {base_sha}..{baseline_head}"
    elif worktree and baseline_head:
        gate["diff_command"] = f"git -C {worktree} diff --stat {baseline_head}..HEAD"
    elif worktree:
        gate["diff_command"] = f"git -C {worktree} status --short"
    return gate


def _extend_review_gate(lines, gate):
    if not isinstance(gate, dict) or not gate:
        return
    lines.append("Review gate:")
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
        value = gate.get(key)
        if value:
            lines.append(f"- {key}: {value}")


def _quote_goal(goal):
    return str(goal).replace('"', '\\"')


def _append_digest_item(digest, label, values):
    text = compact_text_items(values)
    if text:
        digest.append(f"{label}：{text}")


def _completion_evidence_gaps(what_changed, changed_files, verification, integration, task_reports=None):
    gaps = []
    if not what_changed:
        gaps.append("No natural-language change summary was reported.")
    if not changed_files and not _all_reports_are_explicit_no_change(task_reports):
        gaps.append("No changed files were reported.")
    if not verification:
        gaps.append("No verification evidence was reported.")
    if integration == "not recorded":
        gaps.append("No integration status was recorded.")
    return gaps


def _changed_files_note(task_reports, changed_files):
    if changed_files:
        return None
    if not _all_reports_are_explicit_no_change(task_reports):
        return None
    return "No source files changed; this task was completed as a no-change investigation."


def _changed_files_note_zh(note):
    if not note:
        return None
    return "未修改源文件；该任务是无需代码变更的调查任务。"


def _all_reports_are_explicit_no_change(task_reports):
    reports = [task for task in task_reports or [] if isinstance(task, dict)]
    return bool(reports) and all(_report_is_explicit_no_change(task) for task in reports)


def _report_is_explicit_no_change(task):
    if "changed_files" not in task or _text_items(task.get("changed_files")):
        return False
    if task.get("no_code_changes_required") is True or task.get("no_source_changes_required") is True:
        return True
    work_type = str(task.get("work_type") or task.get("task_type") or "").strip().lower()
    return work_type in {
        "analysis",
        "audit",
        "code_investigation",
        "diagnostic",
        "investigation",
        "planning",
        "read_only",
        "review",
    }


def _completion_risks(risks, evidence_gaps, review_gate):
    items = _text_items(risks)
    if not items:
        items = _text_items(evidence_gaps)
    if isinstance(review_gate, dict) and review_gate:
        items.append("存在 review gate；source merge、push、release activation 仍需 operator 审阅。")
    return _unique_limited(items)


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


def _evidence_status_counts(task_reports):
    counts = {"complete": 0, "incomplete": 0, "blocked": 0, "escalated": 0}
    for task in task_reports:
        status = task.get("evidence_status")
        if status in counts:
            counts[status] += 1
    return counts


def _completion_status_line(run_status, task_count, blocked_count):
    task_label = "task" if task_count == 1 else "tasks"
    blocked_label = "blocked task" if blocked_count == 1 else "blocked tasks"
    return f"{run_status or 'unknown'}: {task_count} {task_label} reported, {blocked_count} {blocked_label}"


def _completion_integration(task_reports):
    integrations = _unique_limited(
        task.get("integration")
        for task in task_reports
        if task.get("integration")
    )
    if not integrations:
        return "not recorded"
    if any(str(item).startswith("failed") for item in integrations):
        return "blocked"
    if integrations == ["passed"]:
        return "passed"
    return "; ".join(integrations)


def _integration_recommendation(run_id, blocked_count, integration_baseline, merge_recommendations):
    if blocked_count:
        return "Do not merge until integration passes."
    branch = integration_baseline.get("branch")
    if branch:
        return (
            "Review the final report, then run "
            f"`agentteam integrate --taskpack {run_id}` from a clean target repository "
            "if these changes should land."
        )
    if merge_recommendations:
        return merge_recommendations[0]
    return "No integration baseline was recorded; inspect the run report before merging manually."


def _extend_section(lines, heading, values):
    items = _text_items(values)
    if not items:
        return
    lines.append(heading)
    lines.extend(f"- {item}" for item in items)


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


def _unique_limited(values, limit=5):
    seen = set()
    items = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items

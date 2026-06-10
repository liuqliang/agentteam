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
    return {
        "status_line": _completion_status_line(run_status, task_count, blocked_count),
        "what_changed": what_changed,
        "changed_files": changed_files,
        "verification": verification,
        "integration": _completion_integration(task_reports),
        "integration_recommendation": _integration_recommendation(
            run_id,
            blocked_count,
            integration_baseline,
            merge_recommendations,
        ),
        "next_steps": next_steps,
        "merge_recommendations": merge_recommendations,
    }


def extend_completion_summary_lines(lines, summary):
    if not isinstance(summary, dict) or not summary:
        return
    lines.append("Completion summary:")
    if summary.get("status_line"):
        lines.append(f"Status: {summary['status_line']}")
    _extend_section(lines, "What changed:", summary.get("what_changed"))
    _extend_section(lines, "Changed files:", summary.get("changed_files"))
    _extend_section(lines, "Verification:", summary.get("verification"))
    if summary.get("integration"):
        lines.append(f"Integration: {summary['integration']}")
    if summary.get("integration_recommendation"):
        lines.append(f"Integration recommendation: {summary['integration_recommendation']}")
    _extend_section(lines, "Next:", summary.get("next_steps"))


def _effective_blocked_count(blocked_count, task_reports):
    if blocked_count:
        return blocked_count
    return sum(
        1
        for task in task_reports
        if "blocked" in str(task.get("status") or "")
        or str(task.get("integration") or "").startswith("failed")
    )


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

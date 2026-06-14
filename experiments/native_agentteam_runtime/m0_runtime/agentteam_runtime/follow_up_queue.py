FOLLOW_UP_QUEUE_SCHEMA_VERSION = "follow_up_queue.v1"
DEFAULT_QUEUE_LIMIT = 5
DEFAULT_TEXT_LIMIT = 480


def build_follow_up_queue_summary(
    *,
    source_report,
    goal_memory=None,
    source_taskpack_id=None,
    source_run_dir=None,
    limit=DEFAULT_QUEUE_LIMIT,
    text_limit=DEFAULT_TEXT_LIMIT,
):
    source_report = source_report if isinstance(source_report, dict) else {}
    goal_memory = goal_memory if isinstance(goal_memory, dict) else {}
    source_taskpack_id = source_taskpack_id or source_report.get("run_id") or "unknown"
    source_report_path = source_report.get("report_path")
    items = _queue_items_from_report(
        source_report,
        source_taskpack_id=source_taskpack_id,
        source_report_path=source_report_path,
        text_limit=text_limit,
    )
    items.extend(
        _queue_items_from_goal_memory(
            goal_memory,
            fallback_source_taskpack_id=source_taskpack_id,
            fallback_source_report_path=source_report_path,
            text_limit=text_limit,
        )
    )
    items = _dedupe_items(items, limit=max(0, int(limit or 0)))
    next_item = items[0] if items else None
    next_goal = next_item.get("objective") if isinstance(next_item, dict) else None
    summary = {
        "queue_schema_version": FOLLOW_UP_QUEUE_SCHEMA_VERSION,
        "queue_status": "ready" if items else "empty",
        "source_taskpack_id": source_taskpack_id,
        "source_run_dir": source_run_dir,
        "source_report_path": source_report_path,
        "goal_memory_path": goal_memory.get("memory_path"),
        "item_count": len(items),
        "items": items,
        "next_goal": next_goal,
        "next_command": _next_command(source_taskpack_id, next_goal),
    }
    if not items:
        summary["operator_hint"] = "No follow-up queue items were found; inspect the report before continuing."
    return summary


def render_follow_up_queue_text(summary, *, next_only=False):
    summary = summary if isinstance(summary, dict) else {}
    lines = [
        f"queue_status: {summary.get('queue_status') or 'unknown'}",
        f"source_taskpack_id: {summary.get('source_taskpack_id') or 'unknown'}",
    ]
    if summary.get("source_report_path"):
        lines.append(f"source_report: {summary['source_report_path']}")
    if summary.get("goal_memory_path"):
        lines.append(f"goal_memory: {summary['goal_memory_path']}")
    if summary.get("next_goal"):
        lines.append(f"next_goal: {summary['next_goal']}")
    if summary.get("next_command"):
        lines.append(f"next_command: {summary['next_command']}")
    if not next_only:
        for index, item in enumerate(summary.get("items") or [], start=1):
            if not isinstance(item, dict):
                continue
            source = item.get("source") or "unknown"
            objective = item.get("objective") or "unknown"
            lines.append(f"queue_item_{index}: source={source}; goal={objective}")
    if summary.get("operator_hint"):
        lines.append(f"operator_hint: {summary['operator_hint']}")
    return "\n".join(lines) + "\n"


def _queue_items_from_report(source_report, *, source_taskpack_id, source_report_path, text_limit):
    summary = source_report.get("completion_summary") if isinstance(source_report.get("completion_summary"), dict) else {}
    items = []
    for objective in _text_items(summary.get("next_steps")):
        items.append(
            {
                "objective": _bounded_text(objective, text_limit),
                "source": "report.next_steps",
                "source_taskpack_id": source_taskpack_id,
                "source_report_path": source_report_path,
            }
        )
    recommendation = summary.get("follow_up_recommendation")
    if isinstance(recommendation, dict) and recommendation.get("next_command"):
        objective = _goal_from_next_command(recommendation.get("next_command"))
        if objective:
            items.append(
                {
                    "objective": _bounded_text(objective, text_limit),
                    "source": "report.follow_up_recommendation",
                    "source_taskpack_id": source_taskpack_id,
                    "source_report_path": source_report_path,
                    "source_command": recommendation.get("next_command"),
                }
            )
    return items


def _queue_items_from_goal_memory(
    goal_memory,
    *,
    fallback_source_taskpack_id,
    fallback_source_report_path,
    text_limit,
):
    items = []
    for item in goal_memory.get("follow_up_queue") or []:
        if not isinstance(item, dict) or not item.get("objective"):
            continue
        items.append(
            {
                "objective": _bounded_text(item.get("objective"), text_limit),
                "source": "goal_memory.follow_up_queue",
                "source_taskpack_id": item.get("source_taskpack_id") or fallback_source_taskpack_id,
                "source_report_path": item.get("source_report_path") or fallback_source_report_path,
            }
        )
    return items


def _dedupe_items(items, limit):
    deduped = []
    seen = set()
    for item in items:
        objective = item.get("objective")
        source_taskpack_id = item.get("source_taskpack_id")
        if not objective:
            continue
        key = (objective, source_taskpack_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if limit and len(deduped) >= limit:
            break
    return deduped


def _next_command(source_taskpack_id, next_goal):
    if not next_goal:
        return None
    return f'agentteam next --from-taskpack {source_taskpack_id} --goal "{_quote(next_goal)}"'


def _goal_from_next_command(command):
    marker = ' --goal "'
    text = str(command or "")
    if marker not in text:
        return None
    goal = text.split(marker, 1)[1]
    if '"' in goal:
        goal = goal.split('"', 1)[0]
    return goal.strip() or None


def _quote(value):
    return str(value).replace('"', '\\"')


def _bounded_text(value, limit):
    text = str(value or "").strip()
    if not text:
        return ""
    limit = max(1, int(limit or DEFAULT_TEXT_LIMIT))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "...[truncated]"


def _text_items(values):
    if values is None:
        return []
    if isinstance(values, list):
        return [str(item).strip() for item in values if item is not None and str(item).strip()]
    if isinstance(values, tuple):
        return [str(item).strip() for item in values if item is not None and str(item).strip()]
    text = str(values).strip()
    return [text] if text else []

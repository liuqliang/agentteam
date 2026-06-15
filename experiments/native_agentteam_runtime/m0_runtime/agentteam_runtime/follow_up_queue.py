from .token_usage import format_token_usage


FOLLOW_UP_QUEUE_SCHEMA_VERSION = "follow_up_queue.v1"
DEFAULT_QUEUE_LIMIT = 5
DEFAULT_TEXT_LIMIT = 480
TOKEN_USAGE_KEYS = [
    "usage_status",
    "reported_attempt_count",
    "unreported_attempt_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
]
GENERIC_NEXT_STEP_TEXTS = {
    "continue optimization",
    "continue optimizing",
    "continue improving",
    "continue validation",
    "continue validating",
    "keep optimizing",
    "持续优化",
    "继续处理",
    "继续优化",
    "继续完善",
    "继续推进",
    "继续改进",
    "继续验证",
}


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
        "selected_item": next_item,
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
    _append_selected_item_lines(lines, summary.get("selected_item"))
    if not next_only:
        for index, item in enumerate(summary.get("items") or [], start=1):
            if not isinstance(item, dict):
                continue
            source = item.get("source") or "unknown"
            objective = item.get("objective") or "unknown"
            readiness = item.get("readiness") or "unknown"
            lines.append(
                f"queue_item_{index}: source={source}; readiness={readiness}; goal={objective}"
            )
    if summary.get("operator_hint"):
        lines.append(f"operator_hint: {summary['operator_hint']}")
    return "\n".join(lines) + "\n"


def _queue_items_from_report(source_report, *, source_taskpack_id, source_report_path, text_limit):
    raw_summary = source_report.get("completion_summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    items = []
    recommendation = summary.get("follow_up_recommendation")
    recommendation_objective = None
    if isinstance(recommendation, dict) and recommendation.get("next_command"):
        recommendation_objective = _goal_from_next_command(recommendation.get("next_command"))
    concrete_recommendation_exists = bool(
        recommendation_objective and not _is_generic_next_step(recommendation_objective)
    )
    for objective in _text_items(summary.get("next_steps")):
        objective = _specific_next_step_objective(
            objective,
            summary,
            concrete_fallback_exists=concrete_recommendation_exists,
            text_limit=text_limit,
        )
        if not objective:
            continue
        items.append(
            {
                "objective": objective,
                "source": "report.next_steps",
                "source_taskpack_id": source_taskpack_id,
                "source_report_path": source_report_path,
                **_report_item_readiness(summary),
            }
        )
    if isinstance(recommendation, dict) and recommendation.get("next_command"):
        objective = _specific_next_step_objective(
            recommendation_objective,
            summary,
            concrete_fallback_exists=False,
            text_limit=text_limit,
        )
        if objective:
            items.append(
                {
                    "objective": objective,
                    "source": "report.follow_up_recommendation",
                    "source_taskpack_id": source_taskpack_id,
                    "source_report_path": source_report_path,
                    "source_command": recommendation.get("next_command"),
                    **_report_item_readiness(summary),
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
                **_goal_memory_item_readiness(item),
                **_goal_memory_recap_metadata(item, text_limit),
            }
        )
    return items


def _goal_memory_recap_metadata(item, text_limit):
    metadata = {}
    for key in [
        "source_result_status",
        "source_run_outcome",
        "stop_reason",
        "recommended_next_step",
        "suggested_verification",
    ]:
        value = _bounded_text(item.get(key), text_limit)
        if value:
            metadata[key] = value
    evidence_paths = _evidence_path_items(item.get("source_evidence_paths"), text_limit)
    if evidence_paths:
        metadata["source_evidence_paths"] = evidence_paths
    if isinstance(item.get("token_usage"), dict):
        metadata["token_usage"] = _token_usage_copy(item["token_usage"])
    blockers = _text_items(item.get("blockers"))
    if blockers:
        metadata["blockers"] = [_bounded_text(blocker, text_limit) for blocker in blockers[:3]]
    return metadata


def _append_selected_item_lines(lines, item):
    if not isinstance(item, dict):
        return
    if item.get("source"):
        lines.append(f"selected_source: {item['source']}")
    if item.get("source_taskpack_id"):
        lines.append(f"selected_source_taskpack_id: {item['source_taskpack_id']}")
    if item.get("source_report_path"):
        lines.append(f"selected_source_report: {item['source_report_path']}")
    if item.get("source_command"):
        lines.append(f"selected_source_command: {item['source_command']}")
    result = item.get("source_run_outcome") or item.get("source_result_status")
    if result:
        lines.append(f"selected_result: {result}")
    for path in _evidence_path_texts(item.get("source_evidence_paths")):
        lines.append(f"selected_evidence_path: {path}")
    if item.get("stop_reason"):
        lines.append(f"selected_stop_reason: {item['stop_reason']}")
    if isinstance(item.get("token_usage"), dict):
        lines.append(f"selected_token_usage: {format_token_usage(item['token_usage'])}")
    if item.get("readiness"):
        lines.append(f"selected_readiness: {item['readiness']}")
    blockers = _text_items(item.get("blockers"))
    if blockers:
        lines.append(f"selected_blockers: {'；'.join(blockers)}")
    if item.get("suggested_verification"):
        lines.append(f"selected_verification: {item['suggested_verification']}")
    if item.get("recommended_next_step"):
        lines.append(f"selected_recommended_next_step: {item['recommended_next_step']}")


def _report_item_readiness(summary):
    blockers = _text_items(summary.get("evidence_gaps"))
    verification = _text_items(summary.get("verification"))
    metadata = {
        "readiness": "review_needed" if blockers else "ready",
        "blockers": blockers[:3],
    }
    if verification:
        metadata["suggested_verification"] = verification[0]
    return metadata


def _goal_memory_item_readiness(item):
    blockers = _text_items(item.get("blockers")) or _text_items(item.get("blocked_reasons"))
    verification = _text_items(item.get("suggested_verification")) or _text_items(
        item.get("verification")
    )
    readiness = item.get("readiness") or ("review_needed" if blockers else "ready")
    metadata = {
        "readiness": str(readiness).strip() or "ready",
        "blockers": blockers[:3],
    }
    if verification:
        metadata["suggested_verification"] = verification[0]
    return metadata


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


def _specific_next_step_objective(value, summary, *, concrete_fallback_exists, text_limit):
    objective = str(value or "").strip()
    if not objective:
        return None
    if not _is_generic_next_step(objective):
        return _bounded_text(objective, text_limit)
    if concrete_fallback_exists:
        return None
    evidence = _report_evidence_anchor(summary)
    if not evidence:
        return None
    return _bounded_text(f"{objective}（基于上一轮证据：{evidence}）", text_limit)


def _is_generic_next_step(value):
    text = _normalized_next_step_text(value)
    return text in GENERIC_NEXT_STEP_TEXTS


def _normalized_next_step_text(value):
    text = " ".join(str(value or "").strip().lower().split())
    return text.strip(" \t\r\n。.!！,，;；:：")


def _report_evidence_anchor(summary):
    summary = summary if isinstance(summary, dict) else {}
    fragments = []
    changed_files = _text_items(summary.get("changed_files"))
    if changed_files:
        fragments.append(f"changed_files={'；'.join(changed_files[:2])}")
    verification = _text_items(summary.get("verification"))
    if verification:
        fragments.append(f"verification={verification[0]}")
    measured_results = _text_items(summary.get("measured_results")) + _text_items(
        summary.get("measured_result")
    )
    if measured_results:
        fragments.append(f"measured_result={measured_results[0]}")
    what_changed = _text_items(summary.get("what_changed"))
    if what_changed:
        fragments.append(f"what_changed={what_changed[0]}")
    evidence_gaps = _text_items(summary.get("evidence_gaps"))
    if evidence_gaps:
        fragments.append(f"evidence_gap={evidence_gaps[0]}")
    return "；".join(fragments[:3])


def _bounded_text(value, limit):
    text = str(value or "").strip()
    if not text:
        return ""
    limit = max(1, int(limit or DEFAULT_TEXT_LIMIT))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "...[truncated]"


def _evidence_path_items(values, text_limit):
    items = []
    seen = set()
    for value in values or []:
        if isinstance(value, dict):
            path = value.get("path")
            kind = value.get("type") or "evidence"
        else:
            path = value
            kind = "evidence"
        path = _bounded_text(path, text_limit)
        kind = _bounded_text(kind, text_limit)
        if not path or path in seen:
            continue
        seen.add(path)
        items.append({"type": kind or "evidence", "path": path})
        if len(items) >= DEFAULT_QUEUE_LIMIT:
            break
    return items


def _evidence_path_texts(values):
    return [item["path"] for item in _evidence_path_items(values, DEFAULT_TEXT_LIMIT)]


def _token_usage_copy(value):
    return {key: value.get(key) for key in TOKEN_USAGE_KEYS}


def _text_items(values):
    if values is None:
        return []
    if isinstance(values, list):
        return [str(item).strip() for item in values if item is not None and str(item).strip()]
    if isinstance(values, tuple):
        return [str(item).strip() for item in values if item is not None and str(item).strip()]
    text = str(values).strip()
    return [text] if text else []

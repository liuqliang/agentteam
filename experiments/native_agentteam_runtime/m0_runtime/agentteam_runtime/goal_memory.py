import json
from datetime import UTC, datetime
from pathlib import Path

from .token_usage import format_token_usage


GOAL_MEMORY_SCHEMA_VERSION = "goal_memory.v1"
DEFAULT_MAX_ROUND_HISTORY = 5
DEFAULT_MAX_TEXT_CHARS = 480
DEFAULT_MAX_QUEUE_ITEMS = 5
DEFAULT_MAX_MEMORY_JSON_CHARS = 12000
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


def build_goal_memory(
    *,
    pursue_id,
    original_goal,
    work_root,
    rounds,
    source_report=None,
    stop_reason=None,
    previous_memory=None,
    max_round_history=DEFAULT_MAX_ROUND_HISTORY,
    max_text_chars=DEFAULT_MAX_TEXT_CHARS,
    max_queue_items=DEFAULT_MAX_QUEUE_ITEMS,
    max_memory_json_chars=DEFAULT_MAX_MEMORY_JSON_CHARS,
    updated_at=None,
):
    bounded_rounds = _bounded_round_history(
        rounds,
        max_round_history,
        max_text_chars,
        previous_memory=previous_memory,
    )
    latest_round = bounded_rounds[-1] if bounded_rounds else {}
    summary = source_report.get("completion_summary") if isinstance(source_report, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    latest_report_path = (
        source_report.get("report_path")
        if isinstance(source_report, dict) and source_report.get("report_path")
        else latest_round.get("report_path")
    )
    current_hypothesis = _bounded_text(
        _first_non_empty_text(summary.get("what_changed"))
        or _memory_text(previous_memory, "current_hypothesis"),
        max_text_chars,
    )
    current_next_step = _bounded_text(
        _first_non_empty_text(summary.get("next_steps"))
        or _memory_text(previous_memory, "current_next_step")
        or f"Continue pursuing: {original_goal}",
        max_text_chars,
    )
    latest_round_recap = _latest_round_recap(
        latest_round=latest_round,
        source_report=source_report,
        summary=summary,
        stop_reason=stop_reason,
        current_next_step=current_next_step,
        max_text_chars=max_text_chars,
    )
    if latest_round_recap and bounded_rounds:
        bounded_rounds[-1] = {
            **bounded_rounds[-1],
            **_round_history_recap_fields(latest_round_recap, max_text_chars),
        }
        latest_round = bounded_rounds[-1]
    blocked_reasons = _blocked_reasons(
        stop_reason=stop_reason,
        summary=summary,
        previous_memory=previous_memory,
        max_items=max_queue_items,
        max_text_chars=max_text_chars,
    )
    memory = {
        "memory_schema_version": GOAL_MEMORY_SCHEMA_VERSION,
        "pursue_id": str(pursue_id or "pursue"),
        "original_goal": _bounded_text(original_goal, max_text_chars),
        "work_root": str(Path(work_root).resolve()) if work_root else None,
        "rounds_completed": len(rounds or []),
        "latest_taskpack_id": latest_round.get("taskpack_id"),
        "latest_report_path": latest_report_path,
        "latest_run_ids": [
            item["taskpack_id"]
            for item in bounded_rounds
            if item.get("taskpack_id")
        ],
        "current_hypothesis": current_hypothesis,
        "current_next_step": current_next_step,
        "blocked_reasons": blocked_reasons,
        "follow_up_queue": _follow_up_queue(
            current_next_step=current_next_step,
            latest_round=latest_round,
            latest_round_recap=latest_round_recap,
            latest_report_path=latest_report_path,
            previous_memory=previous_memory,
            max_items=max_queue_items,
            max_text_chars=max_text_chars,
        ),
        "round_history": bounded_rounds,
        "latest_round_recap": latest_round_recap,
        "evidence_sources": _evidence_sources(bounded_rounds, latest_report_path),
        "stop_reason": stop_reason,
        "limits": {
            "max_round_history": max_round_history,
            "max_text_chars": max_text_chars,
            "max_queue_items": max_queue_items,
            "max_memory_json_chars": max_memory_json_chars,
        },
        "updated_at": updated_at or _utc_now(),
    }
    return _fit_memory_json(memory, max_memory_json_chars)


def goal_memory_path(work_root, pursue_id):
    return (Path(work_root) / "pursue" / f"{_safe_artifact_id(pursue_id)}-goal-memory.json").resolve()


def write_goal_memory(work_root, memory):
    pursue_id = memory.get("pursue_id") if isinstance(memory, dict) else "pursue"
    path = goal_memory_path(work_root, pursue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(memory)
    payload["memory_path"] = str(path)
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    payload = _fit_memory_json(
        payload,
        limits.get("max_memory_json_chars", DEFAULT_MAX_MEMORY_JSON_CHARS),
    )
    payload["memory_path"] = str(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def render_goal_memory_prompt_context(memory):
    if not isinstance(memory, dict) or not memory:
        return ""
    latest_run_ids = ", ".join(str(item) for item in memory.get("latest_run_ids", []) if item)
    blocked_reasons = "; ".join(str(item) for item in memory.get("blocked_reasons", []) if item)
    queue = memory.get("follow_up_queue") if isinstance(memory.get("follow_up_queue"), list) else []
    queue_lines = []
    for index, item in enumerate(queue, start=1):
        if not isinstance(item, dict):
            continue
        objective = item.get("objective")
        source = item.get("source_taskpack_id")
        if objective:
            suffix = f" source={source}" if source else ""
            queue_lines.append(f"- queued_next_step_{index}: {objective}{suffix}")
    limits = memory.get("limits") if isinstance(memory.get("limits"), dict) else {}
    lines = [
        "Long-goal memory:",
        f"- memory_path: {memory.get('memory_path') or goal_memory_path(memory.get('work_root') or '.', memory.get('pursue_id') or 'pursue')}",
        f"- original_goal: {memory.get('original_goal') or 'unknown'}",
        f"- completed_rounds: {memory.get('rounds_completed', 0)}",
        f"- latest_run_ids: {latest_run_ids or 'none'}",
        f"- latest_report_path: {memory.get('latest_report_path') or 'unknown'}",
        f"- current_hypothesis: {memory.get('current_hypothesis') or 'unknown'}",
        f"- current_next_step: {memory.get('current_next_step') or 'continue'}",
        f"- blocked_reasons: {blocked_reasons or 'none'}",
        (
            "- memory_bounds: "
            f"round_history<={limits.get('max_round_history', DEFAULT_MAX_ROUND_HISTORY)} "
            f"text<={limits.get('max_text_chars', DEFAULT_MAX_TEXT_CHARS)} "
            f"queue<={limits.get('max_queue_items', DEFAULT_MAX_QUEUE_ITEMS)}"
        ),
    ]
    if queue_lines:
        lines.extend(queue_lines)
    latest_recap = memory.get("latest_round_recap") if isinstance(memory.get("latest_round_recap"), dict) else {}
    if latest_recap:
        result = latest_recap.get("run_outcome") or latest_recap.get("result_status")
        if result:
            lines.append(f"- latest_result: {result}")
        for path in _evidence_path_texts(latest_recap.get("evidence_paths"))[:3]:
            lines.append(f"- latest_evidence_path: {path}")
        if latest_recap.get("stop_reason"):
            lines.append(f"- latest_stop_reason: {latest_recap['stop_reason']}")
        if isinstance(latest_recap.get("token_usage"), dict):
            lines.append(f"- latest_token_usage: {format_token_usage(latest_recap['token_usage'])}")
        if latest_recap.get("recommended_next_step"):
            lines.append(f"- latest_recommended_next_step: {latest_recap['recommended_next_step']}")
    return "\n".join(lines)


def _bounded_round_history(rounds, max_round_history, max_text_chars, previous_memory=None):
    previous_rounds = {}
    if isinstance(previous_memory, dict):
        for item in previous_memory.get("round_history") or []:
            if isinstance(item, dict) and item.get("taskpack_id"):
                previous_rounds[str(item["taskpack_id"])] = item
    bounded = []
    for item in list(rounds or [])[-max_round_history:]:
        if not isinstance(item, dict):
            continue
        record = {
            "round": item.get("round"),
            "taskpack_id": _bounded_text(item.get("taskpack_id"), max_text_chars),
            "status": _bounded_text(item.get("status"), max_text_chars),
            "run_status": _bounded_text(item.get("run_status"), max_text_chars),
            "blocked_count": int(item.get("blocked_count") or 0),
            "report_path": _bounded_text(item.get("report_path"), max_text_chars),
        }
        previous = previous_rounds.get(str(record.get("taskpack_id")))
        if previous:
            record.update(_round_history_recap_fields(previous, max_text_chars))
        bounded.append(record)
    return bounded


def _latest_round_recap(
    *,
    latest_round,
    source_report,
    summary,
    stop_reason,
    current_next_step,
    max_text_chars,
):
    if not isinstance(latest_round, dict) or not latest_round:
        return {}
    source_report = source_report if isinstance(source_report, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    evidence_paths = _round_evidence_paths(source_report, latest_round, max_text_chars)
    blockers = _unique_bounded_texts(
        [
            stop_reason if stop_reason in {
                "blocked",
                "manual_gate_required",
                "permission_request_required",
                "failed",
                "review_gate_required",
            } else None,
            *_text_items(summary.get("evidence_gaps")),
        ],
        DEFAULT_MAX_QUEUE_ITEMS,
        max_text_chars,
    )
    verification = _first_non_empty_text(summary.get("verification"))
    recap = {
        "round": latest_round.get("round"),
        "taskpack_id": _bounded_text(latest_round.get("taskpack_id") or source_report.get("run_id"), max_text_chars),
        "result_status": _bounded_text(latest_round.get("status") or source_report.get("run_status"), max_text_chars),
        "run_status": _bounded_text(source_report.get("run_status") or latest_round.get("run_status"), max_text_chars),
        "run_outcome": _bounded_text(source_report.get("run_outcome"), max_text_chars),
        "stop_reason": _bounded_text(stop_reason, max_text_chars),
        "report_path": _bounded_text(source_report.get("report_path") or latest_round.get("report_path"), max_text_chars),
        "report_json_path": _bounded_text(source_report.get("report_json_path"), max_text_chars),
        "evidence_paths": evidence_paths,
        "blockers": blockers,
        "token_usage": _token_usage_or_unavailable(source_report.get("token_usage")),
        "recommended_next_step": _bounded_text(current_next_step, max_text_chars),
    }
    if verification:
        recap["suggested_verification"] = _bounded_text(verification, max_text_chars)
    return {key: value for key, value in recap.items() if value not in (None, "", [])}


def _round_history_recap_fields(recap, max_text_chars):
    if not isinstance(recap, dict):
        return {}
    fields = {}
    for key in [
        "result_status",
        "run_outcome",
        "stop_reason",
        "recommended_next_step",
        "suggested_verification",
    ]:
        value = _bounded_text(recap.get(key), max_text_chars)
        if value:
            fields[key] = value
    evidence_paths = _evidence_path_items(recap.get("evidence_paths"), max_text_chars)
    if evidence_paths:
        fields["evidence_paths"] = evidence_paths
    blockers = _unique_bounded_texts(_text_items(recap.get("blockers")), DEFAULT_MAX_QUEUE_ITEMS, max_text_chars)
    if blockers:
        fields["blockers"] = blockers
    token_usage = _token_usage_or_unavailable(recap.get("token_usage"))
    if token_usage:
        fields["token_usage"] = token_usage
    return fields


def _blocked_reasons(*, stop_reason, summary, previous_memory, max_items, max_text_chars):
    reasons = []
    if stop_reason in {"blocked", "manual_gate_required", "permission_request_required", "failed"}:
        reasons.append(str(stop_reason))
    for gap in _text_items(summary.get("evidence_gaps")):
        reasons.append(gap)
    if isinstance(previous_memory, dict):
        reasons.extend(_text_items(previous_memory.get("blocked_reasons")))
    return _unique_bounded_texts(reasons, max_items, max_text_chars)


def _follow_up_queue(
    *,
    current_next_step,
    latest_round,
    latest_round_recap,
    latest_report_path,
    previous_memory,
    max_items,
    max_text_chars,
):
    queue = []
    if current_next_step:
        queue.append(
            {
                "objective": _bounded_text(current_next_step, max_text_chars),
                "source_taskpack_id": latest_round.get("taskpack_id"),
                "source_report_path": latest_report_path,
                **_queue_recap_metadata(latest_round_recap, max_text_chars),
            }
        )
    if isinstance(previous_memory, dict):
        for item in previous_memory.get("follow_up_queue", []):
            if isinstance(item, dict) and item.get("objective"):
                queue.append(
                    {
                        "objective": _bounded_text(item.get("objective"), max_text_chars),
                        "source_taskpack_id": _bounded_text(item.get("source_taskpack_id"), max_text_chars),
                        "source_report_path": _bounded_text(item.get("source_report_path"), max_text_chars),
                        **_queue_recap_metadata(item, max_text_chars),
                    }
                )
    deduped = []
    seen = set()
    for item in queue:
        key = (item.get("objective"), item.get("source_taskpack_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


def _queue_recap_metadata(recap, max_text_chars):
    if not isinstance(recap, dict):
        return {}
    metadata = {}
    text_fields = {
        "source_result_status": recap.get("source_result_status") or recap.get("result_status"),
        "source_run_outcome": recap.get("source_run_outcome") or recap.get("run_outcome"),
        "stop_reason": recap.get("stop_reason"),
        "recommended_next_step": recap.get("recommended_next_step"),
        "suggested_verification": recap.get("suggested_verification"),
    }
    for key, value in text_fields.items():
        text = _bounded_text(value, max_text_chars)
        if text:
            metadata[key] = text
    evidence_paths = _evidence_path_items(
        recap.get("source_evidence_paths") or recap.get("evidence_paths"),
        max_text_chars,
    )
    if evidence_paths:
        metadata["source_evidence_paths"] = evidence_paths
    blockers = _unique_bounded_texts(_text_items(recap.get("blockers")), DEFAULT_MAX_QUEUE_ITEMS, max_text_chars)
    if blockers:
        metadata["blockers"] = blockers
    token_usage = _token_usage_or_unavailable(recap.get("token_usage"))
    if token_usage:
        metadata["token_usage"] = token_usage
    return metadata


def _evidence_sources(rounds, latest_report_path):
    paths = []
    for item in rounds:
        report_path = item.get("report_path")
        if report_path:
            paths.append(report_path)
    if latest_report_path:
        paths.append(latest_report_path)
    result = []
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append({"type": "report", "path": path})
    return result


def _round_evidence_paths(source_report, latest_round, max_text_chars):
    paths = []
    report_path = source_report.get("report_path") or latest_round.get("report_path")
    report_json_path = source_report.get("report_json_path")
    run_dir = source_report.get("run_dir")
    if report_path:
        paths.append({"type": "report", "path": report_path})
    if report_json_path:
        paths.append({"type": "report_json", "path": report_json_path})
    if run_dir:
        paths.append({"type": "run_dir", "path": run_dir})
    return _evidence_path_items(paths, max_text_chars)


def _evidence_path_items(values, max_text_chars):
    result = []
    seen = set()
    for item in values or []:
        if isinstance(item, dict):
            path = item.get("path")
            kind = item.get("type") or "evidence"
        else:
            path = item
            kind = "evidence"
        path = _bounded_text(path, max_text_chars)
        kind = _bounded_text(kind, max_text_chars)
        if not path or path in seen:
            continue
        seen.add(path)
        result.append({"type": kind or "evidence", "path": path})
        if len(result) >= DEFAULT_MAX_QUEUE_ITEMS:
            break
    return result


def _evidence_path_texts(values):
    return [item["path"] for item in _evidence_path_items(values, DEFAULT_MAX_TEXT_CHARS)]


def _token_usage_or_unavailable(value):
    if not isinstance(value, dict) or not value.get("usage_status"):
        return {
            "usage_status": "unavailable",
            "reported_attempt_count": 0,
            "unreported_attempt_count": 1,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        }
    return {key: value.get(key) for key in TOKEN_USAGE_KEYS}


def _fit_memory_json(memory, max_memory_json_chars):
    if len(json.dumps(memory, ensure_ascii=False, sort_keys=True)) <= max_memory_json_chars:
        return memory
    compact = dict(memory)
    compact["round_history"] = compact.get("round_history", [])[-1:]
    compact["follow_up_queue"] = compact.get("follow_up_queue", [])[:1]
    compact["blocked_reasons"] = compact.get("blocked_reasons", [])[:1]
    if len(json.dumps(compact, ensure_ascii=False, sort_keys=True)) <= max_memory_json_chars:
        return compact
    for key in ["original_goal", "current_hypothesis", "current_next_step"]:
        compact[key] = _bounded_text(compact.get(key), 120)
    if len(json.dumps(compact, ensure_ascii=False, sort_keys=True)) <= max_memory_json_chars:
        return compact
    compact["evidence_sources"] = compact.get("evidence_sources", [])[-1:]
    compact["blocked_reasons"] = compact.get("blocked_reasons", [])[:1]
    compact["follow_up_queue"] = compact.get("follow_up_queue", [])[:1]
    if len(json.dumps(compact, ensure_ascii=False, sort_keys=True)) <= max_memory_json_chars:
        return compact
    for key in ["current_hypothesis", "current_next_step"]:
        compact[key] = _bounded_text(compact.get(key), 80)
    return compact


def _first_non_empty_text(value):
    for item in _text_items(value):
        if item:
            return item
    return None


def _text_items(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _unique_bounded_texts(items, max_items, max_text_chars):
    result = []
    seen = set()
    for item in items:
        text = _bounded_text(item, max_text_chars)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _memory_text(memory, key):
    if not isinstance(memory, dict):
        return None
    return memory.get(key)


def _bounded_text(value, max_chars):
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _safe_artifact_id(value):
    safe = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in str(value or "pursue")
    ).strip(".-")
    return safe or "pursue"


def _utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

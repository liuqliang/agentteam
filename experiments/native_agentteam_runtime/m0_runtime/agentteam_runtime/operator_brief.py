RUN_STATUS_ZH = {
    "completed": "已完成",
    "failed": "失败",
    "running": "运行中",
    "stopped": "已停止",
    "timed_out": "已超时",
    "unknown": "状态未知",
}

INTEGRATION_STATUS_ZH = {
    "passed": "已通过",
    "blocked": "受阻",
    "not recorded": "未记录",
}


def build_chinese_operator_brief(
    run_id,
    run_status,
    task_count,
    blocked_count,
    completion_summary,
    max_items=2,
):
    summary = completion_summary if isinstance(completion_summary, dict) else {}
    lines = [
        (
            f"本次运行{_run_status_label(run_status)}，"
            f"共 {_count(task_count)} 个任务，{_count(blocked_count)} 个阻塞。"
        )
    ]
    _append_item_line(lines, "主要变更", summary.get("what_changed"), max_items=max_items)
    _append_item_line(lines, "涉及文件", summary.get("changed_files"), max_items=max_items)
    _append_item_line(lines, "验证情况", summary.get("verification"), max_items=max_items)
    integration = _integration_label(summary.get("integration"))
    if integration:
        lines.append(f"集成状态：{integration}")
    recommendation = _first_text(summary.get("integration_recommendation"))
    if recommendation:
        lines.append(f"合并建议：{recommendation}")
    _append_item_line(lines, "下一步", summary.get("next_steps"), max_items=1)
    _append_item_line(lines, "证据缺口", summary.get("evidence_gaps"), max_items=max_items)
    return lines


def _append_item_line(lines, label, values, max_items):
    items = _text_items(values)[:max_items]
    if not items:
        return
    lines.append(f"{label}：{'；'.join(items)}")


def _run_status_label(status):
    text = _first_text(status) or "unknown"
    return RUN_STATUS_ZH.get(text, text)


def _integration_label(status):
    text = _first_text(status)
    if not text:
        return None
    return INTEGRATION_STATUS_ZH.get(text, text)


def _count(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_text(values):
    items = _text_items(values)
    return items[0] if items else None


def _text_items(values):
    if values is None:
        return []
    if isinstance(values, list):
        return [
            str(item).strip()
            for item in values
            if item is not None and str(item).strip()
        ]
    if isinstance(values, tuple):
        return [
            str(item).strip()
            for item in values
            if item is not None and str(item).strip()
        ]
    text = str(values).strip()
    return [text] if text else []

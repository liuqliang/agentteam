import hashlib
from datetime import UTC, datetime
from pathlib import Path


def build_planner_context(
    agent_pool,
    state,
    milestone_id,
    default_worker_role,
    allowed_read_scopes=None,
    allowed_write_scopes=None,
    context_artifact_paths=None,
    context_artifact_excerpt_chars=1200,
):
    allowed_read_scopes = list(allowed_read_scopes or ["."])
    allowed_write_scopes = list(allowed_write_scopes or ["generated/"])
    backlog_items = state.get("backlog", {}).get("items", [])
    context = {
        "context_schema_version": "planner_context.v1",
        "milestone_id": milestone_id,
        "default_worker_role": default_worker_role,
        "allowed_read_scopes": allowed_read_scopes,
        "allowed_write_scopes": allowed_write_scopes,
        "available_agent_roles": _available_roles(agent_pool),
        "backlog_summary": _backlog_summary(backlog_items),
        "completed_task_ids": _completed_task_ids(state.get("steps", [])),
        "inflight_task_ids": [
            attempt["task_id"]
            for attempt in state.get("inflight_attempts", [])
        ],
        "proposal_contract": {
            "schema_version": "task_proposal.v1",
            "required_fields": [
                "task_id",
                "objective",
                "read_scope",
                "write_scope",
                "required_role",
                "risk_target",
            ],
            "forbidden_task_kind": "decompose_backlog",
            "allowed_backlog_statuses": ["ready", "blocked"],
        },
        "runtime_capabilities": [
            "two_phase_dispatch_collect",
            "retry_timeout_recovery",
            "worker_health_restart",
            "planner_proposal_decomposition",
        ],
    }
    if context_artifact_paths:
        context["artifact_context"] = build_artifact_context(
            context_artifact_paths,
            excerpt_chars=context_artifact_excerpt_chars,
        )
    return context


def build_artifact_context(artifact_paths, excerpt_chars=1200):
    if not isinstance(excerpt_chars, int) or excerpt_chars < 1:
        raise ValueError("context artifact excerpt chars must be an integer >= 1")
    sources = []
    warnings = []
    for raw_path in artifact_paths:
        path = Path(raw_path)
        if not path.exists():
            warnings.append({"path": str(path), "warning": "missing"})
            continue
        if not path.is_file():
            warnings.append({"path": str(path), "warning": "not_file"})
            continue
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append({"path": str(path), "warning": "decode_error"})
            continue
        sources.append(_artifact_source_summary(path, data, text, excerpt_chars))
    return {
        "schema_version": "artifact_context.v1",
        "excerpt_budget_chars": excerpt_chars,
        "sources": sources,
        "warnings": warnings,
    }


def _artifact_source_summary(path, data, text, excerpt_chars):
    normalized_text = _normalize_excerpt_text(text)
    excerpt = normalized_text[:excerpt_chars]
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "modified_at": _utc_timestamp(stat.st_mtime),
        "heading_count": len(_markdown_headings(text)),
        "headings": _markdown_headings(text)[:12],
        "excerpt": excerpt,
        "excerpt_chars": len(excerpt),
        "omitted_chars": max(len(normalized_text) - len(excerpt), 0),
    }


def _normalize_excerpt_text(text):
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line.strip())


def _markdown_headings(text):
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        marker, _, title = stripped.partition(" ")
        if marker and set(marker) == {"#"} and title.strip():
            headings.append(title.strip())
    return headings


def _utc_timestamp(epoch_seconds):
    return (
        datetime.fromtimestamp(epoch_seconds, UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _available_roles(agent_pool):
    roles = {
        agent.get("role")
        for agent in agent_pool.get("agents", [])
        if agent.get("role")
    }
    return sorted(roles)


def _backlog_summary(backlog_items):
    summary = {
        "total": len(backlog_items),
        "ready": 0,
        "blocked": 0,
        "done": 0,
        "other": 0,
    }
    for item in backlog_items:
        status = item.get("backlog_status")
        if status in {"ready", "blocked", "done"}:
            summary[status] += 1
        else:
            summary["other"] += 1
    return summary


def _completed_task_ids(steps):
    return [
        step["task_id"]
        for step in steps
        if step.get("step_status") == "processed"
    ]

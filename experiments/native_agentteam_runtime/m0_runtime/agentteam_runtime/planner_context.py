def build_planner_context(
    agent_pool,
    state,
    milestone_id,
    default_worker_role,
    allowed_read_scopes=None,
    allowed_write_scopes=None,
):
    allowed_read_scopes = list(allowed_read_scopes or ["."])
    allowed_write_scopes = list(allowed_write_scopes or ["generated/"])
    backlog_items = state.get("backlog", {}).get("items", [])
    return {
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

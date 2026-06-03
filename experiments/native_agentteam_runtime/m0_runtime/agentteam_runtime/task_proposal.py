_VALID_BACKLOG_STATUSES = {"ready", "blocked"}


def normalize_task_proposal(proposal, existing_task_ids=None):
    existing_task_ids = set(existing_task_ids or set())
    if not isinstance(proposal, dict):
        raise ValueError("task proposal must be an object")
    milestone_id = _required_string(proposal, "milestone_id")
    raw_tasks = proposal.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("tasks must be a non-empty list")

    generated_task_ids = []
    for raw_task in raw_tasks:
        task_id = _required_string(raw_task, "task_id")
        if task_id in existing_task_ids or task_id in generated_task_ids:
            raise ValueError(f"duplicate task_id: {task_id}")
        generated_task_ids.append(task_id)

    allowed_dependency_ids = existing_task_ids | set(generated_task_ids)
    tasks = [
        _normalize_task(
            raw_task,
            default_milestone_id=milestone_id,
            allowed_dependency_ids=allowed_dependency_ids,
        )
        for raw_task in raw_tasks
    ]
    return {
        "proposal_status": "accepted",
        "milestone_id": milestone_id,
        "generated_task_ids": generated_task_ids,
        "tasks": tasks,
    }


def _normalize_task(raw_task, default_milestone_id, allowed_dependency_ids):
    if not isinstance(raw_task, dict):
        raise ValueError("task must be an object")
    if raw_task.get("task_kind") == "decompose_backlog":
        raise ValueError("generated task may not be a decomposition task")

    task_id = _required_string(raw_task, "task_id")
    objective = _required_string(raw_task, "objective")
    required_role = _required_string(raw_task, "required_role")
    risk_target = _required_string(raw_task, "risk_target")
    milestone_id = _optional_string(raw_task, "milestone_id", default_milestone_id)
    backlog_status = _optional_string(raw_task, "backlog_status", "ready")
    if backlog_status not in _VALID_BACKLOG_STATUSES:
        raise ValueError(f"unsupported backlog_status: {backlog_status}")

    depends_on = _string_list(raw_task, "depends_on")
    unknown_dependencies = [
        dependency_id
        for dependency_id in depends_on
        if dependency_id not in allowed_dependency_ids
    ]
    if unknown_dependencies:
        raise ValueError(f"unknown dependency: {unknown_dependencies[0]}")

    return {
        "task_id": task_id,
        "milestone_id": milestone_id,
        "objective": objective,
        "backlog_status": backlog_status,
        "risk_target": risk_target,
        "depends_on": depends_on,
        "read_scope": _string_list(raw_task, "read_scope"),
        "write_scope": _string_list(raw_task, "write_scope"),
        "required_role": required_role,
        "blockers": _string_list(raw_task, "blockers"),
    }


def _required_string(source, key):
    if not isinstance(source, dict):
        raise ValueError(f"{key} must be a string")
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_string(source, key, default):
    value = source.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _string_list(source, key):
    value = source.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return list(value)

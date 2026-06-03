_VALID_BACKLOG_STATUSES = {"ready", "blocked"}
_VALID_RISK_TARGETS = {"L0", "L1", "L2"}


def normalize_task_proposal(
    proposal,
    existing_task_ids=None,
    allowed_roles=None,
    allowed_write_scopes=None,
):
    existing_task_ids = set(existing_task_ids or set())
    allowed_roles = set(allowed_roles) if allowed_roles is not None else None
    allowed_write_scopes = (
        [_scope_prefix(scope) for scope in allowed_write_scopes]
        if allowed_write_scopes is not None
        else None
    )
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
            allowed_roles=allowed_roles,
            allowed_write_scopes=allowed_write_scopes,
        )
        for raw_task in raw_tasks
    ]
    _validate_generated_dependency_cycles(tasks)
    return {
        "proposal_status": "accepted",
        "milestone_id": milestone_id,
        "generated_task_ids": generated_task_ids,
        "tasks": tasks,
    }


def _normalize_task(
    raw_task,
    default_milestone_id,
    allowed_dependency_ids,
    allowed_roles,
    allowed_write_scopes,
):
    if not isinstance(raw_task, dict):
        raise ValueError("task must be an object")
    if raw_task.get("task_kind") == "decompose_backlog":
        raise ValueError("generated task may not be a decomposition task")

    task_id = _required_string(raw_task, "task_id")
    objective = _required_string(raw_task, "objective")
    required_role = _required_string(raw_task, "required_role")
    if allowed_roles is not None and required_role not in allowed_roles:
        raise ValueError(f"unknown required_role: {required_role}")
    risk_target = _required_string(raw_task, "risk_target")
    _validate_risk_target(risk_target)
    milestone_id = _optional_string(raw_task, "milestone_id", default_milestone_id)
    backlog_status = _optional_string(raw_task, "backlog_status", "ready")
    if backlog_status not in _VALID_BACKLOG_STATUSES:
        raise ValueError(f"unsupported backlog_status: {backlog_status}")

    depends_on = _string_list(raw_task, "depends_on")
    if task_id in depends_on:
        raise ValueError(f"self dependency: {task_id}")
    unknown_dependencies = [
        dependency_id
        for dependency_id in depends_on
        if dependency_id not in allowed_dependency_ids
    ]
    if unknown_dependencies:
        raise ValueError(f"unknown dependency: {unknown_dependencies[0]}")

    write_scope = _string_list(raw_task, "write_scope")
    _validate_write_scope(write_scope, allowed_write_scopes)
    _validate_risk_scope_size(risk_target, write_scope)
    blockers = _string_list(raw_task, "blockers")
    backlog_status, blockers = _apply_risk_policy(
        risk_target,
        backlog_status,
        blockers,
    )

    return {
        "task_id": task_id,
        "milestone_id": milestone_id,
        "objective": objective,
        "backlog_status": backlog_status,
        "risk_target": risk_target,
        "depends_on": depends_on,
        "read_scope": _string_list(raw_task, "read_scope"),
        "write_scope": write_scope,
        "required_role": required_role,
        "blockers": blockers,
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


def _validate_write_scope(write_scope, allowed_write_scopes):
    if allowed_write_scopes is None:
        return
    for scope in write_scope:
        scope_prefix = _scope_prefix(scope)
        if not any(scope_prefix.startswith(allowed) for allowed in allowed_write_scopes):
            raise ValueError(f"write_scope outside allowed scope: {scope}")


def _validate_risk_target(risk_target):
    if risk_target not in _VALID_RISK_TARGETS:
        raise ValueError(f"unsupported risk_target: {risk_target}")


def _validate_risk_scope_size(risk_target, write_scope):
    if risk_target == "L0":
        if len(write_scope) > 1:
            raise ValueError("L0 task may not declare multiple write scopes")
        if "." in write_scope:
            raise ValueError("L0 task may not use repository-wide write_scope")
    if risk_target == "L1" and len(write_scope) > 3:
        raise ValueError("L1 task may not declare more than three write scopes")


def _apply_risk_policy(risk_target, backlog_status, blockers):
    if risk_target != "L2":
        return backlog_status, blockers
    normalized_blockers = list(blockers)
    if "requires_review" not in normalized_blockers:
        normalized_blockers.append("requires_review")
    return "blocked", normalized_blockers


def _validate_generated_dependency_cycles(tasks):
    generated_ids = {task["task_id"] for task in tasks}
    graph = {
        task["task_id"]: [
            dependency_id
            for dependency_id in task["depends_on"]
            if dependency_id in generated_ids
        ]
        for task in tasks
    }
    visiting = set()
    visited = set()

    def visit(task_id):
        if task_id in visiting:
            raise ValueError(f"dependency cycle involving task_id: {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency_id in graph[task_id]:
            visit(dependency_id)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in sorted(graph):
        visit(task_id)


def _scope_prefix(scope):
    if scope == ".":
        return "."
    return scope.rstrip("/") + "/"

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


TASKPACK_SCHEMA_VERSION = "taskpack.v1"
TASKPACK_SEMANTIC_CONTRACT_VERSION = "task_semantics.v1"
DEFAULT_WORKER_ROLE = "implementation_worker"
DEFAULT_DAEMON_MAX_STEPS = 45000
DEFAULT_CODEX_RUNTIME_TIMEOUT_SECONDS = 3600
DEFAULT_LEASE_TIMEOUT_GRACE_SECONDS = 60
TASKPACK_TRANSLATABLE_RUNTIME_BACKENDS = {"fake", "codex"}
TASKPACK_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")


class TaskpackValidationError(ValueError):
    pass


def draft_taskpack_files(
    project_root,
    goal,
    draft_root,
    taskpack_id=None,
    read_scope=None,
    write_scope=None,
    verification_command=None,
    allow_merge=False,
    codex_timeout_seconds=1800,
):
    project_root = Path(project_root).resolve()
    draft_root = Path(draft_root).resolve()
    taskpack_id = _resolve_draft_taskpack_id(taskpack_id, goal, draft_root)
    taskpack_dir = (draft_root / taskpack_id).resolve()
    _require_contained_path(taskpack_dir, draft_root, "taskpack_dir")

    read_scope = _string_list(read_scope, ["."], "read_scope")
    write_scope = _string_list(write_scope, [".agentteam/generated/"], "write_scope")
    verification_command = _string_list(
        verification_command,
        ["python3", "-m", "unittest", "discover"],
        "verification_command",
    )

    taskpack_dir.mkdir(parents=True, exist_ok=False)

    task_id = f"TASK-{taskpack_id.upper().replace('-', '_')}-001"
    taskpack = {
        "taskpack_schema_version": TASKPACK_SCHEMA_VERSION,
        "taskpack_id": taskpack_id,
        "status": "draft",
        "semantic_contract_version": TASKPACK_SEMANTIC_CONTRACT_VERSION,
        "project_root": str(project_root),
        "goal": goal,
        "original_goal": goal,
        "runtime": {
            "default_backend": "codex",
            "codex": {
                "sandbox": "workspace-write",
                "timeout_seconds": codex_timeout_seconds,
            },
        },
        "policy": {
            "allow_merge": bool(allow_merge),
            "merge_requires_verified_integration": True,
        },
        "files": {
            "agent_pool": "agent_pool.json",
            "backlog": "backlog.json",
            "verification": "verification.json",
        },
    }
    agent_pool = {
        "scheduler_agent_id": "agent-scheduler",
        "role_runtime_profiles": {
            DEFAULT_WORKER_ROLE: {
                "adapter": "codex",
                "sandbox": "workspace-write",
                "timeout_seconds": codex_timeout_seconds,
            }
        },
        "agents": [
            {
                "agent_id": "agent-implementation-worker-1",
                "role": DEFAULT_WORKER_ROLE,
                "status": "idle",
                "inbox_path": "mailboxes/agent-implementation-worker-1/inbox.jsonl",
                "outbox_path": "mailboxes/agent-implementation-worker-1/outbox.jsonl",
            }
        ],
    }
    backlog = {
        "backlog_id": f"BL-{taskpack_id}",
        "items": [
            {
                "task_id": task_id,
                "milestone_id": "TASKPACK-M0",
                "objective": goal,
                "goal_alignment": _default_goal_alignment(goal),
                "required_deliverables": _default_required_deliverables(goal),
                "backlog_status": "ready",
                "risk_target": "L1",
                "depends_on": [],
                "read_scope": read_scope,
                "write_scope": write_scope,
                "required_role": DEFAULT_WORKER_ROLE,
                "blockers": [],
            }
        ],
    }
    verification = {
        "verification_schema_version": "taskpack_verification.v1",
        "command": verification_command,
        "success_criteria": [
            "verification command exits with code 0",
            "runtime validation accepts changed files inside declared write_scope",
        ],
    }

    _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    _write_json(taskpack_dir / "agent_pool.json", agent_pool)
    _write_json(taskpack_dir / "backlog.json", backlog)
    _write_json(taskpack_dir / "verification.json", verification)
    (taskpack_dir / "README.md").write_text(_render_readme(taskpack, backlog, verification), encoding="utf-8")
    return {"taskpack_dir": str(taskpack_dir), "taskpack_id": taskpack_id}


def load_taskpack(taskpack_dir):
    taskpack_dir = Path(taskpack_dir).resolve()
    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    if not isinstance(taskpack, dict):
        raise TaskpackValidationError("taskpack must be an object")
    files = taskpack.get("files", {})
    if not isinstance(files, dict):
        raise TaskpackValidationError("taskpack files must be an object")
    return {
        "taskpack_dir": str(taskpack_dir),
        "taskpack": taskpack,
        "agent_pool": _read_json(
            _resolve_companion_artifact_path(
                taskpack_dir, files.get("agent_pool", "agent_pool.json"), "files.agent_pool"
            )
        ),
        "backlog": _read_json(
            _resolve_companion_artifact_path(taskpack_dir, files.get("backlog", "backlog.json"), "files.backlog")
        ),
        "verification": _read_json(
            _resolve_companion_artifact_path(
                taskpack_dir, files.get("verification", "verification.json"), "files.verification"
            )
        ),
    }


def validate_taskpack(taskpack_dir):
    try:
        loaded = load_taskpack(taskpack_dir)
    except FileNotFoundError as exc:
        missing_path = Path(exc.filename).name if exc.filename else "taskpack artifact"
        raise TaskpackValidationError(f"missing taskpack artifact: {missing_path}") from exc

    errors = []
    taskpack = loaded["taskpack"]
    agent_pool = loaded["agent_pool"]
    backlog = loaded["backlog"]
    verification = loaded["verification"]
    project_root_value = taskpack.get("project_root")
    taskpack_id = None

    if taskpack.get("taskpack_schema_version") != TASKPACK_SCHEMA_VERSION:
        errors.append("taskpack_schema_version must be taskpack.v1")
    try:
        taskpack_id = _validate_existing_taskpack_id(taskpack.get("taskpack_id"))
    except TaskpackValidationError as exc:
        errors.append(str(exc))
    if not isinstance(project_root_value, str) or not project_root_value:
        errors.append("project_root must be a non-empty string")
    else:
        project_root = Path(project_root_value)
        if not project_root.exists():
            errors.append("project_root does not exist")
        elif not project_root.is_dir():
            errors.append("project_root must be a directory or git repository")
        elif not _is_git_repo(project_root):
            errors.append("project_root must be a git repository")
    if taskpack.get("status") not in {"draft", "frozen"}:
        errors.append("status must be draft or frozen")
    if not _is_non_empty_string(taskpack.get("goal")):
        errors.append("goal must be a non-empty string")
    semantic_contract = taskpack.get("semantic_contract_version")
    if semantic_contract is not None and semantic_contract != TASKPACK_SEMANTIC_CONTRACT_VERSION:
        errors.append("semantic_contract_version must be task_semantics.v1")
    semantic_contract_enabled = semantic_contract == TASKPACK_SEMANTIC_CONTRACT_VERSION
    if semantic_contract_enabled and not _is_non_empty_string(taskpack.get("original_goal")):
        errors.append("original_goal must be a non-empty string")
    try:
        _validate_taskpack_runtime_backend(taskpack.get("runtime"))
    except TaskpackValidationError as exc:
        errors.append(str(exc))

    idle_agent_roles = _validate_agent_pool(agent_pool, errors)

    if not isinstance(backlog, dict):
        errors.append("backlog must be an object")
        items = []
    else:
        items = backlog.get("items", [])
        if not isinstance(items, list):
            errors.append("backlog.items must be a list")
            items = []
        elif not items:
            errors.append("backlog must contain at least one task")
    seen_task_ids = set()
    dependency_graph = {}
    for item in items:
        if not isinstance(item, dict):
            errors.append("backlog.items entries must be objects")
            continue
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            errors.append("task_id must be a non-empty string")
            task_id_label = "<unknown>"
        else:
            task_id_label = task_id
            if task_id in seen_task_ids:
                errors.append(f"duplicate task_id: {task_id}")
            seen_task_ids.add(task_id)
            dependency_graph.setdefault(task_id, [])
        required_role = item.get("required_role")
        if not _is_non_empty_string(item.get("objective")):
            errors.append(f"{task_id_label} objective must be a non-empty string")
        if semantic_contract_enabled and not _is_non_empty_string(item.get("goal_alignment")):
            errors.append(f"{task_id_label} goal_alignment must be a non-empty string")
        deliverables = item.get("required_deliverables")
        if semantic_contract_enabled and (not isinstance(deliverables, list) or not deliverables):
            errors.append(f"{task_id_label} required_deliverables must be a non-empty list")
        elif semantic_contract_enabled and not all(_is_non_empty_string(deliverable) for deliverable in deliverables):
            errors.append(f"{task_id_label} required_deliverables entries must be non-empty strings")
        if not _is_non_empty_string(required_role):
            errors.append(f"{task_id_label} required_role must be a non-empty string")
        elif required_role not in idle_agent_roles:
            errors.append(f"{task_id_label} required_role has no idle agent: {required_role}")
        read_scope = item.get("read_scope")
        if not isinstance(read_scope, list) or not read_scope:
            errors.append(f"{task_id_label} read_scope must be a non-empty list")
        elif not all(isinstance(scope, str) for scope in read_scope):
            errors.append(f"{task_id_label} read_scope entries must be strings")
        if not _is_non_empty_string(item.get("backlog_status")):
            errors.append(f"{task_id_label} backlog_status must be a non-empty string")
        blockers = item.get("blockers")
        if not isinstance(blockers, list):
            errors.append(f"{task_id_label} blockers must be a list")
        elif not all(isinstance(blocker, str) for blocker in blockers):
            errors.append(f"{task_id_label} blockers entries must be strings")
        write_scope = item.get("write_scope", [])
        if not isinstance(write_scope, list) or not write_scope:
            errors.append(f"{task_id_label} write_scope must be a non-empty list")
        else:
            for scope in write_scope:
                if not isinstance(scope, str):
                    errors.append(f"{task_id_label} write_scope entries must be strings")
                    continue
                if scope in {"", "*", "**", "/"}:
                    errors.append("write_scope must not include repository root")
                    continue
                scope_path = Path(scope)
                if scope_path.is_absolute():
                    errors.append(f"{task_id_label} write_scope must be repository-relative: {scope}")
                elif _write_scope_is_repository_root(scope_path):
                    errors.append("write_scope must not include repository root")
                elif _write_scope_is_root_wide_glob(scope_path):
                    errors.append(f"{task_id_label} write_scope must not include repository-wide glob: {scope}")
                elif _write_scope_has_root_prefix_wildcard(scope_path):
                    errors.append(f"{task_id_label} write_scope must not use root-prefix wildcard: {scope}")
                elif _write_scope_escapes_repository(scope_path):
                    errors.append(f"{task_id_label} write_scope must stay inside repository: {scope}")
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list):
            errors.append(f"{task_id_label} depends_on must be a list")
        else:
            for dependency in depends_on:
                if not isinstance(dependency, str):
                    errors.append(f"{task_id_label} depends_on entries must be strings")
                    continue
                if dependency == task_id:
                    errors.append(f"{task_id_label} must not depend on itself")
                if task_id in dependency_graph:
                    dependency_graph[task_id].append(dependency)

    _validate_dependency_graph(dependency_graph, seen_task_ids, errors)

    if not isinstance(verification, dict):
        errors.append("verification must be an object")
        command = None
    else:
        command = verification.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        errors.append("verification.command must be a non-empty string array")
    elif not _verification_command_allowed(command[0], taskpack.get("project_root")):
        errors.append(f"verification command is not allowed: {command[0]}")

    if errors:
        raise TaskpackValidationError("; ".join(errors))
    return {"status": "accepted", "taskpack_id": taskpack_id, "errors": []}


def _verification_command_allowed(executable, project_root):
    if executable in {"python3", "python", "/bin/bash", "bash", "make"}:
        return True
    if not project_root:
        return False
    project_root_path = Path(os.path.abspath(project_root))
    executable_path = Path(executable)
    if not executable_path.is_absolute():
        executable_path = project_root_path / executable_path
    try:
        relative = Path(os.path.abspath(executable_path)).relative_to(project_root_path)
    except ValueError:
        return False
    return relative.as_posix() in {".venv/bin/python", "venv/bin/python"} and executable_path.is_file()


def _default_goal_alignment(goal):
    return (
        "This task must preserve the original goal and either implement an "
        "evidence-backed repository change or explain why no safe in-repo "
        f"change is justified for: {goal}"
    )


def _default_required_deliverables(goal):
    normalized = str(goal or "").lower()
    optimization_markers = [
        "optimize",
        "optimization",
        "improve",
        "audit",
        "性能",
        "优化",
        "改进",
        "提升",
        "检查",
    ]
    if any(marker in normalized for marker in optimization_markers):
        return [
            "repository_understanding_summary",
            "optimization_candidate_matrix",
            "evidence_paths",
            "implemented_changes_or_no_safe_change_rationale",
            "verification_summary",
            "recommended_next_implementation_tasks",
        ]
    return [
        "goal_alignment_summary",
        "implemented_changes_or_no_safe_change_rationale",
        "verification_summary",
        "next_steps",
    ]


def freeze_taskpack(taskpack_dir, frozen_root):
    taskpack_dir = Path(taskpack_dir).resolve()
    validation = validate_taskpack(taskpack_dir)
    taskpack_id = validation["taskpack_id"]
    frozen_root = Path(frozen_root).resolve()
    frozen_dir = (frozen_root / taskpack_id).resolve()
    _require_contained_path(frozen_dir, frozen_root, "frozen_taskpack_dir")
    if frozen_dir.exists():
        raise TaskpackValidationError(f"frozen taskpack already exists: {frozen_dir}")
    inventory = _build_taskpack_artifact_inventory(taskpack_dir)
    _validate_taskpack_artifact_inventory(taskpack_dir, inventory)

    for relative_path, source_path in inventory:
        destination_path = frozen_dir / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)

    frozen_taskpack = _read_json(frozen_dir / "taskpack.yaml")
    frozen_taskpack["status"] = "frozen"
    _write_json(frozen_dir / "taskpack.yaml", frozen_taskpack)

    digest = _digest_taskpack_files(frozen_dir, [relative_path for relative_path, _source_path in inventory])
    manifest = {
        "manifest_schema_version": "taskpack_manifest.v1",
        "taskpack_id": taskpack_id,
        "status": "frozen",
        "digest_sha256": digest,
        "source_taskpack_dir": str(Path(taskpack_dir).resolve()),
        "validation": validation,
    }
    _write_json(frozen_dir / "manifest.json", manifest)
    return {"frozen_taskpack_dir": str(frozen_dir), "manifest": manifest}


def build_taskpack_runtime_args(
    frozen_taskpack_dir,
    run_root,
    daemon=True,
    max_inflight=2,
    max_attempts=1,
    max_steps=DEFAULT_DAEMON_MAX_STEPS,
    commit_verified_integration=False,
):
    taskpack_dir = Path(frozen_taskpack_dir).resolve()
    loaded = load_taskpack(taskpack_dir)
    taskpack = loaded["taskpack"]
    if taskpack.get("status") != "frozen":
        raise TaskpackValidationError("taskpack must be frozen before runtime launch")
    validate_taskpack(taskpack_dir)

    taskpack_id = _validate_existing_taskpack_id(taskpack.get("taskpack_id"))
    files = taskpack.get("files", {})
    if not isinstance(files, dict):
        raise TaskpackValidationError("taskpack files must be an object")
    agent_pool_path = _resolve_companion_artifact_path(
        taskpack_dir,
        files.get("agent_pool", "agent_pool.json"),
        "files.agent_pool",
    )
    backlog_path = _resolve_companion_artifact_path(
        taskpack_dir,
        files.get("backlog", "backlog.json"),
        "files.backlog",
    )
    runtime_backend = _validate_taskpack_runtime_backend(taskpack.get("runtime"))
    codex_timeout_seconds = (
        _taskpack_codex_timeout_seconds(taskpack)
        if runtime_backend == "codex"
        else None
    )
    project_root = taskpack.get("project_root")
    if not isinstance(project_root, str) or not project_root:
        raise TaskpackValidationError("project_root must be a non-empty string")
    verification_command = _validate_taskpack_verification_command(loaded.get("verification"))
    command_json = json.dumps(verification_command)

    run_root = Path(run_root).resolve()
    run_dir = (run_root / taskpack_id).resolve()
    _require_contained_path(run_dir, run_root, "run_dir")
    run_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--agent-pool",
        str(agent_pool_path),
        "--backlog",
        str(backlog_path),
        "--output-dir",
        str(run_dir),
        "--project-root",
        project_root,
    ]
    if daemon:
        args.extend(["--daemon-run-until-idle", "--daemon-two-phase-worker-pool"])
        args.extend(["--max-inflight", str(max_inflight), "--max-attempts", str(max_attempts)])
        args.extend(["--max-steps", str(max_steps)])
        if codex_timeout_seconds is not None:
            args.extend(
                [
                    "--lease-timeout-seconds",
                    str(codex_timeout_seconds + DEFAULT_LEASE_TIMEOUT_GRACE_SECONDS),
                ]
            )
    else:
        args.append("--run-until-idle")
    args.extend(["--runtime", runtime_backend])
    if codex_timeout_seconds is not None:
        args.extend(["--codex-timeout-seconds", str(codex_timeout_seconds)])
    args.append("--integrate-accepted-patch")
    args.extend(["--integration-verification-command-json", command_json])
    if commit_verified_integration:
        args.append("--commit-verified-integration")
    return args


def _taskpack_codex_timeout_seconds(taskpack):
    runtime = taskpack.get("runtime")
    codex = runtime.get("codex") if isinstance(runtime, dict) else None
    if not isinstance(codex, dict) or "timeout_seconds" not in codex:
        return DEFAULT_CODEX_RUNTIME_TIMEOUT_SECONDS
    timeout_seconds = codex.get("timeout_seconds")
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise TaskpackValidationError("runtime.codex.timeout_seconds must be an integer >= 1")
    return timeout_seconds


def _normalize_taskpack_id(taskpack_id, goal):
    if taskpack_id is None:
        taskpack_id = _slugify(goal)
    elif not isinstance(taskpack_id, str):
        raise TaskpackValidationError("taskpack_id must be a string")

    if not TASKPACK_ID_PATTERN.fullmatch(taskpack_id):
        raise TaskpackValidationError(
            "taskpack_id must be a safe lowercase slug containing only letters, numbers, and hyphens"
        )
    return taskpack_id


def _resolve_draft_taskpack_id(taskpack_id, goal, draft_root, extra_reserved_path_templates=None):
    draft_root = Path(draft_root)
    explicit = taskpack_id is not None
    base_id = _normalize_taskpack_id(taskpack_id, goal)
    reserved_path_templates = list(extra_reserved_path_templates or [])
    if explicit:
        _raise_if_draft_id_reserved(base_id, draft_root, reserved_path_templates)
        return base_id

    for candidate in _candidate_taskpack_ids(base_id):
        if not _draft_id_reserved(candidate, draft_root, reserved_path_templates):
            return candidate
    raise TaskpackValidationError(f"could not find an available taskpack id for base: {base_id}")


def _candidate_taskpack_ids(base_id):
    yield base_id
    for index in range(2, 1000):
        suffix = f"-{index}"
        head = base_id[: 80 - len(suffix)].rstrip("-") or "taskpack"
        yield f"{head}{suffix}"


def _raise_if_draft_id_reserved(taskpack_id, draft_root, reserved_path_templates):
    if _draft_id_reserved(taskpack_id, draft_root, reserved_path_templates):
        raise TaskpackValidationError(f"taskpack draft already exists: {taskpack_id}")


def _draft_id_reserved(taskpack_id, draft_root, reserved_path_templates):
    if (draft_root / taskpack_id).exists():
        return True
    for template in reserved_path_templates:
        if (draft_root / template.format(taskpack_id=taskpack_id)).exists():
            return True
    return False


def _validate_existing_taskpack_id(taskpack_id):
    if not isinstance(taskpack_id, str) or not taskpack_id:
        raise TaskpackValidationError("taskpack_id must be a non-empty string")
    return _normalize_taskpack_id(taskpack_id, "")


def _validate_taskpack_runtime_backend(runtime):
    if not isinstance(runtime, dict):
        raise TaskpackValidationError("runtime must be an object")

    backend = runtime.get("default_backend")
    if backend not in TASKPACK_TRANSLATABLE_RUNTIME_BACKENDS:
        raise TaskpackValidationError("runtime.default_backend must be fake or codex")
    return backend


def _validate_taskpack_verification_command(verification):
    if not isinstance(verification, dict):
        raise TaskpackValidationError("verification must be an object")

    command = verification.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise TaskpackValidationError("verification.command must be a non-empty string array")
    return command


def _is_non_empty_string(value):
    return isinstance(value, str) and bool(value)


def _string_list(value, default, field_name):
    if value is None:
        return list(default)
    if isinstance(value, str):
        raise TaskpackValidationError(f"{field_name} must be a list or tuple of strings, not a bare string")
    if not isinstance(value, (list, tuple)):
        raise TaskpackValidationError(f"{field_name} must be a list or tuple of strings")

    items = list(value)
    for item in items:
        if not isinstance(item, str):
            raise TaskpackValidationError(f"{field_name} must contain only strings")
    return items


def _resolve_companion_artifact_path(taskpack_dir, value, field_name):
    if not isinstance(value, str) or not value:
        raise TaskpackValidationError(f"{field_name} must be a relative path string")

    path = Path(value)
    if path.is_absolute():
        raise TaskpackValidationError(f"{field_name} must be relative to the taskpack directory")

    resolved = (taskpack_dir / path).resolve()
    _require_contained_path(resolved, taskpack_dir, field_name)
    return resolved


def _require_contained_path(path, root, field_name):
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TaskpackValidationError(f"{field_name} must stay inside {root}") from exc


def _validate_agent_pool(agent_pool, errors):
    idle_agent_roles = set()
    if not isinstance(agent_pool, dict):
        errors.append("agent_pool must be an object")
        return idle_agent_roles

    if not _is_non_empty_string(agent_pool.get("scheduler_agent_id")):
        errors.append("agent_pool.scheduler_agent_id must be a non-empty string")

    agents = agent_pool.get("agents")
    if not isinstance(agents, list) or not agents:
        errors.append("agent_pool.agents must be a non-empty list")
        return idle_agent_roles

    _validate_role_runtime_profiles(agent_pool.get("role_runtime_profiles"), errors)
    _validate_optional_role_object_map(agent_pool.get("role_prompt_contracts"), "role_prompt_contracts", errors)
    _validate_optional_role_object_map(agent_pool.get("role_context_packages"), "role_context_packages", errors)

    for index, agent in enumerate(agents):
        label = f"agent_pool.agents[{index}]"
        if not isinstance(agent, dict):
            errors.append(f"{label} must be an object")
            continue
        for field_name in ["agent_id", "role", "status", "inbox_path", "outbox_path"]:
            if not _is_non_empty_string(agent.get(field_name)):
                errors.append(f"{label}.{field_name} must be a non-empty string")
        if "runtime_profile" in agent:
            _validate_taskpack_runtime_profile(agent.get("runtime_profile"), f"{label}.runtime_profile", errors)
        if agent.get("status") == "idle" and _is_non_empty_string(agent.get("role")):
            idle_agent_roles.add(agent["role"])
    return idle_agent_roles


def _validate_role_runtime_profiles(role_runtime_profiles, errors):
    if role_runtime_profiles is None:
        return
    if not isinstance(role_runtime_profiles, dict):
        errors.append("role_runtime_profiles must be an object")
        return

    for role, profile in role_runtime_profiles.items():
        label = f"role_runtime_profiles[{role}]"
        if not isinstance(profile, dict):
            errors.append(f"{label} must be an object")
            continue

        _validate_taskpack_runtime_profile(profile, label, errors)


def _validate_taskpack_runtime_profile(profile, label, errors):
    if not isinstance(profile, dict):
        errors.append(f"{label} must be an object")
        return

    adapter = profile.get("adapter")
    if adapter is not None and adapter not in TASKPACK_TRANSLATABLE_RUNTIME_BACKENDS:
        errors.append(f"{label}.adapter must be fake or codex")

    if "command" in profile:
        errors.append(f"{label}.command is not allowed in taskpacks")

    timeout_seconds = profile.get("timeout_seconds")
    if timeout_seconds is not None:
        if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            errors.append(f"{label}.timeout_seconds must be an integer >= 1")

    model = profile.get("model")
    if model is not None and not _is_non_empty_string(model):
        errors.append(f"{label}.model must be a non-empty string")

    sandbox = profile.get("sandbox")
    if sandbox is not None and not _is_non_empty_string(sandbox):
        errors.append(f"{label}.sandbox must be a non-empty string")


def _validate_optional_role_object_map(value, field_name, errors):
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{field_name} must be an object")
        return

    for role, role_value in value.items():
        if not isinstance(role_value, dict):
            errors.append(f"{field_name}[{role}] must be an object")


def _build_taskpack_artifact_inventory(taskpack_dir):
    taskpack_dir = Path(taskpack_dir).resolve()
    taskpack_path = taskpack_dir / "taskpack.yaml"
    taskpack = _read_json(taskpack_path)
    if not isinstance(taskpack, dict):
        raise TaskpackValidationError("taskpack must be an object")
    files = taskpack.get("files", {})
    if not isinstance(files, dict):
        raise TaskpackValidationError("taskpack files must be an object")

    artifacts = [
        taskpack_path,
        _resolve_companion_artifact_path(
            taskpack_dir, files.get("agent_pool", "agent_pool.json"), "files.agent_pool"
        ),
        _resolve_companion_artifact_path(taskpack_dir, files.get("backlog", "backlog.json"), "files.backlog"),
        _resolve_companion_artifact_path(
            taskpack_dir, files.get("verification", "verification.json"), "files.verification"
        ),
        taskpack_dir / "README.md",
    ]

    inventory = []
    seen_relative_paths = set()
    for source_path in artifacts:
        source_path = Path(source_path)
        try:
            unresolved_relative_path = source_path.relative_to(taskpack_dir)
        except ValueError as exc:
            raise TaskpackValidationError(f"taskpack artifact must stay inside taskpack: {source_path}") from exc
        if source_path.is_symlink():
            raise TaskpackValidationError(
                f"taskpack artifact must not be a symlink: {unresolved_relative_path.as_posix()}"
            )
        relative_path = source_path.resolve().relative_to(taskpack_dir)
        if relative_path in seen_relative_paths:
            raise TaskpackValidationError(f"duplicate taskpack artifact path: {relative_path.as_posix()}")
        seen_relative_paths.add(relative_path)
        inventory.append((relative_path, source_path))
    return inventory


def _validate_taskpack_artifact_inventory(taskpack_dir, inventory):
    taskpack_dir = Path(taskpack_dir).resolve()
    inventory_paths = {relative_path for relative_path, _source_path in inventory}

    for relative_path, source_path in inventory:
        if not source_path.exists():
            raise TaskpackValidationError(f"taskpack artifact is missing: {relative_path.as_posix()}")
        if source_path.is_symlink():
            raise TaskpackValidationError(f"taskpack artifact must not be a symlink: {relative_path.as_posix()}")
        if not source_path.is_file():
            raise TaskpackValidationError(f"taskpack artifact must be a regular file: {relative_path.as_posix()}")

    for path in taskpack_dir.rglob("*"):
        relative_path = path.relative_to(taskpack_dir)
        if path.is_symlink():
            raise TaskpackValidationError(f"taskpack directory must not contain symlinks: {relative_path.as_posix()}")
        if path.is_file() and relative_path not in inventory_paths:
            raise TaskpackValidationError(f"unexpected taskpack artifact: {relative_path.as_posix()}")


def _write_scope_is_repository_root(scope_path):
    return scope_path == Path(".") or not scope_path.parts


def _write_scope_is_root_wide_glob(scope_path):
    parts = tuple(part for part in scope_path.parts if part != ".")
    return parts in {("*",), ("**",), ("**", "*")}


def _write_scope_has_root_prefix_wildcard(scope_path):
    parts = tuple(part for part in scope_path.parts if part != ".")
    return bool(parts) and ("*" in parts[0] or "?" in parts[0])


def _write_scope_escapes_repository(scope_path):
    if any(part == ".." for part in scope_path.parts):
        return True

    repository_root = Path("/__agentteam_repository_root__").resolve()
    try:
        (repository_root / scope_path).resolve().relative_to(repository_root)
    except ValueError:
        return True
    return False


def _is_git_repo(path):
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _validate_dependency_graph(dependency_graph, task_ids, errors):
    for task_id, dependencies in dependency_graph.items():
        for dependency in dependencies:
            if dependency not in task_ids:
                errors.append(f"{task_id} depends_on references unknown task_id: {dependency}")

    cycle = _find_dependency_cycle(dependency_graph, task_ids)
    if cycle:
        errors.append(f"depends_on cycle detected: {' -> '.join(cycle)}")


def _find_dependency_cycle(dependency_graph, task_ids):
    visiting = set()
    visited = set()
    stack = []

    def visit(task_id):
        if task_id in visiting:
            return stack[stack.index(task_id) :] + [task_id]
        if task_id in visited:
            return None

        visiting.add(task_id)
        stack.append(task_id)
        for dependency in dependency_graph.get(task_id, []):
            if dependency in task_ids:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        visiting.remove(task_id)
        visited.add(task_id)
        return None

    for task_id in dependency_graph:
        cycle = visit(task_id)
        if cycle:
            return cycle
    return None


def _digest_taskpack_files(taskpack_dir, relative_paths):
    hasher = hashlib.sha256()
    for relative_path in relative_paths:
        path = Path(taskpack_dir) / relative_path
        path_key = relative_path.as_posix()
        hasher.update(path_key.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80].strip("-") or "taskpack"


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaskpackValidationError(f"invalid json in {path.name}: {exc.msg}") from exc


def _render_readme(taskpack, backlog, verification):
    task = backlog["items"][0]
    return "\n".join(
        [
            f"# {taskpack['taskpack_id']}",
            "",
            f"Goal: {taskpack['goal']}",
            "",
            f"Original goal: {taskpack.get('original_goal') or taskpack['goal']}",
            "",
            f"Project root: `{taskpack['project_root']}`",
            "",
            f"Task: `{task['task_id']}`",
            "",
            f"Goal alignment: {task.get('goal_alignment') or 'not specified'}",
            "",
            f"Required deliverables: `{json.dumps(task.get('required_deliverables', []), sort_keys=True)}`",
            "",
            f"Read scope: `{json.dumps(task['read_scope'], sort_keys=True)}`",
            "",
            f"Write scope: `{json.dumps(task['write_scope'], sort_keys=True)}`",
            "",
            f"Verification: `{json.dumps(verification['command'])}`",
            "",
        ]
    )

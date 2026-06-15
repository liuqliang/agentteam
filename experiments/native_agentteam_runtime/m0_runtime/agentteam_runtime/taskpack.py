import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


TASKPACK_SCHEMA_VERSION = "taskpack.v1"
TASKPACK_SEMANTIC_CONTRACT_VERSION = "task_semantics.v1"
TASKPACK_VERIFICATION_PROFILE_SCHEMA_VERSION = "agentteam_verification_profile.v1"
DEFAULT_WORKER_ROLE = "implementation_worker"
DEFAULT_DAEMON_MAX_STEPS = 45000
DEFAULT_CODEX_RUNTIME_TIMEOUT_SECONDS = 3600
DEFAULT_LEASE_TIMEOUT_GRACE_SECONDS = 60
DEFAULT_VERIFICATION_COMMAND = ["python3", "-m", "unittest", "discover"]
TASKPACK_TRANSLATABLE_RUNTIME_BACKENDS = {"fake", "codex"}
TASKPACK_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
TASKPACK_GOAL_KINDS = {"implementation", "optimization", "audit"}
OPTIMIZATION_CODE_WORK_TYPES = {"code_implementation", "code_investigation"}
OPTIMIZATION_INTENT_MARKERS = [
    "optimize",
    "optimization",
    "performance",
    "accuracy",
    "latency",
    "throughput",
    "benchmark",
    "metric",
    "profile",
    "hotspot",
    "baseline",
    "speed",
    "优化",
    "性能",
    "准确率",
    "精度",
    "加速",
    "比赛",
    "指标",
    "延迟",
    "耗时",
]
OPTIMIZATION_DECOMPOSITION_MARKERS = [
    "baseline",
    "current behavior",
    "profile",
    "profiling",
    "candidate",
    "matrix",
    "benchmark",
    "metric",
    "measure",
    "measurement",
    "measured",
    "hotspot",
    "基线",
    "当前行为",
    "画像",
    "候选",
    "矩阵",
    "指标",
    "测量",
    "复测",
    "热点",
]
LONG_RUNNING_FOLLOWUP_MARKERS = [
    "follow-up goal:",
    "previous taskpack context:",
    "continue pursuing the long-running goal",
    "previous report",
    "source_report_path",
    "long-goal memory:",
    "goal_memory_path",
    "completed_rounds:",
]
FOLLOWUP_PREVIOUS_EVIDENCE_MARKERS = [
    "previous report",
    "source report",
    "source_report_path",
    "previous findings",
    "previous evidence",
    "verification results",
    "blockers",
    "goal memory",
    "long-goal memory",
    "completed_rounds",
    "latest_run_ids",
    "evidence",
    "report",
]
FOLLOWUP_CONCRETE_PREVIOUS_EVIDENCE_MARKERS = [
    "source_report_path",
    "source report path",
    "verification result",
    "verification results",
    "failed verification",
    "blocker",
    "blockers",
    "goal_memory_path",
    "goal memory path",
    "goal_memory",
    "completed_rounds",
    "latest_run_ids",
    "queue-selected next_goal",
    "queue selected next_goal",
    "selected next_goal",
    "next_goal",
]
FOLLOWUP_MEASURABLE_NEXT_STEP_MARKERS = [
    "next-step",
    "next step",
    "measurable",
    "implement",
    "verify",
    "validate",
    "test",
    "measure",
    "metric",
    "complete",
    "fix",
    "change",
    "下一步",
    "实现",
    "验证",
    "测量",
    "指标",
    "修复",
]
DOCUMENTATION_INTENT_MARKERS = [
    "documentation",
    "document",
    "docs",
    "readme",
    "文档",
    "说明",
]
ROADMAP_FOLLOWUP_REQUIRED_DELIVERABLES = [
    "repository_understanding_summary",
    "previous_evidence_summary",
    "roadmap_followup_route_template",
    "evidence_paths",
    "non_goals",
    "success_metrics_or_no_metric_delta",
    "implemented_changes_or_no_safe_change_rationale",
    "verification_summary",
    "recommended_next_implementation_tasks",
]
AGENTTEAM_TARGET_REVIEW_GATE_DELIVERABLE = "agentteam_target_review_gate"


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
    verification_profile=None,
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
    verification_profile = _normalize_taskpack_verification_profile(verification_profile)
    default_verification_command = verification_profile["correctness"]["command"]
    verification_command = _string_list(verification_command, default_verification_command, "verification_command")
    goal_kind = classify_goal_kind(goal)

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
        "goal_kind": goal_kind,
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
                "objective": _default_task_objective(goal, goal_kind),
                "work_type": _default_work_type(goal_kind),
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
        "verification_profile": verification_profile,
        "success_criteria": [
            "verification command exits with code 0",
            "runtime validation accepts changed files inside declared write_scope",
        ],
    }
    performance_profile = verification_profile.get("performance")
    if isinstance(performance_profile, dict) and (
        performance_profile.get("command") or performance_profile.get("metrics")
    ):
        verification["performance"] = performance_profile

    _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    _write_json(taskpack_dir / "agent_pool.json", agent_pool)
    _write_json(taskpack_dir / "backlog.json", backlog)
    _write_json(taskpack_dir / "verification.json", verification)
    (taskpack_dir / "README.md").write_text(_render_readme(taskpack, backlog, verification), encoding="utf-8")
    return {"taskpack_dir": str(taskpack_dir), "taskpack_id": taskpack_id}


def draft_deterministic_taskpack_skeleton(
    project_root,
    goal,
    draft_root,
    taskpack_id=None,
    context_refs=None,
    verification_command=None,
    verification_profile=None,
    codex_timeout_seconds=1800,
):
    if classify_goal_kind(goal) == "optimization":
        raise TaskpackValidationError(
            "deterministic taskpack skeleton requires semantic authoring for optimization goals"
        )
    result = draft_taskpack_files(
        project_root=project_root,
        goal=goal,
        draft_root=draft_root,
        taskpack_id=taskpack_id,
        read_scope=["."],
        write_scope=[".agentteam/generated/"],
        verification_command=verification_command,
        verification_profile=verification_profile,
        allow_merge=False,
        codex_timeout_seconds=codex_timeout_seconds,
    )
    taskpack_dir = Path(result["taskpack_dir"])
    refs = _string_dict(context_refs, "context_refs")
    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    backlog = _read_json(taskpack_dir / "backlog.json")
    verification = _read_json(taskpack_dir / "verification.json")
    semantic_slots = [
        "task_specific_objective",
        "goal_alignment",
        "read_scope_refinement",
        "write_scope_refinement",
        "verification_plan",
        "evidence_paths",
    ]

    taskpack["authoring_mode"] = "deterministic_skeleton"
    taskpack["semantic_authoring_required"] = True
    taskpack["context_refs"] = refs
    taskpack["semantic_slots"] = semantic_slots
    policy = taskpack.get("policy") if isinstance(taskpack.get("policy"), dict) else {}
    policy["allow_merge"] = False
    policy["operator_review_required"] = True
    taskpack["policy"] = policy

    item = backlog["items"][0]
    item["work_type"] = "code_investigation"
    item["objective"] = (
        "Review the supplied deterministic context references and complete the "
        f"semantic task slots before code changes for: {goal}"
    )
    item["goal_alignment"] = (
        "This deterministic skeleton preserves taskpack.original_goal but does "
        "not infer repository semantics; semantic authoring is required before "
        f"selecting code changes for: {goal}"
    )
    item["required_deliverables"] = [
        "context_refs_review",
        "semantic_slots_completion",
        "verification_summary",
        "recommended_next_implementation_tasks",
    ]
    item["read_scope"] = ["."]
    item["write_scope"] = [".agentteam/generated/"]
    item["semantic_authoring_required"] = True
    item["context_refs"] = refs
    item["semantic_slots"] = semantic_slots
    item["blockers"] = ["semantic_authoring_required"]

    _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    _write_json(taskpack_dir / "backlog.json", backlog)
    (taskpack_dir / "README.md").write_text(_render_readme(taskpack, backlog, verification), encoding="utf-8")
    validate_taskpack(taskpack_dir)
    return result


def materialize_semantic_taskpack(
    skeleton_taskpack_dir,
    output_root,
    semantic_task,
    taskpack_id=None,
):
    skeleton_taskpack_dir = Path(skeleton_taskpack_dir).resolve()
    loaded = load_taskpack(skeleton_taskpack_dir)
    _require_semantic_skeleton(loaded)
    semantic_task = _validate_semantic_task_materialization(semantic_task)

    source_taskpack = loaded["taskpack"]
    source_taskpack_id = _validate_existing_taskpack_id(source_taskpack.get("taskpack_id"))
    taskpack_id = _resolve_materialized_taskpack_id(taskpack_id, source_taskpack_id, output_root)
    output_root = Path(output_root).resolve()
    taskpack_dir = (output_root / taskpack_id).resolve()
    _require_contained_path(taskpack_dir, output_root, "taskpack_dir")
    if taskpack_dir.exists():
        raise TaskpackValidationError(f"materialized taskpack already exists: {taskpack_dir}")

    inventory = _build_taskpack_artifact_inventory(skeleton_taskpack_dir)
    _validate_taskpack_artifact_inventory(skeleton_taskpack_dir, inventory)
    for relative_path, source_path in inventory:
        destination_path = taskpack_dir / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)

    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    backlog = _read_json(
        _resolve_companion_artifact_path(
            taskpack_dir,
            (taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}).get(
                "backlog",
                "backlog.json",
            ),
            "files.backlog",
        )
    )
    verification = _read_json(
        _resolve_companion_artifact_path(
            taskpack_dir,
            (taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}).get(
                "verification",
                "verification.json",
            ),
            "files.verification",
        )
    )

    taskpack["taskpack_id"] = taskpack_id
    taskpack["status"] = "draft"
    taskpack["authoring_mode"] = "semantic_materialized"
    taskpack["semantic_authoring_required"] = False
    taskpack["materialized_from"] = {
        "taskpack_id": source_taskpack_id,
        "taskpack_dir": str(skeleton_taskpack_dir),
        "authoring_mode": source_taskpack.get("authoring_mode") or "unknown",
    }
    taskpack["semantic_slots_completed"] = [
        "task_specific_objective",
        "goal_alignment",
        "read_scope_refinement",
        "write_scope_refinement",
        "verification_plan",
        "evidence_paths",
    ]

    items = backlog.get("items") if isinstance(backlog, dict) else None
    if not isinstance(items, list) or not items:
        raise TaskpackValidationError("skeleton backlog must contain at least one task")
    item = dict(items[0])
    item["task_id"] = f"TASK-{taskpack_id.upper().replace('-', '_')}-001"
    item["objective"] = semantic_task["objective"]
    item["goal_alignment"] = semantic_task["goal_alignment"]
    item["read_scope"] = semantic_task["read_scope"]
    item["write_scope"] = semantic_task["write_scope"]
    item["work_type"] = semantic_task["work_type"]
    item["required_deliverables"] = semantic_task["required_deliverables"]
    item["required_role"] = semantic_task["required_role"]
    item["backlog_status"] = semantic_task["backlog_status"]
    item["risk_target"] = semantic_task["risk_target"]
    item["depends_on"] = semantic_task["depends_on"]
    item["blockers"] = []
    item["semantic_authoring_required"] = False
    item["semantic_materialization"] = {
        "source_taskpack_id": source_taskpack_id,
        "evidence_paths": semantic_task["evidence_paths"],
    }
    if semantic_task["evidence_paths"]:
        item["evidence_paths"] = semantic_task["evidence_paths"]
    for key in ("context_refs", "semantic_slots"):
        if key in item:
            item.pop(key)
    backlog["backlog_id"] = f"BL-{taskpack_id}"
    backlog["items"] = [item]

    verification["command"] = semantic_task["verification_command"]

    _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    _write_json(
        _resolve_companion_artifact_path(
            taskpack_dir,
            (taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}).get(
                "backlog",
                "backlog.json",
            ),
            "files.backlog",
        ),
        backlog,
    )
    _write_json(
        _resolve_companion_artifact_path(
            taskpack_dir,
            (taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}).get(
                "verification",
                "verification.json",
            ),
            "files.verification",
        ),
        verification,
    )
    (taskpack_dir / "README.md").write_text(_render_readme(taskpack, backlog, verification), encoding="utf-8")
    validation = validate_taskpack(taskpack_dir)
    return {
        "taskpack_dir": str(taskpack_dir),
        "taskpack_id": taskpack_id,
        "source_taskpack_id": source_taskpack_id,
        "validation": validation,
    }


def derive_semantic_task_from_skeleton(skeleton_taskpack_dir):
    skeleton_taskpack_dir = Path(skeleton_taskpack_dir).resolve()
    loaded = load_taskpack(skeleton_taskpack_dir)
    _require_semantic_skeleton(loaded)
    return _derive_semantic_task_from_loaded_skeleton(loaded)


def auto_materialize_semantic_taskpack(
    skeleton_taskpack_dir,
    output_root,
    taskpack_id=None,
):
    skeleton_taskpack_dir = Path(skeleton_taskpack_dir).resolve()
    semantic_task = derive_semantic_task_from_skeleton(skeleton_taskpack_dir)
    materialized = materialize_semantic_taskpack(
        skeleton_taskpack_dir,
        output_root=output_root,
        semantic_task=semantic_task,
        taskpack_id=taskpack_id,
    )
    taskpack_dir = Path(materialized["taskpack_dir"])
    loaded = load_taskpack(taskpack_dir)
    taskpack = loaded["taskpack"]
    backlog = loaded["backlog"]
    verification = loaded["verification"]

    taskpack["semantic_completion"] = {
        "completion_schema_version": "taskpack_semantic_completion.v1",
        "authority": "automatic_deterministic",
        "operator_semantic_json_required": False,
        "source": "deterministic_skeleton_context_refs",
        "semantic_task_fields": sorted(semantic_task),
    }
    if _is_agentteam_target_project(taskpack.get("project_root")):
        policy = taskpack.get("policy") if isinstance(taskpack.get("policy"), dict) else {}
        policy["allow_merge"] = False
        policy["operator_review_required"] = True
        policy["merge_requires_verified_integration"] = True
        policy["source_control_restrictions"] = [
            "no_merge",
            "no_push",
            "no_release_activation",
        ]
        taskpack["policy"] = policy

    items = backlog.get("items") if isinstance(backlog, dict) else []
    if isinstance(items, list) and items and isinstance(items[0], dict):
        materialization = items[0].get("semantic_materialization")
        if not isinstance(materialization, dict):
            materialization = {}
        materialization["completion_authority"] = "automatic_deterministic"
        materialization["operator_semantic_json_required"] = False
        items[0]["semantic_materialization"] = materialization

    files = taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}
    backlog_path = _resolve_companion_artifact_path(
        taskpack_dir,
        files.get("backlog", "backlog.json"),
        "files.backlog",
    )
    _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    _write_json(backlog_path, backlog)
    (taskpack_dir / "README.md").write_text(_render_readme(taskpack, backlog, verification), encoding="utf-8")
    validation = validate_taskpack(taskpack_dir)
    materialized["validation"] = validation
    materialized["semantic_task"] = semantic_task
    return materialized


def materialize_deterministic_taskpack_skeleton(
    skeleton_taskpack_dir,
    output_root,
    taskpack_id=None,
):
    return auto_materialize_semantic_taskpack(
        skeleton_taskpack_dir,
        output_root=output_root,
        taskpack_id=taskpack_id,
    )


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
    effective_goal = taskpack.get("original_goal") or taskpack.get("goal") or ""
    classified_goal_kind = classify_goal_kind(effective_goal)
    goal_kind = taskpack.get("goal_kind")
    if semantic_contract_enabled:
        if goal_kind is None:
            goal_kind = classified_goal_kind
        elif not _is_non_empty_string(goal_kind):
            errors.append("goal_kind must be a non-empty string")
            goal_kind = classified_goal_kind
        elif goal_kind not in TASKPACK_GOAL_KINDS:
            errors.append(f"goal_kind must be one of: {', '.join(sorted(TASKPACK_GOAL_KINDS))}")
        elif classified_goal_kind != "implementation" and goal_kind != classified_goal_kind:
            errors.append(f"goal_kind must match original_goal classification: {classified_goal_kind}")
            goal_kind = classified_goal_kind
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
    has_optimization_code_item = False
    is_long_running_followup = (
        semantic_contract_enabled
        and goal_kind == "implementation"
        and _is_long_running_followup_goal(effective_goal)
    )
    semantic_authoring_required = bool(taskpack.get("semantic_authoring_required"))
    has_followup_quality_item = False
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
        work_type = item.get("work_type")
        if semantic_contract_enabled and work_type is not None and not _is_non_empty_string(work_type):
            errors.append(f"{task_id_label} work_type must be a non-empty string")
        if semantic_contract_enabled and not _is_non_empty_string(item.get("goal_alignment")):
            errors.append(f"{task_id_label} goal_alignment must be a non-empty string")
        deliverables = item.get("required_deliverables")
        if semantic_contract_enabled and (not isinstance(deliverables, list) or not deliverables):
            errors.append(f"{task_id_label} required_deliverables must be a non-empty list")
        elif semantic_contract_enabled and not all(_is_non_empty_string(deliverable) for deliverable in deliverables):
            errors.append(f"{task_id_label} required_deliverables entries must be non-empty strings")
        elif semantic_contract_enabled and goal_kind == "optimization" and _is_optimization_code_item(item):
            has_optimization_code_item = True
            if not _optimization_item_preserves_goal_intent(item):
                errors.append(f"{task_id_label} optimization task must preserve optimization intent")
            if not _optimization_item_preserves_decomposition_intent(item):
                errors.append(
                    f"{task_id_label} optimization task must include "
                    "baseline/profile/candidate/metric decomposition intent"
                )
            missing = _missing_optimization_deliverables(deliverables)
            if missing:
                errors.append(
                    f"{task_id_label} optimization required_deliverables missing: {', '.join(missing)}"
                )
        elif semantic_contract_enabled and is_long_running_followup and _is_followup_code_item(item):
            has_followup_quality_item = True
            if not _followup_objective_uses_previous_evidence(item):
                errors.append(f"{task_id_label} long-running follow-up task must tie objective to previous evidence")
            if not _followup_objective_names_concrete_previous_evidence(item):
                errors.append(
                    f"{task_id_label} long-running follow-up task must tie objective "
                    "to concrete previous evidence"
                )
            if not _followup_objective_has_measurable_next_step(item):
                errors.append(
                    f"{task_id_label} long-running follow-up task must define "
                    "a measurable next-step implementation objective"
                )
            missing = _missing_followup_deliverables(deliverables)
            if missing:
                errors.append(
                    f"{task_id_label} long-running follow-up required_deliverables missing: "
                    f"{', '.join(missing)}"
                )
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
    if semantic_contract_enabled and goal_kind == "optimization" and not has_optimization_code_item:
        errors.append(
            "optimization taskpack requires at least one ready code-facing backlog item "
            "with work_type code_implementation or code_investigation and non-document write_scope"
        )
    if (
        semantic_contract_enabled
        and is_long_running_followup
        and not semantic_authoring_required
        and not _goal_requests_documentation(effective_goal)
        and not has_followup_quality_item
    ):
        errors.append(
            "long-running follow-up taskpack requires at least one ready code-facing backlog item "
            "unless the operator asked for documentation"
        )

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


def _normalize_taskpack_verification_profile(profile=None):
    profile = profile if isinstance(profile, dict) else {}
    correctness = profile.get("correctness") if isinstance(profile.get("correctness"), dict) else {}
    performance = profile.get("performance") if isinstance(profile.get("performance"), dict) else {}
    correctness_command = _profile_command_or_default(
        correctness.get("command"),
        DEFAULT_VERIFICATION_COMMAND,
        "verification_profile.correctness.command",
    )
    performance_command = performance.get("command")
    if performance_command is not None:
        performance_command = _profile_command_or_default(
            performance_command,
            None,
            "verification_profile.performance.command",
        )
    metrics = performance.get("metrics", [])
    if not isinstance(metrics, list) or not all(isinstance(metric, str) and metric for metric in metrics):
        raise TaskpackValidationError("verification_profile.performance.metrics must be a string array")
    return {
        "verification_profile_schema_version": TASKPACK_VERIFICATION_PROFILE_SCHEMA_VERSION,
        "correctness": {"command": correctness_command},
        "performance": {
            "command": performance_command,
            "metrics": list(metrics),
        },
    }


def _profile_command_or_default(command, default, field_name):
    if command is None:
        if default is None:
            return None
        return list(default)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise TaskpackValidationError(f"{field_name} must be a non-empty string array")
    return list(command)


def _default_goal_alignment(goal):
    if classify_goal_kind(goal) == "implementation" and _is_long_running_followup_goal(goal):
        return (
            "This long-running follow-up task must preserve taskpack.original_goal, "
            "use the previous report, verification results, blockers, or goal-memory "
            "evidence, and select a measurable next-step implementation objective "
            "instead of generic safe-but-trivial work for: "
            f"{goal}"
        )
    if classify_goal_kind(goal) == "optimization":
        return (
            "This optimization task must preserve the original goal, establish "
            "baseline or current behavior, identify optimization candidates and "
            "metrics, then either implement an evidence-backed repository change "
            "or explain why no safe in-repo change is justified for: "
            f"{goal}"
        )
    return (
        "This task must preserve the original goal and either implement an "
        "evidence-backed repository change or explain why no safe in-repo "
        f"change is justified for: {goal}"
    )


def classify_goal_kind(goal):
    normalized = str(goal or "").lower()
    audit_markers = [
        "audit",
        "review",
        "inspect",
        "check",
        "检查",
        "审计",
        "评估",
    ]
    if any(marker in normalized for marker in OPTIMIZATION_INTENT_MARKERS):
        return "optimization"
    if any(marker in normalized for marker in audit_markers):
        return "audit"
    return "implementation"


def _default_work_type(goal_kind):
    if goal_kind == "audit":
        return "audit"
    return "code_implementation"


def _default_required_deliverables(goal):
    goal_kind = classify_goal_kind(goal)
    if goal_kind == "optimization":
        return [
            "repository_understanding_summary",
            "baseline_or_current_behavior",
            "optimization_candidate_matrix",
            "evidence_paths",
            "implemented_changes_or_no_safe_change_rationale",
            "metric_delta_or_no_safe_change_evidence",
            "verification_summary",
            "recommended_next_implementation_tasks",
        ]
    if _is_long_running_followup_goal(goal):
        return [
            "repository_understanding_summary",
            "previous_evidence_summary",
            "evidence_paths",
            "implemented_changes_or_no_safe_change_rationale",
            "verification_summary",
            "recommended_next_implementation_tasks",
        ]
    if goal_kind == "audit":
        return [
            "repository_understanding_summary",
            "evidence_paths",
            "audit_findings",
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


def _default_task_objective(goal, goal_kind):
    if goal_kind == "implementation" and _is_long_running_followup_goal(goal):
        return (
            "Implement the next measurable follow-up step using previous report "
            f"evidence and verification context for: {goal}"
        )
    return goal


def _is_optimization_code_item(item):
    if _item_requires_semantic_authoring(item):
        return False
    if item.get("backlog_status") not in {None, "ready", "in_progress"}:
        return False
    if item.get("work_type") not in OPTIMIZATION_CODE_WORK_TYPES:
        return False
    write_scope = item.get("write_scope")
    if not isinstance(write_scope, list) or not write_scope:
        return False
    return not _write_scope_is_document_only(write_scope)


def _optimization_item_preserves_goal_intent(item):
    text = " ".join(
        str(value or "")
        for value in [
            item.get("objective"),
            item.get("goal_alignment"),
        ]
    ).lower()
    return any(marker in text for marker in OPTIMIZATION_INTENT_MARKERS)


def _optimization_item_preserves_decomposition_intent(item):
    text = " ".join(
        str(value or "")
        for value in [
            item.get("objective"),
            item.get("goal_alignment"),
        ]
    ).lower()
    return any(marker in text for marker in OPTIMIZATION_DECOMPOSITION_MARKERS)


def _missing_optimization_deliverables(deliverables):
    if not isinstance(deliverables, list):
        return _default_required_deliverables("optimization")
    present = set(deliverables)
    return [
        deliverable
        for deliverable in _default_required_deliverables("optimization")
        if deliverable not in present
    ]


def _is_long_running_followup_goal(goal):
    text = str(goal or "").lower()
    return any(marker in text for marker in LONG_RUNNING_FOLLOWUP_MARKERS)


def _is_followup_code_item(item):
    if _item_requires_semantic_authoring(item):
        return False
    if item.get("backlog_status") not in {None, "ready", "in_progress"}:
        return False
    if item.get("work_type") not in OPTIMIZATION_CODE_WORK_TYPES:
        return False
    write_scope = item.get("write_scope")
    if not isinstance(write_scope, list) or not write_scope:
        return False
    return not _write_scope_is_document_only(write_scope)


def _item_requires_semantic_authoring(item):
    if not isinstance(item, dict):
        return False
    blockers = item.get("blockers")
    has_semantic_blocker = isinstance(blockers, list) and "semantic_authoring_required" in blockers
    return bool(item.get("semantic_authoring_required") or has_semantic_blocker)


def _followup_objective_uses_previous_evidence(item):
    text = str(item.get("objective") or "").lower()
    return any(marker in text for marker in FOLLOWUP_PREVIOUS_EVIDENCE_MARKERS)


def _followup_objective_names_concrete_previous_evidence(item):
    text = str(item.get("objective") or "").lower()
    return any(marker in text for marker in FOLLOWUP_CONCRETE_PREVIOUS_EVIDENCE_MARKERS)


def _followup_objective_has_measurable_next_step(item):
    text = str(item.get("objective") or "").lower()
    return any(marker in text for marker in FOLLOWUP_MEASURABLE_NEXT_STEP_MARKERS)


def _missing_followup_deliverables(deliverables):
    if not isinstance(deliverables, list):
        return _default_required_deliverables("Follow-up goal: previous report")
    present = set(deliverables)
    return [
        deliverable
        for deliverable in _default_required_deliverables("Follow-up goal: previous report")
        if deliverable not in present
    ]


def _goal_requests_documentation(goal):
    text = str(goal or "").lower()
    return any(marker in text for marker in DOCUMENTATION_INTENT_MARKERS)


def _write_scope_is_document_only(write_scope):
    document_scopes = [
        _write_scope_is_document_path(scope)
        for scope in write_scope
        if isinstance(scope, str)
    ]
    return bool(document_scopes) and all(document_scopes)


def _write_scope_is_document_path(scope):
    normalized = scope.strip().lstrip("./")
    if not normalized:
        return False
    lowered = normalized.lower()
    if lowered in {"readme", "readme.md", "readme.rst"}:
        return True
    if lowered.startswith(("docs/", "doc/", "documentation/")):
        return True
    return Path(lowered).suffix in {".md", ".rst", ".txt", ".adoc"}


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
    _raise_if_semantic_authoring_required_for_runtime(loaded)

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


def _raise_if_semantic_authoring_required_for_runtime(loaded):
    taskpack = loaded.get("taskpack") if isinstance(loaded, dict) else {}
    if isinstance(taskpack, dict) and taskpack.get("semantic_authoring_required"):
        taskpack_id = taskpack.get("taskpack_id") or "unknown"
        raise TaskpackValidationError(
            "semantic authoring required before runtime launch "
            f"for taskpack {taskpack_id}; materialize an executable taskpack first"
        )
    backlog = loaded.get("backlog") if isinstance(loaded, dict) else {}
    items = backlog.get("items") if isinstance(backlog, dict) else []
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        blockers = item.get("blockers")
        has_semantic_blocker = isinstance(blockers, list) and "semantic_authoring_required" in blockers
        if item.get("semantic_authoring_required") or has_semantic_blocker:
            task_id = item.get("task_id") or "unknown"
            raise TaskpackValidationError(
                "semantic authoring required before runtime launch "
                f"for backlog item {task_id}; materialize an executable taskpack first"
            )


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


def _resolve_materialized_taskpack_id(taskpack_id, source_taskpack_id, output_root):
    output_root = Path(output_root)
    base_id = taskpack_id
    if base_id is None:
        base_id = f"{source_taskpack_id}-executable"
    return _resolve_draft_taskpack_id(base_id, base_id, output_root)


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


def _require_semantic_skeleton(loaded):
    if not isinstance(loaded, dict):
        raise TaskpackValidationError("taskpack load result must be an object")
    taskpack = loaded.get("taskpack")
    if not isinstance(taskpack, dict):
        raise TaskpackValidationError("taskpack must be an object")
    if not taskpack.get("semantic_authoring_required"):
        raise TaskpackValidationError("semantic materialization requires a taskpack with semantic_authoring_required")
    if taskpack.get("authoring_mode") != "deterministic_skeleton":
        raise TaskpackValidationError("semantic materialization requires a deterministic_skeleton taskpack")
    backlog = loaded.get("backlog")
    items = backlog.get("items") if isinstance(backlog, dict) else None
    if not isinstance(items, list) or not items:
        raise TaskpackValidationError("semantic skeleton backlog must contain at least one task")
    if not any(isinstance(item, dict) and item.get("semantic_authoring_required") for item in items):
        raise TaskpackValidationError("semantic skeleton must contain a semantic_authoring_required backlog item")


def _derive_semantic_task_from_loaded_skeleton(loaded):
    taskpack = loaded["taskpack"]
    backlog = loaded["backlog"]
    verification = loaded["verification"]
    items = backlog.get("items") if isinstance(backlog, dict) else []
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    context_refs = _semantic_context_refs(taskpack, item)
    goal = taskpack.get("original_goal") or taskpack.get("goal") or item.get("objective") or "Complete taskpack goal."
    project_root = taskpack.get("project_root")
    agentteam_target = _is_agentteam_target_project(project_root)
    read_scope = _semantic_scope_from_context(
        context_refs,
        ["read_scope", "read_scopes", "read_scope_refinement"],
        item.get("read_scope") or ["."],
        "read_scope",
    )
    write_scope = _semantic_scope_from_context(
        context_refs,
        ["write_scope", "write_scopes", "write_scope_refinement"],
        item.get("write_scope") or [".agentteam/generated/"],
        "write_scope",
    )
    work_type = _semantic_context_text(context_refs, ["work_type"]) or _default_work_type(
        taskpack.get("goal_kind") or classify_goal_kind(goal)
    )
    if work_type == "audit" and write_scope and not _write_scope_is_document_only(write_scope):
        work_type = "code_implementation"
    required_deliverables = _semantic_required_deliverables(goal, context_refs, agentteam_target)
    evidence_paths = _semantic_evidence_paths(context_refs)
    return {
        "objective": _semantic_objective(goal, context_refs),
        "goal_alignment": _semantic_goal_alignment(goal, context_refs, agentteam_target),
        "read_scope": read_scope,
        "write_scope": write_scope,
        "work_type": work_type,
        "required_deliverables": required_deliverables,
        "required_role": item.get("required_role") or DEFAULT_WORKER_ROLE,
        "backlog_status": item.get("backlog_status") or "ready",
        "risk_target": item.get("risk_target") or "L1",
        "depends_on": item.get("depends_on") if isinstance(item.get("depends_on"), list) else [],
        "evidence_paths": evidence_paths,
        "verification_command": _semantic_verification_command_from_context(context_refs, verification),
    }


def _semantic_context_refs(taskpack, item):
    refs = {}
    for source in [taskpack.get("context_refs"), item.get("context_refs")]:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if not isinstance(key, str) or value is None:
                continue
            text = str(value).strip()
            if text:
                refs[key] = text
    return refs


def _semantic_context_text(context_refs, keys):
    normalized = {
        str(key).lower(): str(value).strip()
        for key, value in (context_refs or {}).items()
        if isinstance(key, str) and str(value).strip()
    }
    for key in keys:
        value = normalized.get(str(key).lower())
        if value:
            return value
    return None


def _semantic_scope_from_context(context_refs, keys, default, field_name):
    value = _semantic_context_text(context_refs, keys)
    if value:
        items = _parse_semantic_ref_list(value, f"context_refs.{keys[0]}")
        if items:
            return items
    items = _string_list(default, [], field_name)
    return items or (["."] if field_name == "read_scope" else [".agentteam/generated/"])


def _parse_semantic_ref_list(value, field_name):
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TaskpackValidationError(f"{field_name} must be a JSON string array or delimited string") from exc
        if not isinstance(decoded, list) or not all(isinstance(item, str) and item.strip() for item in decoded):
            raise TaskpackValidationError(f"{field_name} must be a string array")
        return [item.strip() for item in decoded]

    values = []
    for part in re.split(r"[\n,;]+", text):
        item = part.strip()
        if item.startswith("- "):
            item = item[2:].strip()
        if item:
            values.append(item)
    return values


def _semantic_verification_command_from_context(context_refs, verification):
    default = _validate_taskpack_verification_command(verification)
    value = _semantic_context_text(
        context_refs,
        ["verification_command", "verification_plan_command", "correctness_command"],
    )
    if not value:
        return list(default)
    if value.startswith("["):
        command = _parse_semantic_ref_list(value, "context_refs.verification_command")
    else:
        try:
            command = shlex.split(value)
        except ValueError as exc:
            raise TaskpackValidationError("context_refs.verification_command must be shell-splittable") from exc
    if not command or not all(isinstance(part, str) and part for part in command):
        raise TaskpackValidationError("context_refs.verification_command must be a non-empty string array")
    return command


def _semantic_required_deliverables(goal, context_refs, agentteam_target):
    explicit = _semantic_context_text(context_refs, ["required_deliverables", "deliverables"])
    if explicit:
        deliverables = _parse_semantic_ref_list(explicit, "context_refs.required_deliverables")
    elif _is_roadmap_followup_context(goal, context_refs):
        deliverables = list(ROADMAP_FOLLOWUP_REQUIRED_DELIVERABLES)
    else:
        deliverables = list(_default_required_deliverables(goal))
    if agentteam_target and AGENTTEAM_TARGET_REVIEW_GATE_DELIVERABLE not in deliverables:
        deliverables.append(AGENTTEAM_TARGET_REVIEW_GATE_DELIVERABLE)
    return deliverables


def _semantic_evidence_paths(context_refs):
    evidence_paths = []
    seen = set()
    for key, value in (context_refs or {}).items():
        lowered = str(key).lower()
        if not (
            lowered.endswith("_path")
            or lowered.endswith("_paths")
            or lowered in {"source_report", "previous_report", "repo_context"}
        ):
            continue
        for path in _parse_semantic_ref_list(value, f"context_refs.{key}"):
            if path.lower() in {"not provided", "none", "n/a", "null"}:
                continue
            if path not in seen:
                seen.add(path)
                evidence_paths.append(path)
    return evidence_paths


def _semantic_objective(goal, context_refs):
    source_report_path = _semantic_context_text(context_refs, ["source_report_path"]) or "not provided"
    selected_next_goal = _semantic_context_text(
        context_refs,
        ["selected_next_goal", "queue_selected_next_goal", "next_goal"],
    )
    if selected_next_goal:
        return (
            "Implement and verify the measurable queue-selected next_goal from "
            f"source_report_path {source_report_path}: {selected_next_goal}"
        )
    return (
        "Implement and verify the measurable next step from "
        f"source_report_path {source_report_path} for taskpack.original_goal: {goal}"
    )


def _semantic_goal_alignment(goal, context_refs, agentteam_target):
    source_report_path = _semantic_context_text(context_refs, ["source_report_path"]) or "not provided"
    selected_next_goal = _semantic_context_text(
        context_refs,
        ["selected_next_goal", "queue_selected_next_goal", "next_goal"],
    ) or "not provided"
    non_goals = _semantic_context_text(context_refs, ["non_goals", "non-goals"]) or (
        "merge, push, and release activation" if agentteam_target else "unbounded repository changes"
    )
    alignment = (
        "Advances taskpack.original_goal by automatically completing deterministic "
        "semantic slots from context_refs without operator-written semantic JSON. "
        f"Previous evidence: source_report_path={source_report_path}; "
        f"queue-selected next_goal={selected_next_goal}. "
        f"Non-goals: {non_goals}. Original goal: {goal}"
    )
    if agentteam_target:
        alignment = (
            f"{alignment} AgentTeam-as-target review gate: do not merge, push, "
            "or activate releases; operator review is required."
        )
    return alignment


def _is_roadmap_followup_context(goal, context_refs):
    text = " ".join(
        [str(goal or "")]
        + [
            f"{key} {value}"
            for key, value in (context_refs or {}).items()
            if isinstance(key, str)
        ]
    ).lower()
    markers = [
        "source_report_path",
        "previous report",
        "previous taskpack",
        "goal_memory_path",
        "selected_next_goal",
        "next_goal",
        "queue-selected next_goal",
        "roadmap",
    ]
    return _is_long_running_followup_goal(goal) or any(marker in text for marker in markers)


def _is_agentteam_target_project(project_root):
    if not project_root:
        return False
    return (
        Path(project_root)
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
        / "agentteam_runtime"
    ).is_dir()


def _validate_semantic_task_materialization(semantic_task):
    if not isinstance(semantic_task, dict):
        raise TaskpackValidationError("semantic_task must be an object")
    result = {
        "objective": _required_non_empty_semantic_string(semantic_task, "objective"),
        "goal_alignment": _required_non_empty_semantic_string(semantic_task, "goal_alignment"),
        "read_scope": _required_non_empty_semantic_string_list(semantic_task, "read_scope"),
        "write_scope": _required_non_empty_semantic_string_list(semantic_task, "write_scope"),
        "required_deliverables": _required_non_empty_semantic_string_list(
            semantic_task,
            "required_deliverables",
        ),
        "work_type": _optional_non_empty_semantic_string(
            semantic_task,
            "work_type",
            "code_implementation",
        ),
        "required_role": _optional_non_empty_semantic_string(
            semantic_task,
            "required_role",
            DEFAULT_WORKER_ROLE,
        ),
        "backlog_status": _optional_non_empty_semantic_string(
            semantic_task,
            "backlog_status",
            "ready",
        ),
        "risk_target": _optional_non_empty_semantic_string(
            semantic_task,
            "risk_target",
            "L1",
        ),
        "depends_on": _optional_semantic_string_list(semantic_task, "depends_on", []),
        "evidence_paths": _optional_semantic_string_list(semantic_task, "evidence_paths", []),
        "verification_command": _optional_semantic_string_list(
            semantic_task,
            "verification_command",
            DEFAULT_VERIFICATION_COMMAND,
        ),
    }
    if not result["verification_command"]:
        raise TaskpackValidationError("semantic_task.verification_command must be a non-empty string array")
    return result


def _required_non_empty_semantic_string(semantic_task, field_name):
    value = semantic_task.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise TaskpackValidationError(f"semantic_task.{field_name} must be a non-empty string")
    return value.strip()


def _optional_non_empty_semantic_string(semantic_task, field_name, default):
    value = semantic_task.get(field_name, default)
    if not isinstance(value, str) or not value.strip():
        raise TaskpackValidationError(f"semantic_task.{field_name} must be a non-empty string")
    return value.strip()


def _required_non_empty_semantic_string_list(semantic_task, field_name):
    values = _optional_semantic_string_list(semantic_task, field_name, None)
    if not values:
        raise TaskpackValidationError(f"semantic_task.{field_name} must be a non-empty string array")
    return values


def _optional_semantic_string_list(semantic_task, field_name, default):
    value = semantic_task.get(field_name, default)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise TaskpackValidationError(f"semantic_task.{field_name} must be a string array")
    return [item.strip() for item in value]


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


def _string_dict(value, field_name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TaskpackValidationError(f"{field_name} must be an object")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise TaskpackValidationError(f"{field_name} keys must be non-empty strings")
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result[key] = text
    return result


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
            f"Goal kind: `{taskpack.get('goal_kind') or classify_goal_kind(taskpack.get('original_goal') or taskpack['goal'])}`",
            "",
            f"Project root: `{taskpack['project_root']}`",
            "",
            f"Task: `{task['task_id']}`",
            "",
            f"Work type: `{task.get('work_type') or 'not specified'}`",
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

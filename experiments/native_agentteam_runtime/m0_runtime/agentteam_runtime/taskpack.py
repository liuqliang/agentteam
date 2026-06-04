import json
import re
from pathlib import Path


TASKPACK_SCHEMA_VERSION = "taskpack.v1"
DEFAULT_WORKER_ROLE = "implementation_worker"
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
    taskpack_id = _normalize_taskpack_id(taskpack_id, goal)
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
        "project_root": str(project_root),
        "goal": goal,
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


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80].strip("-") or "taskpack"


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _render_readme(taskpack, backlog, verification):
    task = backlog["items"][0]
    return "\n".join(
        [
            f"# {taskpack['taskpack_id']}",
            "",
            f"Goal: {taskpack['goal']}",
            "",
            f"Project root: `{taskpack['project_root']}`",
            "",
            f"Task: `{task['task_id']}`",
            "",
            f"Read scope: `{json.dumps(task['read_scope'], sort_keys=True)}`",
            "",
            f"Write scope: `{json.dumps(task['write_scope'], sort_keys=True)}`",
            "",
            f"Verification: `{json.dumps(verification['command'])}`",
            "",
        ]
    )

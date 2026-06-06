import json
import re
import subprocess
from pathlib import Path

from .repo_map import build_repository_map
from .taskpack import (
    TaskpackValidationError,
    _require_contained_path,
    _resolve_draft_taskpack_id,
    draft_taskpack_files,
    validate_taskpack,
)


REQUIRED_TASKPACK_FILES = [
    "taskpack.yaml",
    "agent_pool.json",
    "backlog.json",
    "verification.json",
    "README.md",
]


def draft_taskpack_from_goal(
    project_root,
    goal,
    draft_root,
    author_runtime="fake",
    taskpack_id=None,
    codex_command=None,
    codex_timeout_seconds=600,
):
    if author_runtime == "fake":
        return draft_taskpack_files(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            read_scope=["."],
            write_scope=[".agentteam/generated/"],
            verification_command=["python3", "-m", "unittest", "discover"],
            codex_timeout_seconds=codex_timeout_seconds,
        )
    if author_runtime == "codex":
        return _draft_with_codex(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            codex_command=codex_command,
            codex_timeout_seconds=codex_timeout_seconds,
        )
    raise TaskpackValidationError(f"unsupported taskpack author runtime: {author_runtime}")


def _draft_with_codex(
    project_root,
    goal,
    draft_root,
    taskpack_id=None,
    codex_command=None,
    codex_timeout_seconds=600,
):
    project_root = Path(project_root).resolve()
    draft_root = Path(draft_root).resolve()
    taskpack_id = _resolve_draft_taskpack_id(
        taskpack_id,
        goal,
        draft_root,
        extra_reserved_path_templates=[".{taskpack_id}-author"],
    )
    taskpack_dir = (draft_root / taskpack_id).resolve()
    author_context_dir = (draft_root / f".{taskpack_id}-author").resolve()
    _require_contained_path(taskpack_dir, draft_root, "taskpack_dir")
    _require_contained_path(author_context_dir, draft_root, "author_context_dir")
    if _path_is_relative_to(draft_root, project_root) or _path_is_relative_to(
        project_root,
        draft_root,
    ):
        raise TaskpackValidationError("codex taskpack draft_root must not overlap the target repository")

    repo_status_before = _git_status_signature(project_root)
    if repo_status_before["status"]:
        raise TaskpackValidationError("codex taskpack author requires a clean target repository")

    taskpack_dir.mkdir(parents=True, exist_ok=False)
    author_context_dir.mkdir(parents=True, exist_ok=False)

    repo_map = build_repository_map(project_root, author_context_dir)

    prompt = _author_prompt(
        project_root=project_root,
        goal=goal,
        taskpack_id=taskpack_id,
        taskpack_dir=taskpack_dir,
        author_context_dir=author_context_dir,
        repo_map=repo_map,
    )
    prompt_path = author_context_dir / "author_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    command = _command_list(codex_command)
    result_path = author_context_dir / "author_result.json"
    try:
        completed = subprocess.run(
            command,
            cwd=draft_root,
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=codex_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_json(
            result_path,
            {
                "status": "timed_out",
                "timeout_seconds": codex_timeout_seconds,
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            },
        )
        _raise_if_target_repo_modified(project_root, repo_status_before)
        raise TaskpackValidationError("codex taskpack author timed out") from exc

    _write_json(
        result_path,
        {
            "status": "completed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )

    _raise_if_target_repo_modified(project_root, repo_status_before)
    if completed.returncode != 0:
        raise TaskpackValidationError(f"codex taskpack author failed with exit code {completed.returncode}")

    _verify_required_taskpack_files(taskpack_dir)
    _canonicalize_codex_taskpack_files(taskpack_dir)
    validate_taskpack(taskpack_dir)
    return {
        "taskpack_dir": str(taskpack_dir),
        "taskpack_id": taskpack_id,
        "author_context_path": str(author_context_dir),
        "author_result_path": str(result_path),
    }


def _author_prompt(
    project_root,
    goal,
    taskpack_id,
    taskpack_dir,
    author_context_dir,
    repo_map,
):
    repo_paths = repo_map["paths"]
    return "\n".join(
        [
            "You are the AgentTeam taskpack author.",
            "",
            "Author a draft taskpack for this goal:",
            goal,
            "",
            f"Project root, read-only: {project_root}",
            f"Taskpack directory to write: {taskpack_dir}",
            f"Author context directory, read/write helpers allowed here: {author_context_dir}",
            "",
            "Do not edit the project root. Do not run repository-changing commands.",
            "Write only these files directly inside the taskpack directory:",
            *[f"- {name}" for name in REQUIRED_TASKPACK_FILES],
            "",
            (
                "Do not create helper files, subdirectories, symlinks, "
                "author_context/, or hidden files inside the taskpack directory."
            ),
            "If you need scratch notes, write them under the author context directory only.",
            "",
            "Repository map context:",
            f"- manifest: {repo_paths['manifest_path']}",
            f"- inventory: {repo_paths['inventory_path']}",
            f"- symbols: {repo_paths['symbols_path']}",
            "",
            "The runtime loader currently reads taskpack.yaml as JSON despite the .yaml suffix.",
            "Use valid JSON for taskpack.yaml, agent_pool.json, backlog.json, and verification.json.",
            "",
            "Minimum required content:",
            f"- taskpack.taskpack_schema_version: taskpack.v1",
            f"- taskpack.taskpack_id: {taskpack_id}",
            "- taskpack.status: draft",
            f"- taskpack.project_root: {project_root}",
            "- taskpack.runtime.default_backend: codex",
            "- taskpack.files maps agent_pool, backlog, and verification to the JSON filenames above",
            "- agent_pool contains at least one idle agent with role implementation_worker",
            "- backlog.items contains at least one ready item with required_role implementation_worker",
            "- backlog item read_scope is a non-empty string array",
            "- backlog item write_scope is a narrow repository-relative string array; never use repository root",
            "- verification.command is a non-empty string array using an allowed executable such as python3",
            "- README.md briefly summarizes the taskpack goal, scopes, and verification command",
            "",
            "When finished, exit successfully. Do not print a long explanation.",
        ]
    )


def _verify_required_taskpack_files(taskpack_dir):
    taskpack_dir = Path(taskpack_dir)
    required = set(REQUIRED_TASKPACK_FILES)
    missing = []
    invalid = []
    for name in REQUIRED_TASKPACK_FILES:
        path = taskpack_dir / name
        if not path.exists():
            missing.append(name)
        elif path.is_symlink() or not path.is_file():
            invalid.append(name)

    if missing:
        raise TaskpackValidationError(
            f"codex taskpack author missed required files: {', '.join(missing)}"
        )
    if invalid:
        raise TaskpackValidationError(
            f"codex taskpack author wrote invalid taskpack files: {', '.join(invalid)}"
        )

    unexpected = sorted(
        path.name
        for path in taskpack_dir.iterdir()
        if path.name not in required
    )
    if unexpected:
        raise TaskpackValidationError(
            f"codex taskpack author left unexpected taskpack artifacts: {', '.join(unexpected)}"
        )


def _canonicalize_codex_taskpack_files(taskpack_dir):
    taskpack_dir = Path(taskpack_dir)
    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    files = taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}

    agent_pool_path = taskpack_dir / files.get("agent_pool", "agent_pool.json")
    agent_pool = _read_json(agent_pool_path)
    if isinstance(agent_pool, dict):
        if not agent_pool.get("scheduler_agent_id"):
            agent_pool["scheduler_agent_id"] = "agent-scheduler"
        agents = agent_pool.get("agents")
        if isinstance(agents, list):
            for agent in agents:
                if not isinstance(agent, dict):
                    continue
                agent_id = agent.get("agent_id")
                if agent_id and not agent.get("inbox_path"):
                    agent["inbox_path"] = f"mailboxes/{agent_id}/inbox.jsonl"
                if agent_id and not agent.get("outbox_path"):
                    agent["outbox_path"] = f"mailboxes/{agent_id}/outbox.jsonl"
        _write_json(agent_pool_path, agent_pool)

    backlog_path = taskpack_dir / files.get("backlog", "backlog.json")
    backlog = _read_json(backlog_path)
    if isinstance(backlog, dict) and isinstance(backlog.get("items"), list):
        for item in backlog["items"]:
            if not isinstance(item, dict):
                continue
            if not item.get("task_id") and item.get("item_id"):
                item["task_id"] = item["item_id"]
            if not item.get("objective") and item.get("title"):
                item["objective"] = item["title"]
            if not item.get("backlog_status") and item.get("status"):
                item["backlog_status"] = item["status"]
            if "blockers" not in item:
                item["blockers"] = []
        _write_json(backlog_path, backlog)

    verification_path = taskpack_dir / files.get("verification", "verification.json")
    verification = _read_json(verification_path)
    if isinstance(verification, dict):
        command = verification.get("command")
        project_root = taskpack.get("project_root")
        canonical_command = _canonical_verification_command(command, project_root)
        if canonical_command != command:
            verification["command"] = canonical_command
            _write_json(verification_path, verification)


def _canonical_verification_command(command, project_root):
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return command
    project_python = _project_python(project_root)
    python_index = _python_command_index(command)
    if python_index is None:
        return command
    python_executable = str(project_python) if project_python is not None else "python3"
    if _is_env_python_wrapper(command, python_index):
        return [python_executable, *command[python_index + 1 :]]
    canonical = list(command)
    if python_index == 0 or project_python is not None:
        canonical[python_index] = python_executable
    return canonical


def _is_env_python_wrapper(command, python_index):
    if python_index != 1:
        return False
    executable = Path(command[0]).name
    return executable == "env"


def _project_python(project_root):
    if not project_root:
        return None
    root = Path(project_root)
    candidates = [
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def _python_command_index(command):
    for index, part in enumerate(command[:3]):
        if _is_python_command(part):
            return index
    return None


def _is_python_command(value):
    path = Path(value)
    name = path.name
    if value in {".venv/bin/python", "venv/bin/python"}:
        return True
    return bool(re.fullmatch(r"python(?:3(?:\.\d+)?)?", name))


def _command_list(command):
    if command is None:
        return ["codex", "exec", "--skip-git-repo-check"]
    if isinstance(command, str):
        raise TaskpackValidationError("codex_command must be a list or tuple of strings, not a bare string")
    if not isinstance(command, (list, tuple)):
        raise TaskpackValidationError("codex_command must be a list or tuple of strings")
    items = list(command)
    if not items or not all(isinstance(item, str) for item in items):
        raise TaskpackValidationError("codex_command must be a non-empty string array")
    return items


def _path_is_relative_to(path, root):
    try:
        Path(path).relative_to(root)
    except ValueError:
        return False
    return True


def _raise_if_target_repo_modified(project_root, repo_status_before):
    repo_status_after = _git_status_signature(project_root)
    if repo_status_after != repo_status_before:
        raise TaskpackValidationError("codex taskpack author modified the target repository")


def _git_status_signature(project_root):
    status = subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise TaskpackValidationError(f"failed to inspect target repository status: {status.stderr.strip()}")
    return {
        "head": _git_optional_output(project_root, ["rev-parse", "--verify", "HEAD"]),
        "branch": _git_optional_output(project_root, ["rev-parse", "--abbrev-ref", "HEAD"]),
        "status": tuple(status.stdout.splitlines()),
    }


def _git_optional_output(project_root, args):
    completed = subprocess.run(
        ["git", "-C", str(project_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

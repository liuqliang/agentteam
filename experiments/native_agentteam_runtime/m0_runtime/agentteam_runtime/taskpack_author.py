import json
import subprocess
from pathlib import Path

from .repo_map import build_repository_map
from .taskpack import (
    TaskpackValidationError,
    _normalize_taskpack_id,
    _require_contained_path,
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
    taskpack_id = _normalize_taskpack_id(taskpack_id, goal)
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
    if repo_status_before:
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

    repo_status_after = _git_status_signature(project_root)
    if repo_status_after != repo_status_before:
        raise TaskpackValidationError("codex taskpack author modified the target repository")
    if completed.returncode != 0:
        raise TaskpackValidationError(f"codex taskpack author failed with exit code {completed.returncode}")

    _verify_required_taskpack_files(taskpack_dir)
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


def _command_list(command):
    if command is None:
        return ["codex", "exec"]
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


def _git_status_signature(project_root):
    completed = subprocess.run(
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
    if completed.returncode != 0:
        raise TaskpackValidationError(f"failed to inspect target repository status: {completed.stderr.strip()}")
    return tuple(completed.stdout.splitlines())


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

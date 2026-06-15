import subprocess
from collections import Counter
from pathlib import Path


REPO_GROUNDING_SCHEMA_VERSION = "repo_grounding.v1"
REPO_STRUCTURE_SCHEMA_VERSION = "repo_structure.v1"

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".sh": "shell",
}

CATEGORY_SORT_ORDER = {
    "source": 0,
    "test": 1,
    "docs": 2,
    "config": 3,
    "build": 4,
    "unknown": 5,
}

SKIP_PATH_PARTS = {
    ".git",
    ".agentteam",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}


def build_repo_grounding(project_root, max_files=5000, sample_limit=5):
    project_root = Path(project_root).resolve()
    warnings = []
    relative_paths = _repo_files(project_root, warnings, max_files=max_files)
    language_counts = Counter()
    language_samples = {}
    for relative_path in relative_paths:
        language = _language_for_path(relative_path)
        if language == "unknown":
            continue
        language_counts[language] += 1
        language_samples.setdefault(language, [])
        if len(language_samples[language]) < sample_limit:
            language_samples[language].append(relative_path)

    project_tools = _project_tools(project_root, relative_paths)
    test_entrypoints = _test_entrypoints(relative_paths)
    candidate_verification_commands = _candidate_verification_commands(
        project_tools,
        test_entrypoints,
    )
    repository_structure = _repository_structure(relative_paths)
    return {
        "grounding_schema_version": REPO_GROUNDING_SCHEMA_VERSION,
        "project_root": str(project_root),
        "scan_status": "degraded" if warnings else "ok",
        "tracked_file_count": len(relative_paths),
        "languages": [
            {
                "language": language,
                "file_count": count,
                "sample_files": language_samples.get(language, []),
            }
            for language, count in sorted(
                language_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "project_tools": project_tools,
        "test_entrypoints": test_entrypoints,
        "candidate_verification_commands": candidate_verification_commands,
        "repository_structure": repository_structure,
        "warnings": warnings,
    }


def render_repo_grounding_text(grounding):
    languages = grounding.get("languages") or []
    tools = grounding.get("project_tools") or []
    commands = grounding.get("candidate_verification_commands") or []
    lines = [
        f"project: {grounding.get('project') or 'unknown'}",
        f"grounding_status: {grounding.get('scan_status') or 'unknown'}",
        f"tracked_files: {grounding.get('tracked_file_count', 0)}",
        "languages: " + _join_or_none(
            f"{item['language']}={item['file_count']}" for item in languages
        ),
        "project_tools: " + _join_or_none(
            f"{item['tool_id']}:{item['path']}" for item in tools
        ),
        "candidate_verification: " + _join_or_none(
            " ".join(item["command"]) for item in commands
        ),
    ]
    structure = grounding.get("repository_structure") or {}
    top_level_entries = structure.get("top_level_entries") or []
    lines.append(
        "repository_structure: " + _join_or_none(
            f"{item['path']}:{item['entry_type']}[{item['file_count']}]"
            for item in top_level_entries
        )
    )
    if grounding.get("warnings"):
        lines.append(f"warnings: {len(grounding['warnings'])}")
    return "\n".join(lines) + "\n"


def _repo_files(project_root, warnings, *, max_files):
    completed = _run(["git", "-C", str(project_root), "ls-files"])
    if completed.returncode == 0:
        source = completed.stdout.splitlines()
    else:
        warnings.append(
            {
                "warning": "git_ls_files_failed",
                "stderr": completed.stderr.strip(),
            }
        )
        fallback = _run(["rg", "--files"], cwd=project_root)
        if fallback.returncode != 0:
            warnings.append(
                {
                    "warning": "rg_files_fallback_failed",
                    "stderr": fallback.stderr.strip(),
                }
            )
            return []
        warnings.append({"warning": "used_rg_files_fallback"})
        source = fallback.stdout.splitlines()
    filtered = [
        path
        for path in source
        if path and not _is_ignored_path(path) and (project_root / path).is_file()
    ]
    if len(filtered) > max_files:
        warnings.append(
            {
                "warning": "file_limit_reached",
                "max_files": max_files,
                "actual_file_count": len(filtered),
            }
        )
        filtered = filtered[:max_files]
    return sorted(filtered)


def _project_tools(project_root, relative_paths):
    paths = set(relative_paths)
    candidates = [
        ("python-pyproject", "python", "pyproject.toml", [["python3", "-m", "unittest", "discover"]]),
        ("python-setup", "python", "setup.py", [["python3", "-m", "unittest", "discover"]]),
        ("python-requirements", "python", "requirements.txt", [["python3", "-m", "unittest", "discover"]]),
        ("node-package-json", "node", "package.json", [["npm", "test"]]),
        ("make", "make", "Makefile", [["make", "test"]]),
        ("cmake", "cmake", "CMakeLists.txt", [["ctest", "--test-dir", "build"]]),
        ("cargo", "rust", "Cargo.toml", [["cargo", "test"]]),
        ("go-module", "go", "go.mod", [["go", "test", "./..."]]),
        ("maven", "java", "pom.xml", [["mvn", "test"]]),
        ("gradle", "java", "build.gradle", [["gradle", "test"]]),
        ("gradle-kts", "java", "build.gradle.kts", [["gradle", "test"]]),
        ("meson", "meson", "meson.build", [["meson", "test", "-C", "build"]]),
    ]
    tools = []
    for tool_id, tool_type, path, commands in candidates:
        if path not in paths:
            continue
        tools.append(
            {
                "tool_id": tool_id,
                "tool_type": tool_type,
                "path": path,
                "candidate_commands": commands,
            }
        )
    if "package.json" in paths and (project_root / "package-lock.json").exists():
        _append_lockfile(tools, "node-package-json", "package-lock.json")
    return tools


def _append_lockfile(tools, tool_id, lockfile):
    for tool in tools:
        if tool["tool_id"] == tool_id:
            tool["lockfile"] = lockfile
            return


def _test_entrypoints(relative_paths):
    entrypoints = []
    for path in sorted(relative_paths):
        path_obj = Path(path)
        name = path_obj.name
        language = _language_for_path(path)
        hint = None
        if language == "python" and (
            "tests" in path_obj.parts
            or name.startswith("test_")
            or name.endswith("_test.py")
        ):
            hint = "python"
        elif language in {"javascript", "typescript"} and (
            name.endswith(".test.js")
            or name.endswith(".spec.js")
            or name.endswith(".test.ts")
            or name.endswith(".spec.ts")
            or "tests" in path_obj.parts
        ):
            hint = "node"
        elif language == "go" and name.endswith("_test.go"):
            hint = "go"
        elif language in {"c", "cpp"} and "test" in name.lower():
            hint = "native"
        elif language == "rust" and ("tests" in path_obj.parts or name.endswith("_test.rs")):
            hint = "rust"
        if hint:
            entrypoints.append(
                {
                    "path": path,
                    "language": language,
                    "test_framework_hint": hint,
                }
            )
    return entrypoints


def _candidate_verification_commands(project_tools, test_entrypoints):
    commands = []
    for tool in project_tools:
        for command in tool.get("candidate_commands") or []:
            _append_command(
                commands,
                command,
                reason=f"detected {tool['tool_id']} at {tool['path']}",
            )
    entrypoint_hints = {entry["test_framework_hint"] for entry in test_entrypoints}
    if "python" in entrypoint_hints:
        _append_command(
            commands,
            ["python3", "-m", "unittest", "discover"],
            reason="detected python test files",
        )
    if "node" in entrypoint_hints:
        _append_command(commands, ["npm", "test"], reason="detected node test files")
    if "go" in entrypoint_hints:
        _append_command(commands, ["go", "test", "./..."], reason="detected go test files")
    if "rust" in entrypoint_hints:
        _append_command(commands, ["cargo", "test"], reason="detected rust test files")
    return commands


def _append_command(commands, command, *, reason):
    if any(existing["command"] == command for existing in commands):
        return
    commands.append({"command": command, "reason": reason})


def _repository_structure(relative_paths):
    entries = [
        {
            "path": path,
            "language": _language_for_path(path),
            "category": _category_for_path(path),
        }
        for path in sorted(relative_paths)
    ]
    return {
        "structure_schema_version": REPO_STRUCTURE_SCHEMA_VERSION,
        "tracked_file_count": len(entries),
        "category_counts": _count_summary(entries, "category"),
        "language_counts": _count_summary(entries, "language"),
        "top_level_entries": _top_level_entries(entries),
    }


def _top_level_entries(entries):
    groups = {}
    for entry in entries:
        path_parts = Path(entry["path"]).parts
        if not path_parts:
            continue
        if len(path_parts) == 1:
            top_level_path = path_parts[0]
            entry_type = "file"
        else:
            top_level_path = f"{path_parts[0]}/"
            entry_type = "directory"
        group = groups.setdefault(
            top_level_path,
            {
                "path": top_level_path,
                "entry_type": entry_type,
                "files": [],
            },
        )
        group["files"].append(entry)

    return [
        {
            "path": group["path"],
            "entry_type": group["entry_type"],
            "file_count": len(group["files"]),
            "category_counts": _count_summary(group["files"], "category"),
            "language_counts": _count_summary(group["files"], "language"),
        }
        for group in sorted(groups.values(), key=lambda item: item["path"])
    ]


def _count_summary(entries, field):
    counts = Counter(entry.get(field) or "unknown" for entry in entries)
    return [
        {field: value, "file_count": counts[value]}
        for value in sorted(
            counts,
            key=lambda value: (
                -counts[value],
                _count_sort_order(field, value),
                value,
            ),
        )
    ]


def _count_sort_order(field, value):
    if field == "category":
        return CATEGORY_SORT_ORDER.get(value, len(CATEGORY_SORT_ORDER))
    return 0


def _language_for_path(path):
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower(), "unknown")


def _category_for_path(path):
    path_obj = Path(path)
    path_parts = set(path_obj.parts)
    name = path_obj.name.lower()
    suffix = path_obj.suffix.lower()
    language = _language_for_path(path)
    if "tests" in path_parts or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if name == "makefile" or suffix in {".mk"}:
        return "build"
    if suffix in {".md", ".rst", ".txt"} or "docs" in path_parts:
        return "docs"
    if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}:
        return "config"
    if language in {
        "python",
        "javascript",
        "typescript",
        "c",
        "cpp",
        "java",
        "go",
        "rust",
        "shell",
    }:
        return "source"
    return "unknown"


def _is_ignored_path(path):
    return bool(set(Path(path).parts) & SKIP_PATH_PARTS)


def _join_or_none(items):
    values = list(items)
    if not values:
        return "none"
    return ", ".join(values)


def _run(command, cwd=None):
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))

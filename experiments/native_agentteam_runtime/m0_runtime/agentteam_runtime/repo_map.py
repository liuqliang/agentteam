import ast
import hashlib
import json
import re
import subprocess
from copy import deepcopy
from pathlib import Path


REPO_MAP_SCHEMA_VERSION = "repo_map.v1"
REPO_INVENTORY_SCHEMA_VERSION = "repo_inventory.v1"
REPO_CONTEXT_SCHEMA_VERSION = "repo_context.v1"
REPO_SYMBOLS_SCHEMA_VERSION = "repo_symbols.v1"
SYMBOL_EXTRACTION_VERSION = "python_ast.v1"


def build_repository_map(project_root, output_dir, max_file_bytes=65536):
    project_root = Path(project_root)
    output_dir = Path(output_dir)
    repo_map_dir = output_dir / "state" / "repo_map"
    repo_map_dir.mkdir(parents=True, exist_ok=True)

    warnings = []
    inventory_path = repo_map_dir / "inventory.json"
    symbols_path = repo_map_dir / "symbols.json"
    manifest_path = repo_map_dir / "manifest.json"
    inventory_options = {
        "max_file_bytes": max_file_bytes,
    }
    repo_commit = _git_stdout(project_root, ["rev-parse", "HEAD"])
    working_tree_state, dirty_status = _working_tree_state(project_root)
    cached = _load_reusable_repo_map(
        project_root,
        manifest_path,
        inventory_path,
        symbols_path,
        repo_commit,
        working_tree_state,
        inventory_options,
    )
    if cached:
        return cached

    if working_tree_state == "dirty_or_unversioned":
        warnings.append(
            {
                "warning": "working_tree_dirty",
                "status_count": len(dirty_status),
                "status_sample": dirty_status[:20],
            }
        )

    tracked_files = _tracked_files(project_root, warnings)
    inventory = {
        "inventory_schema_version": REPO_INVENTORY_SCHEMA_VERSION,
        "project_root": str(project_root),
        "files": [
            _inventory_entry(project_root, relative_path, max_file_bytes, warnings)
            for relative_path in tracked_files
        ],
    }
    inventory["files"] = [
        entry for entry in inventory["files"] if entry is not None
    ]
    symbols = _build_symbols(project_root, inventory["files"], max_file_bytes, warnings)

    manifest = {
        "repo_map_schema_version": REPO_MAP_SCHEMA_VERSION,
        "project_root": str(project_root),
        "scan_status": "degraded" if warnings else "ok",
        "cache_status": "rebuilt",
        "repo_commit": repo_commit,
        "working_tree_state": working_tree_state,
        "warning_count": len(warnings),
        "warnings": warnings,
        "inventory_path": str(inventory_path),
        "symbols_path": str(symbols_path),
        "inventory_file_count": len(inventory["files"]),
        "symbol_file_count": len(symbols["files"]),
        "symbol_extraction_version": SYMBOL_EXTRACTION_VERSION,
        "inventory_options": inventory_options,
    }

    _write_json(inventory_path, inventory)
    _write_json(symbols_path, symbols)
    _write_json(manifest_path, manifest)

    return {
        "manifest": manifest,
        "inventory": inventory,
        "symbols": symbols,
        "paths": {
            "inventory_path": str(inventory_path),
            "symbols_path": str(symbols_path),
            "manifest_path": str(manifest_path),
        },
    }


def _load_reusable_repo_map(
    project_root,
    manifest_path,
    inventory_path,
    symbols_path,
    repo_commit,
    working_tree_state,
    inventory_options,
):
    if working_tree_state != "clean" or not repo_commit:
        return None
    if not manifest_path.exists() or not inventory_path.exists() or not symbols_path.exists():
        return None
    try:
        previous_manifest = _read_json(manifest_path)
        inventory = _read_json(inventory_path)
        symbols = _read_json(symbols_path)
    except (json.JSONDecodeError, OSError):
        return None
    if previous_manifest.get("repo_map_schema_version") != REPO_MAP_SCHEMA_VERSION:
        return None
    if previous_manifest.get("project_root") != str(project_root):
        return None
    if previous_manifest.get("repo_commit") != repo_commit:
        return None
    if previous_manifest.get("working_tree_state") != "clean":
        return None
    if previous_manifest.get("inventory_options") != inventory_options:
        return None
    if previous_manifest.get("symbol_extraction_version") != SYMBOL_EXTRACTION_VERSION:
        return None

    manifest = deepcopy(previous_manifest)
    manifest["cache_status"] = "reused"
    manifest["repo_commit"] = repo_commit
    manifest["working_tree_state"] = working_tree_state
    _write_json(manifest_path, manifest)
    return {
        "manifest": manifest,
        "inventory": inventory,
        "symbols": symbols,
        "paths": {
            "inventory_path": str(inventory_path),
            "symbols_path": str(symbols_path),
            "manifest_path": str(manifest_path),
        },
    }


def build_repo_context(
    project_root,
    output_dir,
    task,
    agent_role,
    max_files=8,
    max_file_bytes=65536,
    context_id=None,
):
    output_dir = Path(output_dir)
    repo_map = build_repository_map(
        project_root,
        output_dir,
        max_file_bytes=max_file_bytes,
    )
    task_id = str(task.get("task_id") or "unknown-task")
    context_name = _safe_context_name(context_id or task_id)
    role_name = _safe_context_name(agent_role or "unknown-role")
    context_path = output_dir / "repo_contexts" / f"{context_name}-{role_name}.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)

    symbol_by_path = {
        file_symbols["path"]: file_symbols
        for file_symbols in repo_map["symbols"]["files"]
    }
    ranked = _rank_inventory_files(repo_map["inventory"]["files"], symbol_by_path, task)
    selected = ranked[:max_files]
    context = {
        "repo_context_schema_version": REPO_CONTEXT_SCHEMA_VERSION,
        "repo_context_path": str(context_path),
        "task_id": task_id,
        "agent_role": agent_role,
        "repo_map_manifest_path": repo_map["paths"]["manifest_path"],
        "repo_map_inventory_path": repo_map["paths"]["inventory_path"],
        "repo_map_symbols_path": repo_map["paths"]["symbols_path"],
        "selected_files": [
            _context_file_entry(entry, symbol_by_path.get(entry["path"]), score, reasons)
            for score, reasons, entry in selected
        ],
        "omitted_file_count": max(0, len(ranked) - len(selected)),
        "warnings": repo_map["manifest"]["warnings"],
    }
    _write_json(context_path, context)
    return context


def _tracked_files(project_root, warnings):
    completed = _run(["git", "-C", str(project_root), "ls-files"])
    if completed.returncode == 0:
        return sorted(
            path for path in completed.stdout.splitlines()
            if path and not _is_generated_or_vendor_path(path)
        )

    warnings.append(
        {
            "warning": "git_ls_files_failed",
            "stderr": completed.stderr.strip(),
        }
    )
    fallback = _run(["rg", "--files"], cwd=project_root)
    if fallback.returncode == 0:
        warnings.append({"warning": "used_rg_files_fallback"})
        return sorted(
            path for path in fallback.stdout.splitlines()
            if path and not _is_generated_or_vendor_path(path)
        )

    warnings.append(
        {
            "warning": "rg_files_fallback_failed",
            "stderr": fallback.stderr.strip(),
        }
    )
    return []


def _rank_inventory_files(inventory_files, symbol_by_path, task):
    read_scope = _string_list(task.get("read_scope"))
    write_scope = _string_list(task.get("write_scope"))
    objective_tokens = _objective_tokens(task.get("objective"))
    ranked = []
    for entry in inventory_files:
        score = 0
        reasons = []
        path = entry["path"]
        if _path_in_scopes(path, write_scope):
            score += 100
            reasons.append("write_scope")
        if _path_in_scopes(path, read_scope):
            score += 50
            reasons.append("read_scope")
        if _matches_objective(path, symbol_by_path.get(path), objective_tokens):
            score += 10
            reasons.append("objective")
        ranked.append((score, reasons, entry))
    ranked.sort(key=lambda item: (-item[0], item[2]["path"]))
    return ranked


def _context_file_entry(entry, symbols, score, reasons):
    context_entry = {
        "path": entry["path"],
        "language": entry["language"],
        "category": entry["category"],
        "size_bytes": entry["size_bytes"],
        "selection_score": score,
        "selection_reasons": reasons,
    }
    if "sha256" in entry:
        context_entry["sha256"] = entry["sha256"]
    if symbols:
        context_entry["symbols"] = {
            "imports": symbols["imports"],
            "functions": symbols["functions"],
            "classes": symbols["classes"],
        }
    return context_entry


def _path_in_scopes(path, scopes):
    if not scopes:
        return False
    normalized_path = _normalize_scope(path)
    for scope in scopes:
        normalized_scope = _normalize_scope(scope)
        if normalized_scope in {"", "."}:
            return True
        if normalized_scope.endswith("/"):
            if normalized_path.startswith(normalized_scope):
                return True
        elif normalized_path == normalized_scope:
            return True
    return False


def _normalize_scope(value):
    normalized = str(value).strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _objective_tokens(objective):
    if not objective:
        return set()
    tokens = set()
    for token in re.findall(r"[A-Za-z0-9_]+", str(objective).lower()):
        tokens.add(token)
        tokens.update(part for part in token.split("_") if part)
    return tokens


def _matches_objective(path, symbols, objective_tokens):
    if not objective_tokens:
        return False
    haystack = _objective_haystack(path, symbols)
    return any(token in haystack for token in objective_tokens)


def _objective_haystack(path, symbols):
    values = [path.lower()]
    if symbols:
        values.extend(symbol.lower() for symbol in symbols["imports"])
        values.extend(function["name"].lower() for function in symbols["functions"])
        for class_symbols in symbols["classes"]:
            values.append(class_symbols["name"].lower())
            values.extend(method["name"].lower() for method in class_symbols["methods"])
    return "\n".join(values)


def _string_list(value):
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _safe_context_name(value):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return safe or "unknown"


def _inventory_entry(project_root, relative_path, max_file_bytes, warnings):
    file_path = project_root / relative_path
    try:
        stat = file_path.stat()
    except FileNotFoundError:
        warnings.append({"path": relative_path, "warning": "missing_during_scan"})
        return None

    entry = {
        "path": relative_path,
        "size_bytes": stat.st_size,
        "language": _language_for_path(relative_path),
        "category": _category_for_path(relative_path),
    }
    if stat.st_size <= max_file_bytes:
        entry["sha256"] = _sha256(file_path)
    else:
        warnings.append(
            {
                "path": relative_path,
                "warning": "file_exceeds_max_file_bytes",
                "size_bytes": stat.st_size,
                "max_file_bytes": max_file_bytes,
            }
        )
    return entry


def _build_symbols(project_root, inventory_files, max_file_bytes, warnings):
    symbol_files = []
    for inventory_entry in inventory_files:
        if inventory_entry["language"] != "python":
            continue
        if inventory_entry["size_bytes"] > max_file_bytes:
            warnings.append(
                {
                    "path": inventory_entry["path"],
                    "warning": "symbol_file_exceeds_max_file_bytes",
                    "size_bytes": inventory_entry["size_bytes"],
                    "max_file_bytes": max_file_bytes,
                }
            )
            continue
        symbol_files.append(
            _python_symbol_summary(project_root, inventory_entry["path"], warnings)
        )

    return {
        "symbols_schema_version": REPO_SYMBOLS_SCHEMA_VERSION,
        "symbol_extraction_version": SYMBOL_EXTRACTION_VERSION,
        "files": [
            file_symbols for file_symbols in symbol_files
            if file_symbols is not None
        ],
    }


def _python_symbol_summary(project_root, relative_path, warnings):
    file_path = project_root / relative_path
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
    except (SyntaxError, UnicodeDecodeError) as exc:
        warnings.append(
            {
                "path": relative_path,
                "warning": "python_ast_parse_failed",
                "message": str(exc),
            }
        )
        return None
    except FileNotFoundError:
        warnings.append({"path": relative_path, "warning": "missing_during_symbol_scan"})
        return None

    return {
        "path": relative_path,
        "imports": _python_imports(tree),
        "functions": _python_top_level_functions(tree),
        "classes": _python_classes(tree),
    }


def _python_imports(tree):
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _append_unique(imports, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                name = f"{module}.{alias.name}" if module else alias.name
                _append_unique(imports, name)
    return imports


def _python_top_level_functions(tree):
    functions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({"name": node.name, "line": node.lineno})
    return functions


def _python_classes(tree):
    classes = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = []
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append({"name": child.name, "line": child.lineno})
        classes.append({"name": node.name, "line": node.lineno, "methods": methods})
    return classes


def _append_unique(values, value):
    if value not in values:
        values.append(value)


def _language_for_path(path):
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".md": "markdown",
        ".rst": "restructuredtext",
        ".txt": "text",
        ".json": "json",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".java": "java",
        ".sh": "shell",
    }.get(suffix, "unknown")


def _category_for_path(path):
    path_obj = Path(path)
    path_parts = set(path_obj.parts)
    name = path_obj.name.lower()
    language = _language_for_path(path)
    if "tests" in path_parts or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if language in {"markdown", "restructuredtext", "text"} or "docs" in path_parts:
        return "docs"
    if path_obj.suffix.lower() in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}:
        return "config"
    if language in {
        "python",
        "javascript",
        "typescript",
        "rust",
        "go",
        "c",
        "cpp",
        "java",
        "shell",
    }:
        return "source"
    return "unknown"


def _is_generated_or_vendor_path(path):
    parts = set(Path(path).parts)
    return bool(parts & {"__pycache__", ".git", "node_modules"})


def _sha256(file_path):
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        return _MissingCommandResult(str(exc))


def _git_stdout(project_root, arguments):
    completed = _run(["git", "-C", str(project_root), *arguments])
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _working_tree_state(project_root):
    completed = _run(["git", "-C", str(project_root), "status", "--porcelain"])
    if completed.returncode != 0:
        return "unknown", []
    status = completed.stdout.splitlines()
    if status:
        return "dirty_or_unversioned", status
    return "clean", []


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, payload):
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class _MissingCommandResult:
    returncode = 127
    stdout = ""

    def __init__(self, stderr):
        self.stderr = stderr

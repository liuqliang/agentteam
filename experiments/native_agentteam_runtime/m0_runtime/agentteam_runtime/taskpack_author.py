import json
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .repo_grounding import build_repo_grounding
from .repo_map import build_repository_map
from .taskpack import (
    TASKPACK_SEMANTIC_CONTRACT_VERSION,
    TaskpackValidationError,
    auto_materialize_semantic_taskpack,
    classify_goal_kind,
    _default_goal_alignment,
    _default_required_deliverables,
    _default_work_type,
    _normalize_taskpack_verification_profile,
    _require_contained_path,
    _resolve_draft_taskpack_id,
    draft_deterministic_taskpack_skeleton,
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
    verification_profile=None,
    progress_callback=None,
    progress_interval_seconds=30.0,
):
    if author_runtime == "fake":
        return draft_taskpack_files(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            read_scope=["."],
            write_scope=[".agentteam/generated/"],
            verification_command=None,
            verification_profile=verification_profile,
            codex_timeout_seconds=codex_timeout_seconds,
        )
    if author_runtime == "deterministic":
        return _draft_with_deterministic_author(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            codex_timeout_seconds=codex_timeout_seconds,
            verification_profile=verification_profile,
        )
    if author_runtime == "codex":
        return _draft_with_codex(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            codex_command=codex_command,
            codex_timeout_seconds=codex_timeout_seconds,
            verification_profile=verification_profile,
            progress_callback=progress_callback,
            progress_interval_seconds=progress_interval_seconds,
        )
    raise TaskpackValidationError(f"unsupported taskpack author runtime: {author_runtime}")


def _draft_with_deterministic_author(
    project_root,
    goal,
    draft_root,
    taskpack_id=None,
    codex_timeout_seconds=600,
    verification_profile=None,
):
    project_root = Path(project_root).resolve()
    draft_root = Path(draft_root).resolve()
    taskpack_id = _resolve_draft_taskpack_id(
        taskpack_id,
        goal,
        draft_root,
        extra_reserved_path_templates=[".{taskpack_id}-deterministic-author"],
    )
    author_context_dir = (draft_root / f".{taskpack_id}-deterministic-author").resolve()
    _require_contained_path(author_context_dir, draft_root, "author_context_dir")
    author_context_dir.mkdir(parents=True, exist_ok=False)

    repo_map = build_repository_map(project_root, author_context_dir)
    grounding = build_repo_grounding(project_root)
    verification_command = _deterministic_author_verification_command(
        grounding,
        verification_profile,
    )
    read_scope, write_scope, scope_diagnostic = _deterministic_author_scopes(
        repo_map,
        goal,
    )
    context_refs = _deterministic_author_context_refs(
        goal=goal,
        repo_map=repo_map,
        grounding=grounding,
        read_scope=read_scope,
        write_scope=write_scope,
        scope_diagnostic=scope_diagnostic,
        verification_command=verification_command,
    )

    skeleton_root = author_context_dir / "skeletons"
    skeleton = draft_deterministic_taskpack_skeleton(
        project_root=project_root,
        goal=goal,
        draft_root=skeleton_root,
        taskpack_id=f"{taskpack_id}-skeleton",
        context_refs=context_refs,
        verification_command=verification_command,
        verification_profile=verification_profile,
        codex_timeout_seconds=codex_timeout_seconds,
    )
    materialized = auto_materialize_semantic_taskpack(
        skeleton["taskpack_dir"],
        output_root=draft_root,
        taskpack_id=taskpack_id,
    )
    return {
        "taskpack_dir": materialized["taskpack_dir"],
        "taskpack_id": materialized["taskpack_id"],
        "author_runtime": "deterministic",
        "source_taskpack_id": materialized["source_taskpack_id"],
    }


def _deterministic_author_context_refs(
    *,
    goal,
    repo_map,
    grounding,
    read_scope,
    write_scope,
    scope_diagnostic,
    verification_command,
):
    manifest = repo_map.get("manifest") if isinstance(repo_map.get("manifest"), dict) else {}
    paths = repo_map.get("paths") if isinstance(repo_map.get("paths"), dict) else {}
    structure = grounding.get("repository_structure") if isinstance(grounding, dict) else {}
    languages = [
        f"{item.get('language')}={item.get('file_count')}"
        for item in grounding.get("languages", [])
        if isinstance(item, dict) and item.get("language")
    ]
    tools = [
        f"{item.get('tool_id')}:{item.get('path')}"
        for item in grounding.get("project_tools", [])
        if isinstance(item, dict) and item.get("tool_id") and item.get("path")
    ]
    top_level_entries = [
        f"{item.get('path')}[{item.get('file_count')}]"
        for item in structure.get("top_level_entries", [])
        if isinstance(item, dict) and item.get("path")
    ]
    structure_budget = (
        structure.get("top_level_entry_budget")
        if isinstance(structure.get("top_level_entry_budget"), dict)
        else {
            "max_entries": len(structure.get("top_level_entries") or []),
            "total_entry_count": len(structure.get("top_level_entries") or []),
            "included_count": len(structure.get("top_level_entries") or []),
            "omitted_count": 0,
        }
    )
    selected_next_goal = (
        "Use repo_grounding.v1 and repo_structure.v1 deterministic signals to "
        f"implement the bounded next step: {goal}"
    )
    return {
        "source_report_path": "not provided",
        "goal_memory_path": "not provided",
        "selected_next_goal": selected_next_goal,
        "repo_map_manifest_path": paths.get("manifest_path") or "not provided",
        "repo_map_inventory_path": paths.get("inventory_path") or "not provided",
        "repo_map_symbols_path": paths.get("symbols_path") or "not provided",
        "repo_grounding_schema_version": grounding.get("grounding_schema_version") or "repo_grounding.v1",
        "repo_structure_schema_version": structure.get("structure_schema_version") or "repo_structure.v1",
        "repo_grounding_languages": _deterministic_context_json(
            [
                {
                    "language": item.get("language"),
                    "file_count": item.get("file_count"),
                    "sample_files": item.get("sample_files") or [],
                }
                for item in (grounding.get("languages") or [])[:8]
                if isinstance(item, dict) and item.get("language")
            ]
        ),
        "repo_grounding_project_tools": _deterministic_context_json(
            [
                {
                    "tool_id": item.get("tool_id"),
                    "tool_type": item.get("tool_type"),
                    "path": item.get("path"),
                    "candidate_commands": item.get("candidate_commands") or [],
                }
                for item in (grounding.get("project_tools") or [])[:8]
                if isinstance(item, dict) and item.get("tool_id")
            ]
        ),
        "repo_grounding_test_entrypoints": _deterministic_context_json(
            [
                {
                    "path": item.get("path"),
                    "language": item.get("language"),
                    "test_framework_hint": item.get("test_framework_hint"),
                }
                for item in (grounding.get("test_entrypoints") or [])[:12]
                if isinstance(item, dict) and item.get("path")
            ]
        ),
        "repo_grounding_candidate_verification_commands": _deterministic_context_json(
            [
                {
                    "command": item.get("command"),
                    "reason": item.get("reason") or "deterministic repo grounding",
                }
                for item in (grounding.get("candidate_verification_commands") or [])[:8]
                if isinstance(item, dict) and item.get("command")
            ]
        ),
        "repo_structure_category_counts": _deterministic_context_json(
            (structure.get("category_counts") or [])[:8]
        ),
        "repo_structure_language_counts": _deterministic_context_json(
            (structure.get("language_counts") or [])[:8]
        ),
        "repo_structure_top_level_entries": _deterministic_context_json(
            structure.get("top_level_entries") or []
        ),
        "repo_structure_budget": _deterministic_context_json(structure_budget),
        "deterministic_scope_diagnostic": _deterministic_context_json(scope_diagnostic),
        "repo_grounding_summary": "; ".join(
            [
                f"scan_status={grounding.get('scan_status') or 'unknown'}",
                f"tracked_files={grounding.get('tracked_file_count', 0)}",
                "languages=" + ",".join(languages[:8]),
                "tools=" + ",".join(tools[:8]),
            ]
        ),
        "repo_structure_summary": "; ".join(
            [
                f"repo_map_scan_status={manifest.get('scan_status') or 'unknown'}",
                f"top_level_entries={','.join(top_level_entries[:12])}",
                f"top_level_omitted={structure_budget.get('omitted_count', 0)}",
            ]
        ),
        "read_scope": "\n".join(read_scope),
        "write_scope": "\n".join(write_scope),
        "verification_command": json.dumps(verification_command),
        "required_deliverables": "\n".join(
            list(_default_required_deliverables(goal)) + ["agentteam_target_review_gate"]
        ),
        "non_goals": "natural-language report formatting; DB-primary storage; model adapters; merge; push; release activation",
    }


def _deterministic_context_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _deterministic_author_verification_command(grounding, verification_profile):
    profile = _normalize_taskpack_verification_profile(verification_profile)
    correctness = profile.get("correctness") if isinstance(profile.get("correctness"), dict) else {}
    command = correctness.get("command") if isinstance(correctness, dict) else None
    if isinstance(command, list) and command and all(isinstance(part, str) and part for part in command):
        return list(command)
    for candidate in grounding.get("candidate_verification_commands", []):
        if not isinstance(candidate, dict):
            continue
        command = candidate.get("command")
        if isinstance(command, list) and command and all(isinstance(part, str) and part for part in command):
            return list(command)
    return ["python3", "-m", "unittest", "discover"]


def _deterministic_author_scopes(repo_map, goal, max_source_files=8, max_test_files=4):
    inventory = repo_map.get("inventory") if isinstance(repo_map.get("inventory"), dict) else {}
    files = inventory.get("files") if isinstance(inventory.get("files"), list) else []
    goal_tokens = _deterministic_goal_tokens(goal)
    diagnostic_matches = []
    ranked = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        category = entry.get("category") or "unknown"
        if category not in {"source", "test"}:
            continue
        score, matched_tokens = _deterministic_path_score(path, goal_tokens)
        if score <= 0:
            continue
        diagnostic_matches.extend(matched_tokens)
        ranked.append((score, category, path))

    ranked.sort(key=lambda item: (-item[0], _deterministic_category_order(item[1]), item[2]))
    source_paths = [
        path
        for _score, category, path in ranked
        if category == "source"
    ][:max_source_files]
    test_paths = [
        path
        for _score, category, path in ranked
        if category == "test"
    ][:max_test_files]
    if not source_paths:
        source_paths = _fallback_scope_paths(files, category="source", limit=max_source_files)
    if not test_paths:
        test_paths = _fallback_scope_paths(files, category="test", limit=max_test_files)

    write_scope = _dedupe_paths(source_paths + test_paths)
    if not write_scope:
        write_scope = [".agentteam/generated/"]
    read_scope = _dedupe_paths(write_scope)
    diagnostic = _deterministic_scope_diagnostic(
        goal_tokens=goal_tokens,
        matched_goal_tokens=diagnostic_matches,
        write_scope=write_scope,
    )
    return read_scope, write_scope, diagnostic


def _deterministic_goal_tokens(goal):
    text = str(goal or "").lower()
    tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", text)
        if len(token) >= 3
    }
    if "repo_grounding" in text or "grounding" in tokens:
        tokens.update({"repo", "grounding"})
    if "repo_structure" in text or "structure" in tokens:
        tokens.update({"repo", "structure"})
    if "taskpack" in text:
        tokens.add("taskpack")
    if "author" in text or "authoring" in tokens:
        tokens.add("author")
    if "semantic" in tokens or "materialization" in tokens:
        tokens.update({"semantic", "materialize", "materialization"})
    if "status" in tokens or "state" in tokens or "liveness" in tokens:
        tokens.update({"status", "scheduler", "report"})
    if "report" in tokens or "summary" in tokens:
        tokens.update({"report", "summary", "completion", "operator"})
    if "notification" in tokens or "notify" in tokens or "feishu" in tokens:
        tokens.update({"notification", "notify", "feishu"})
    if "worker" in tokens or "heartbeat" in tokens or "checkpoint" in tokens or "outbox" in tokens:
        tokens.update({"worker", "heartbeat", "mailbox", "pool"})
    if "token" in tokens or "usage" in tokens:
        tokens.update({"token", "usage"})
    if "scope" in tokens or "diagnostic" in tokens or "diagnostics" in tokens:
        tokens.update({"scope", "diagnostic", "author", "taskpack"})
    return tokens


def _deterministic_path_score(path, goal_tokens):
    path_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", path.lower())
        if len(token) >= 3
    }
    matched_tokens = sorted((path_tokens - _DETERMINISTIC_SCOPE_STOP_TOKENS) & goal_tokens)
    score = len(matched_tokens)
    lowered = path.lower()
    for marker in ("taskpack", "author", "grounding", "repo_map", "semantic"):
        if marker in lowered and marker.replace("_", "") in goal_tokens:
            score += 2
            matched_tokens.append(marker.replace("_", ""))
        elif marker in lowered and marker in goal_tokens:
            score += 2
            matched_tokens.append(marker)
    for rule in _DETERMINISTIC_SCOPE_RULES:
        if not (goal_tokens & rule["triggers"]):
            continue
        if any(marker in lowered for marker in rule["path_markers"]):
            score += rule["weight"]
            matched_tokens.extend(sorted(goal_tokens & rule["triggers"]))
    return score, sorted(set(matched_tokens))


_DETERMINISTIC_SCOPE_STOP_TOKENS = {
    "agentteam",
    "code",
    "experiments",
    "implementation",
    "m0",
    "native",
    "python",
    "repo",
    "runtime",
    "test",
    "tests",
}


_DETERMINISTIC_SCOPE_RULES = [
    {
        "triggers": {"scheduler", "status", "state", "liveness", "inflight", "lease"},
        "path_markers": ("two_phase_scheduler", "scheduler", "agentteam.py", "cli.py"),
        "expected_module": "two_phase_scheduler.py",
        "weight": 5,
    },
    {
        "triggers": {"report", "summary", "completion", "operator"},
        "path_markers": ("operator_report", "completion_summary", "operator_brief"),
        "expected_module": "operator_report.py",
        "weight": 5,
    },
    {
        "triggers": {"notification", "notify", "feishu"},
        "path_markers": ("notifications",),
        "expected_module": "notifications.py",
        "weight": 5,
    },
    {
        "triggers": {"worker", "heartbeat", "checkpoint", "outbox", "mailbox", "pool"},
        "path_markers": ("mailbox_worker", "worker_pool"),
        "expected_module": "mailbox_worker.py",
        "weight": 5,
    },
    {
        "triggers": {"token", "usage"},
        "path_markers": ("token_usage",),
        "expected_module": "token_usage.py",
        "weight": 5,
    },
    {
        "triggers": {"scope", "diagnostic", "author", "taskpack"},
        "path_markers": ("taskpack_author", "taskpack.py"),
        "expected_module": "taskpack_author.py",
        "weight": 5,
    },
]


def _deterministic_scope_diagnostic(*, goal_tokens, matched_goal_tokens, write_scope):
    expected_modules = []
    missing_expected_modules = []
    for rule in _DETERMINISTIC_SCOPE_RULES:
        if not (goal_tokens & rule["triggers"]):
            continue
        expected_module = rule["expected_module"]
        expected_modules.append(expected_module)
        if not any(expected_module in path for path in write_scope):
            missing_expected_modules.append(expected_module)
    confidence = "high" if not missing_expected_modules and write_scope != [".agentteam/generated/"] else "low"
    return {
        "diagnostic_schema_version": "deterministic_scope_diagnostic.v1",
        "confidence": confidence,
        "authoring_needs_review": confidence == "low",
        "matched_goal_tokens": sorted(set(matched_goal_tokens)),
        "expected_modules": sorted(set(expected_modules)),
        "missing_expected_modules": sorted(set(missing_expected_modules)),
        "selected_write_scope_count": len(write_scope),
    }


def _deterministic_category_order(category):
    return {"source": 0, "test": 1}.get(category, 2)


def _fallback_scope_paths(files, *, category, limit):
    paths = [
        entry.get("path")
        for entry in files
        if isinstance(entry, dict)
        and entry.get("category") == category
        and isinstance(entry.get("path"), str)
    ]
    return sorted(paths)[:limit]


def _dedupe_paths(paths):
    result = []
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _draft_with_codex(
    project_root,
    goal,
    draft_root,
    taskpack_id=None,
    codex_command=None,
    codex_timeout_seconds=600,
    verification_profile=None,
    progress_callback=None,
    progress_interval_seconds=30.0,
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
        verification_profile=verification_profile,
    )
    prompt_path = author_context_dir / "author_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    command = _command_list(codex_command)
    result_path = author_context_dir / "author_result.json"
    state_path = author_context_dir / "author_state.json"
    completed = _run_codex_author_command(
        command,
        draft_root=draft_root,
        prompt=prompt,
        timeout_seconds=codex_timeout_seconds,
        state_path=state_path,
        result_path=result_path,
        taskpack_id=taskpack_id,
        taskpack_dir=taskpack_dir,
        author_context_dir=author_context_dir,
        prompt_path=prompt_path,
        progress_callback=progress_callback,
        progress_interval_seconds=progress_interval_seconds,
    )
    if completed.returncode != -9:
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
    if completed.returncode == -9:
        raise TaskpackValidationError(
            _codex_author_failure_message(
                "codex taskpack author timed out",
                result_path=result_path,
                state_path=state_path,
            )
        )
    if completed.returncode != 0:
        raise TaskpackValidationError(f"codex taskpack author failed with exit code {completed.returncode}")

    _verify_required_taskpack_files(taskpack_dir)
    _canonicalize_codex_taskpack_files(taskpack_dir)
    _apply_verification_profile_to_taskpack(taskpack_dir, verification_profile)
    validate_taskpack(taskpack_dir)
    return {
        "taskpack_dir": str(taskpack_dir),
        "taskpack_id": taskpack_id,
        "author_context_path": str(author_context_dir),
        "author_result_path": str(result_path),
    }


def _run_codex_author_command(
    command,
    draft_root,
    prompt,
    timeout_seconds,
    state_path,
    result_path,
    taskpack_id,
    taskpack_dir,
    author_context_dir,
    prompt_path,
    progress_callback=None,
    progress_interval_seconds=30.0,
):
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    interval = max(float(progress_interval_seconds or 0), 0.5)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            with subprocess.Popen(
                command,
                cwd=draft_root,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            ) as process:
                base_state = {
                    "author_status": "running",
                    "taskpack_id": taskpack_id,
                    "pid": process.pid,
                    "command": command,
                    "started_at": started_at,
                    "taskpack_dir": str(taskpack_dir),
                    "author_context_dir": str(author_context_dir),
                    "prompt_path": str(prompt_path),
                    "result_path": str(result_path),
                    "timeout_seconds": timeout_seconds,
                }
                _write_author_state(state_path, base_state, started_monotonic, progress_callback)
                if process.stdin:
                    try:
                        process.stdin.write(prompt)
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                next_progress_at = time.monotonic() + interval
                timed_out = False
                while process.poll() is None:
                    now = time.monotonic()
                    elapsed = now - started_monotonic
                    if timeout_seconds is not None and elapsed >= timeout_seconds:
                        timed_out = True
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                        break
                    if now >= next_progress_at:
                        _write_author_state(state_path, base_state, started_monotonic, progress_callback)
                        next_progress_at = now + interval
                    time.sleep(min(interval, 0.2))
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read()
                stderr = stderr_file.read()
                returncode = -9 if timed_out else process.returncode
                status = "timed_out" if timed_out else ("completed" if returncode == 0 else "failed")
                completed = subprocess.CompletedProcess(command, returncode, stdout, stderr)
                diagnostic = _codex_author_diagnostic(taskpack_dir, stdout, stderr)
                _write_json(
                    result_path,
                    {
                        "status": status,
                        "exit_code": returncode,
                        "timeout_seconds": timeout_seconds,
                        "stdout": stdout,
                        "stderr": stderr,
                        "diagnostic": diagnostic,
                    },
                )
                final_state = {
                    **base_state,
                    "author_status": status,
                    "exit_code": returncode,
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr.encode("utf-8")),
                    "diagnostic": diagnostic,
                    "finished_at": _utc_now(),
                }
                _write_author_state(state_path, final_state, started_monotonic, progress_callback)
                return completed


def _write_author_state(state_path, state, started_monotonic, progress_callback=None):
    state_path = Path(state_path)
    state = {
        **state,
        "state_path": str(state_path),
        "elapsed_seconds": round(max(time.monotonic() - started_monotonic, 0.0), 3),
        "updated_at": _utc_now(),
    }
    _write_json(state_path, state)
    if progress_callback:
        progress_callback(dict(state))


def _utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _author_prompt(
    project_root,
    goal,
    taskpack_id,
    taskpack_dir,
    author_context_dir,
    repo_map,
    verification_profile=None,
):
    repo_paths = repo_map["paths"]
    verification_profile_json = json.dumps(verification_profile or {}, sort_keys=True)
    lines = [
        "You are the AgentTeam taskpack author.",
        "",
        "Author a draft taskpack for this goal:",
        goal,
        "",
        f"Project root, read-only: {project_root}",
        f"Taskpack directory to write: {taskpack_dir}",
        f"Author context directory, read/write helpers allowed here: {author_context_dir}",
        "",
        *_direct_artifact_protocol_prompt(),
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
        "Project verification profile:",
        verification_profile_json,
        "",
        "The runtime loader currently reads taskpack.yaml as JSON despite the .yaml suffix.",
        "Use valid JSON for taskpack.yaml, agent_pool.json, backlog.json, and verification.json.",
        "",
        "Minimum required content:",
        f"- taskpack.taskpack_schema_version: taskpack.v1",
        f"- taskpack.taskpack_id: {taskpack_id}",
        "- taskpack.status: draft",
        f"- taskpack.semantic_contract_version: {TASKPACK_SEMANTIC_CONTRACT_VERSION}",
        f"- taskpack.project_root: {project_root}",
        f"- taskpack.goal: {goal}",
        f"- taskpack.original_goal: {goal}",
        "- taskpack.goal_kind: one of implementation, optimization, audit",
        "- taskpack.runtime.default_backend: codex",
        "- taskpack.files maps agent_pool, backlog, and verification to the JSON filenames above",
        "- agent_pool contains at least one idle agent with role implementation_worker",
        "- backlog.items contains at least one ready item with required_role implementation_worker",
        "- each backlog item must include work_type, for example code_implementation, code_investigation, or audit",
        "- each backlog item must include goal_alignment explaining how it advances taskpack.original_goal",
        "- each backlog item must include required_deliverables as a non-empty string array",
        "- Preserve the operator's original goal in taskpack.original_goal and in every executable backlog item.",
        (
            "- decompose broad or long-running goals into narrow, measurable next-step tasks "
            "with concrete read_scope, write_scope, evidence, and verification expectations"
        ),
        (
            "- for follow-up goals with previous report, previous taskpack, or goal-memory context, "
            "tie each executable next-step objective to previous evidence, verification results, "
            "blockers, or reported next steps"
        ),
        (
            "- include a concise rationale naming source_report_path, verification results, "
            "blockers, goal_memory_path, or the queue-selected next_goal"
        ),
        (
            "- avoid safe-but-trivial documentation-only changes unless the operator explicitly asked "
            "for documentation; otherwise prefer bounded code/test/repository changes or explain why "
            "no safe in-repo change is justified"
        ),
        "",
        *_roadmap_followup_template_prompt(),
        "",
        (
            "- optimization goals, including optimize/optimization/performance/accuracy/"
            "latency/benchmark/metric/比赛/优化/性能/准确率/延迟, "
            "must set taskpack.goal_kind to optimization"
        ),
        (
            "- never downgrade an optimization goal into an audit/completeness task; the executable "
            "task objective or goal_alignment must preserve optimization, performance, metric, "
            "accuracy, latency, or benchmark intent"
        ),
        (
            "- optimization taskpacks must include at least one ready backlog item with work_type "
            "code_implementation or code_investigation and non-document write_scope"
        ),
        (
            "- optimization code-facing tasks must explicitly mention baseline/current behavior, "
            "profiling, candidate matrix, metrics, measurements, or hotspots in objective or "
            "goal_alignment"
        ),
        (
            "- optimization taskpacks must not fall back to only README/docs fixes unless the "
            "taskpack explicitly proves no safe code-facing work exists"
        ),
        (
            "- optimization required_deliverables must include repository_understanding_summary, "
            "baseline_or_current_behavior, optimization_candidate_matrix, evidence_paths, "
            "implemented_changes_or_no_safe_change_rationale, metric_delta_or_no_safe_change_evidence, "
            "verification_summary, and recommended_next_implementation_tasks"
        ),
        "- backlog item read_scope is a non-empty string array",
        "- backlog item write_scope is a narrow repository-relative string array; never use repository root",
        "- verification.command is a non-empty string array using an allowed executable such as python3",
        "- if the project verification profile has correctness.command, use it as verification.command",
        "- copy any project performance command and metrics into verification.performance",
        "- README.md briefly summarizes the taskpack goal, scopes, and verification command",
        "",
        "When finished, exit successfully. Do not print a long explanation.",
    ]
    if _is_agentteam_target_project(project_root):
        lines.extend(["", *_agentteam_target_policy_prompt()])
    return "\n".join(lines)


def _is_agentteam_target_project(project_root):
    if not project_root:
        return False
    project_root = Path(project_root)
    return (
        project_root
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
        / "agentteam_runtime"
    ).is_dir()


def _direct_artifact_protocol_prompt():
    return [
        "Direct artifact-production protocol:",
        "- This is a file-authoring job, not a planning, research, or design session.",
        (
            "- Do not read skill docs, create specs or plans, invoke workflow checklists, "
            "or run broad source exploration before writing the draft."
        ),
        (
            "- First write the five required taskpack files before optional exploration "
            "or refinement."
        ),
        (
            "- If context is incomplete, write a conservative valid taskpack with a "
            "bounded audit or implementation item instead of spending the budget on "
            "open-ended analysis."
        ),
    ]


def _agentteam_target_policy_prompt():
    return [
        "AgentTeam-as-target policy:",
        "- Treat this as ordinary goal-directed implementation against the AgentTeam repository.",
        "- If the operator gave a functional or semantic requirement, read the relevant repository context, identify affected modules, and translate it into bounded implementation tasks.",
        "- If the operator gave a concrete code request, create a narrow direct implementation task.",
        "- If the operator gave only open-ended improvement requests, create an audit/planning task or block for clarification instead of broad source edits.",
        "- preserve the original operator requirement in taskpack.original_goal and each backlog item goal_alignment.",
        "- do not request git merge or git push; source merge, push, and release activation require operator review after the run.",
        "- include agentteam_target_review_gate in required_deliverables.",
    ]


def _codex_author_diagnostic(taskpack_dir, stdout, stderr):
    taskpack_dir = Path(taskpack_dir)
    written_required_files = [
        name
        for name in REQUIRED_TASKPACK_FILES
        if (taskpack_dir / name).is_file()
    ]
    missing_required_files = [
        name for name in REQUIRED_TASKPACK_FILES if name not in set(written_required_files)
    ]
    stdout_bytes = len(str(stdout or "").encode("utf-8"))
    stderr_bytes = len(str(stderr or "").encode("utf-8"))
    if stdout_bytes == 0 and stderr_bytes == 0:
        largest_stream = "none"
    elif stderr_bytes >= stdout_bytes:
        largest_stream = "stderr"
    else:
        largest_stream = "stdout"
    return {
        "required_file_count": len(REQUIRED_TASKPACK_FILES),
        "written_required_file_count": len(written_required_files),
        "written_required_files": written_required_files,
        "missing_required_files": missing_required_files,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "largest_stream": largest_stream,
        "next_action": (
            "rerun with author-direct constraints or inspect author_result/state "
            "before widening dogfood"
        ),
    }


def _codex_author_failure_message(prefix, *, result_path, state_path):
    try:
        result = _read_json(result_path)
    except Exception:
        result = {}
    diagnostic = result.get("diagnostic") if isinstance(result, dict) else {}
    if not isinstance(diagnostic, dict):
        diagnostic = {}
    written = diagnostic.get("written_required_file_count")
    required = diagnostic.get("required_file_count")
    missing = diagnostic.get("missing_required_files")
    largest_stream = diagnostic.get("largest_stream")
    next_action = diagnostic.get("next_action")
    details = [prefix]
    if written is not None and required is not None:
        details.append(f"required_files_written={written}/{required}")
    if missing:
        details.append(f"missing={','.join(str(item) for item in missing)}")
    if largest_stream:
        details.append(f"largest_stream={largest_stream}")
    if next_action:
        details.append(f"next_action={next_action}")
    details.append(f"result_path={result_path}")
    details.append(f"state_path={state_path}")
    return "; ".join(details)


def _roadmap_followup_template_prompt():
    return [
        "Roadmap-derived follow-up task template:",
        (
            "- Use this template when the goal references a roadmap, route note, "
            "queue-selected next_goal, source_report_path, previous taskpack, "
            "or goal_memory_path."
        ),
        (
            "- Each executable backlog item should name source_report_path or "
            "goal_memory_path in objective or goal_alignment and include concrete "
            "previous evidence: verification result, blocker, selected next_goal, "
            "or evidence path."
        ),
        (
            "- required_deliverables for roadmap-derived follow-ups should include "
            "repository_understanding_summary, previous_evidence_summary, "
            "roadmap_followup_route_template, evidence_paths, non_goals, "
            "success_metrics_or_no_metric_delta, "
            "implemented_changes_or_no_safe_change_rationale, verification_summary, "
            "and recommended_next_implementation_tasks."
        ),
        (
            "- State explicit non_goals in goal_alignment or required_deliverables "
            "when roadmap excludes broader work such as M68 model adapters, "
            "DB-primary storage, direct semantic authority edits, merge, push, "
            "or release activation."
        ),
        (
            "- source merge, push, and release activation remain operator review gates; "
            "workers may only prepare patches, reports, evidence, and integration baselines."
        ),
        (
            "- Prefer bounded code/test/repository tasks; documentation-only follow-ups "
            "must say why the route explicitly asks for docs or why no safe code change "
            "is justified."
        ),
    ]


def _apply_agentteam_target_taskpack_policy(taskpack):
    policy = taskpack.get("policy")
    if not isinstance(policy, dict):
        policy = {}
    policy["allow_merge"] = False
    policy["merge_requires_verified_integration"] = True
    policy["operator_review_required"] = True
    policy["source_control_restrictions"] = [
        "no_merge",
        "no_push",
        "no_release_activation",
    ]
    taskpack["policy"] = policy


def _apply_agentteam_target_task_policy(item):
    _append_text_field(
        item,
        "objective",
        "AgentTeam-as-target review gate: do not merge, push, or activate releases; operator review is required.",
    )
    _append_text_field(
        item,
        "goal_alignment",
        "AgentTeam-as-target work must preserve the operator requirement while leaving source merge, push, and release activation to operator review.",
    )
    deliverables = item.get("required_deliverables")
    if isinstance(deliverables, list) and "agentteam_target_review_gate" not in deliverables:
        deliverables.append("agentteam_target_review_gate")


def _append_text_field(item, field, addition):
    value = item.get(field)
    if not value:
        item[field] = addition
        return
    text = str(value)
    if addition not in text:
        item[field] = f"{text} {addition}"


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
    if isinstance(taskpack, dict):
        taskpack = _unwrap_nested_taskpack_object(taskpack)
    if isinstance(taskpack, dict):
        if not taskpack.get("semantic_contract_version"):
            taskpack["semantic_contract_version"] = TASKPACK_SEMANTIC_CONTRACT_VERSION
        if not taskpack.get("original_goal") and taskpack.get("goal"):
            taskpack["original_goal"] = taskpack["goal"]
        effective_goal = taskpack.get("original_goal") or taskpack.get("goal")
        classified_goal_kind = classify_goal_kind(effective_goal)
        declared_goal_kind = taskpack.get("goal_kind")
        if classified_goal_kind != "implementation" and declared_goal_kind != classified_goal_kind:
            goal_kind = classified_goal_kind
        else:
            goal_kind = declared_goal_kind or classified_goal_kind
        taskpack["goal_kind"] = goal_kind
        agentteam_target = _is_agentteam_target_project(taskpack.get("project_root"))
        if agentteam_target:
            _apply_agentteam_target_taskpack_policy(taskpack)
        _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    else:
        effective_goal = None
        goal_kind = "implementation"
        agentteam_target = False
    taskpack_data = taskpack if isinstance(taskpack, dict) else {}
    files = taskpack_data.get("files") if isinstance(taskpack_data.get("files"), dict) else {}

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
            if not item.get("work_type"):
                item["work_type"] = _default_work_type(goal_kind)
            if not item.get("goal_alignment"):
                item["goal_alignment"] = _default_goal_alignment(
                    taskpack_data.get("original_goal") or taskpack_data.get("goal") or item.get("objective")
                )
            if not item.get("required_deliverables"):
                item["required_deliverables"] = _default_required_deliverables(
                    taskpack_data.get("original_goal") or taskpack_data.get("goal") or item.get("objective")
                )
            elif goal_kind == "optimization" and isinstance(item.get("required_deliverables"), list):
                for deliverable in _default_required_deliverables(effective_goal):
                    if deliverable not in item["required_deliverables"]:
                        item["required_deliverables"].append(deliverable)
            if agentteam_target:
                _apply_agentteam_target_task_policy(item)
            if not item.get("backlog_status") and item.get("status"):
                item["backlog_status"] = item["status"]
            if "blockers" not in item:
                item["blockers"] = []
        _write_json(backlog_path, backlog)

    verification_path = taskpack_dir / files.get("verification", "verification.json")
    verification = _read_json(verification_path)
    verification_unwrapped = False
    if isinstance(verification, dict):
        unwrapped = _unwrap_nested_named_object(verification, "verification")
        verification_unwrapped = unwrapped != verification
        verification = unwrapped
    if isinstance(verification, dict):
        command = verification.get("command")
        project_root = taskpack_data.get("project_root")
        canonical_command = _canonical_verification_command(command, project_root)
        if canonical_command != command or verification_unwrapped:
            verification["command"] = canonical_command
            _write_json(verification_path, verification)


def _unwrap_nested_taskpack_object(taskpack):
    return _unwrap_nested_named_object(taskpack, "taskpack")


def _unwrap_nested_named_object(value, key):
    nested = value.get(key)
    if not isinstance(nested, dict):
        return value
    unwrapped = dict(nested)
    for outer_key, outer_value in value.items():
        if outer_key == key:
            continue
        if outer_key not in unwrapped:
            unwrapped[outer_key] = outer_value
    return unwrapped


def _apply_verification_profile_to_taskpack(taskpack_dir, verification_profile):
    if not verification_profile:
        return
    taskpack_dir = Path(taskpack_dir)
    profile = _normalize_taskpack_verification_profile(verification_profile)
    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    files = taskpack.get("files") if isinstance(taskpack.get("files"), dict) else {}
    verification_path = taskpack_dir / files.get("verification", "verification.json")
    verification = _read_json(verification_path)
    if not isinstance(verification, dict):
        return
    verification["verification_profile"] = profile
    verification["command"] = profile["correctness"]["command"]
    performance = profile.get("performance") if isinstance(profile.get("performance"), dict) else {}
    if performance.get("command") or performance.get("metrics"):
        verification["performance"] = performance
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
            return candidate
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

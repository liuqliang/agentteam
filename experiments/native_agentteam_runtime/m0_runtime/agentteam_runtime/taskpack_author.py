import json
import os
import re
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .model_invocation import (
    InvocationLifecycle,
    ModelInvocationCall,
    ModelInvocationError,
    ModelInvocationIntegrityError,
    is_supported_codex_command,
)
from .repo_grounding import build_repo_grounding
from .repo_map import build_repository_map
from .taskpack import (
    BROAD_FRAMEWORK_REQUIRED_DELIVERABLES,
    DEFAULT_WORKER_ROLE,
    OPTIMIZATION_CODE_WORK_TYPES,
    REPO_MAP_HANDOFF_PATH,
    REPO_MAP_ROLE,
    TASKPACK_SEMANTIC_CONTRACT_VERSION,
    TaskpackValidationError,
    auto_materialize_semantic_taskpack,
    classify_goal_kind,
    _default_goal_alignment,
    _default_required_deliverables,
    _default_work_type,
    _canonical_taskpack_verification_command,
    _is_broad_framework_goal,
    _is_long_running_followup_goal,
    _normalize_taskpack_verification_profile,
    _require_contained_path,
    _resolve_draft_taskpack_id,
    _write_scope_is_document_only,
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
AUTHOR_OUTPUT_EXCERPT_CHARS = 2000


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
    codex_model=None,
    author_invocation_context=None,
    systemd_runner_factory=None,
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
            codex_model=codex_model,
            author_invocation_context=author_invocation_context,
            systemd_runner_factory=systemd_runner_factory,
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
        project_root,
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


def _deterministic_author_verification_command(grounding, verification_profile, project_root=None):
    profile = _normalize_taskpack_verification_profile(
        verification_profile,
        project_root=project_root,
    )
    correctness = profile.get("correctness") if isinstance(profile.get("correctness"), dict) else {}
    command = correctness.get("command") if isinstance(correctness, dict) else None
    if isinstance(command, list) and command and all(isinstance(part, str) and part for part in command):
        return _canonical_taskpack_verification_command(command, project_root)
    for candidate in grounding.get("candidate_verification_commands", []):
        if not isinstance(candidate, dict):
            continue
        command = candidate.get("command")
        if isinstance(command, list) and command and all(isinstance(part, str) and part for part in command):
            return _canonical_taskpack_verification_command(command, project_root)
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
    codex_model=None,
    author_invocation_context=None,
    systemd_runner_factory=None,
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
    repo_grounding = build_repo_grounding(project_root)

    template_bundle_path = _write_author_template_bundle(
        author_context_dir=author_context_dir,
        taskpack_id=taskpack_id,
        project_root=project_root,
        goal=goal,
        verification_profile=verification_profile,
        repo_grounding=repo_grounding,
    )

    prompt = _author_prompt(
        project_root=project_root,
        goal=goal,
        taskpack_id=taskpack_id,
        taskpack_dir=taskpack_dir,
        author_context_dir=author_context_dir,
        repo_map=repo_map,
        repo_grounding=repo_grounding,
        verification_profile=verification_profile,
        template_bundle_path=template_bundle_path,
    )
    prompt_path = author_context_dir / "author_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    command = _codex_author_jsonl_command(
        _command_list(codex_command),
        model=codex_model,
    )
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
        model=codex_model,
        author_invocation_context=author_invocation_context,
        systemd_runner_factory=systemd_runner_factory,
    )
    if completed.returncode != -9:
        existing_result = _read_json(result_path)
        if not isinstance(existing_result, dict):
            existing_result = {}
        _write_json(
            result_path,
            {
                **existing_result,
                "status": "completed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
            },
        )

    _raise_if_target_repo_modified(project_root, repo_status_before)
    if completed.returncode == -9:
        salvage = _salvage_timed_out_codex_taskpack(
            taskpack_dir=taskpack_dir,
            verification_profile=verification_profile,
        )
        if salvage["accepted"]:
            _record_codex_author_salvage(result_path, state_path, salvage)
            return {
                "taskpack_dir": str(taskpack_dir),
                "taskpack_id": taskpack_id,
                "author_context_path": str(author_context_dir),
                "author_result_path": str(result_path),
                "author_timeout_salvaged": True,
                "author_salvage": salvage,
                "author_lifecycle": _author_lifecycle_result(result_path),
            }
        raise TaskpackValidationError(
            _codex_author_failure_message(
                "codex taskpack author timed out",
                result_path=result_path,
                state_path=state_path,
            )
        )
    if completed.returncode != 0:
        raise TaskpackValidationError(
            _codex_author_failure_message(
                f"codex taskpack author failed with exit code {completed.returncode}",
                result_path=result_path,
                state_path=state_path,
            )
        )

    _verify_required_taskpack_files(taskpack_dir)
    _canonicalize_codex_taskpack_files(taskpack_dir)
    _apply_verification_profile_to_taskpack(taskpack_dir, verification_profile)
    validate_taskpack(taskpack_dir)
    return {
        "taskpack_dir": str(taskpack_dir),
        "taskpack_id": taskpack_id,
        "author_context_path": str(author_context_dir),
        "author_result_path": str(result_path),
        "author_timeout_salvaged": False,
        "author_lifecycle": _author_lifecycle_result(result_path),
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
    model=None,
    author_invocation_context=None,
    systemd_runner_factory=None,
):
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    input_metrics = _codex_author_input_metrics(
        prompt=prompt,
        author_context_dir=author_context_dir,
    )
    supported = is_supported_codex_command(command)
    context = _author_model_invocation_context(
        taskpack_id=taskpack_id,
        draft_root=draft_root,
        model=model or _codex_command_model(command),
        supported=supported,
        supplied=author_invocation_context,
    )
    invocation = ModelInvocationCall(
        author_context_dir,
        context,
        supported=supported,
        systemd_runner_factory=systemd_runner_factory,
    )
    base_state = {
        "author_status": "running",
        "taskpack_id": taskpack_id,
        "pid": os.getpid(),
        "command": command,
        "started_at": started_at,
        "taskpack_dir": str(taskpack_dir),
        "author_context_dir": str(author_context_dir),
        "prompt_path": str(prompt_path),
        "result_path": str(result_path),
        "timeout_seconds": timeout_seconds,
        "input_metrics": input_metrics,
        "active_model_invocation_id": invocation.lifecycle.invocation_id,
        "model_invocation": invocation.lifecycle.summary(),
        "runtime_execution_session_id": context["runtime_execution_session_id"],
        "usage_stage": context["usage_stage"],
        "round_index": context.get("round_index"),
        "pursue_id": context.get("pursue_id"),
    }
    _write_author_state(
        state_path,
        base_state,
        started_monotonic,
        progress_callback,
    )

    try:
        execution = invocation.execute(
            command,
            cwd=draft_root,
            input_text=prompt,
            timeout_seconds=timeout_seconds,
            progress_callback=(
                lambda: _write_author_state(
                    state_path,
                    base_state,
                    started_monotonic,
                    progress_callback,
                )
            ),
            progress_interval_seconds=progress_interval_seconds,
        )
    except ModelInvocationError as exc:
        failure = {
            "status": "launch_failed",
            "exit_code": None,
            "timeout_seconds": timeout_seconds,
            "input_metrics": input_metrics,
            "output": {
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_excerpt": "",
                "stderr_excerpt": str(exc)[:AUTHOR_OUTPUT_EXCERPT_CHARS],
                "stdout_path": None,
                "stderr_path": None,
            },
            "diagnostic": _codex_author_diagnostic(taskpack_dir, "", str(exc)),
            "model_invocation": invocation.lifecycle.summary(),
        }
        _write_json(result_path, failure)
        _write_author_state(
            state_path,
            {
                **base_state,
                "author_status": "launch_failed",
                "launch_error": str(exc)[:AUTHOR_OUTPUT_EXCERPT_CHARS],
                "finished_at": _utc_now(),
            },
            started_monotonic,
            progress_callback,
        )
        raise TaskpackValidationError(
            f"codex taskpack author launch failed: {str(exc)[:500]}"
        ) from exc

    if execution.launch_failed:
        terminal_status = "launch_failed"
        status = "launch_failed"
        returncode = 127
    elif execution.timed_out:
        terminal_status = "timed_out"
        status = "timed_out"
        returncode = -9
    else:
        returncode = execution.returncode
        terminal_status = "completed" if returncode == 0 else "failed"
        status = terminal_status
    terminal = invocation.finalize(
        terminal_status,
        execution,
        terminal_writer="taskpack_author",
    )
    completed = subprocess.CompletedProcess(
        command,
        returncode,
        execution.stdout,
        execution.stderr,
    )
    output = _write_author_output_summary(
        author_context_dir,
        execution.stdout,
        execution.stderr,
    )
    diagnostic = _codex_author_diagnostic(
        taskpack_dir,
        execution.stdout,
        execution.stderr,
    )
    lifecycle_summary = {
        **invocation.lifecycle.summary(),
        "usage_event_id": terminal["usage_event_id"],
        "terminal_status": terminal["terminal_status"],
        "usage_status": terminal["usage_status"],
    }
    _write_json(
        result_path,
        {
            "status": status,
            "exit_code": returncode,
            "timeout_seconds": timeout_seconds,
            "input_metrics": input_metrics,
            "output": output,
            "diagnostic": diagnostic,
            "model_invocation": lifecycle_summary,
            "model_invocation_usage": terminal,
        },
    )
    final_state = {
        **base_state,
        "author_status": status,
        "exit_code": returncode,
        "stdout_bytes": output["stdout_bytes"],
        "stderr_bytes": output["stderr_bytes"],
        "output": output,
        "diagnostic": diagnostic,
        "model_invocation": lifecycle_summary,
        "finished_at": _utc_now(),
    }
    _write_author_state(
        state_path,
        final_state,
        started_monotonic,
        progress_callback,
    )
    return completed


def _codex_author_jsonl_command(command, *, model=None):
    command = list(command)
    if not is_supported_codex_command(command):
        return command
    if "--json" not in command:
        command.append("--json")
    if model and "-m" not in command and "--model" not in command:
        command.extend(["-m", str(model)])
    return command


def _codex_command_model(command):
    command = list(command)
    for flag in ("-m", "--model"):
        try:
            value = command[command.index(flag) + 1]
        except (ValueError, IndexError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _author_model_invocation_context(
    *,
    taskpack_id,
    draft_root,
    model,
    supported,
    supplied=None,
):
    supplied = dict(supplied or {})
    stage = supplied.get("usage_stage") or "taskpack_author"
    role = (
        "follow_up_author"
        if stage == "follow_up_author"
        else "taskpack_author"
    )
    runtime_session = (
        supplied.get("runtime_execution_session_id")
        or f"AUTHOR-SESSION-{uuid.uuid4().hex}"
    )
    owner = (
        supplied.get("lifecycle_owner_token")
        or f"AUTHOR-OWNER-{uuid.uuid4().hex}"
    )
    project = supplied.get("project") or Path(draft_root).parent.name or "agentteam"
    return {
        "project": str(project),
        "run_id": str(supplied.get("run_id") or taskpack_id),
        "pursue_id": supplied.get("pursue_id"),
        "round_index": supplied.get("round_index"),
        "taskpack_id": str(taskpack_id),
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": None,
        "attempt_id": supplied.get("attempt_id"),
        "runtime_execution_session_id": str(runtime_session),
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": str(owner),
        "agent_id": str(
            supplied.get("agent_id") or "taskpack-author-controller"
        ),
        "role": role,
        "usage_stage": stage,
        "backend": "codex",
        "model": model,
        "coverage_class": (
            "supported_model_invocation"
            if supported
            else "not_applicable_adapter"
        ),
        "provider_usage_scope": supplied.get("provider_usage_scope"),
        "experiment_sandbox_reference": supplied.get(
            "experiment_sandbox_reference"
        ),
        "experiment_sandbox_required": (
            supplied.get("experiment_sandbox_required") is True
        ),
        "experiment_authority_root": supplied.get(
            "experiment_authority_root"
        ),
    }


def _author_lifecycle_result(result_path):
    result = _read_json(result_path)
    lifecycle = result.get("model_invocation")
    return lifecycle if isinstance(lifecycle, dict) else None


def recover_open_author_invocations(
    author_context_dir,
    *,
    fence_assessor=None,
    service_stopper=None,
):
    """Reconcile dead author controllers without guessing from a numeric PID."""
    author_context_dir = Path(author_context_dir).resolve()
    invocation_root = author_context_dir / "model_invocations"
    if not invocation_root.exists():
        return []
    custom_fence_assessor = fence_assessor is not None
    stopped_service_assessor = None
    if fence_assessor is None or service_stopper is None:
        from .two_phase_scheduler import (
            _assess_persisted_execution_group,
            _assess_stopped_exact_service,
            _stop_exact_transient_service,
        )

        fence_assessor = fence_assessor or _assess_persisted_execution_group
        service_stopper = service_stopper or _stop_exact_transient_service
        stopped_service_assessor = _assess_stopped_exact_service

    results = []
    for started_path in sorted(invocation_root.glob("*/started.json")):
        terminal_path = started_path.with_name("terminal.json")
        start = _read_json(started_path)
        if terminal_path.exists():
            terminal = _read_json(terminal_path)
            results.append(
                {
                    "reconciliation_status": "terminal_available",
                    "invocation_id": start["invocation_id"],
                    "usage_event_id": terminal.get("usage_event_id"),
                    "terminal_path": str(terminal_path),
                }
            )
            continue
        assessment = fence_assessor(dict(start))
        status = assessment.get("fence_status")
        pidfd = assessment.get("pidfd")
        if status == "live_pinned":
            if isinstance(pidfd, int):
                os.close(pidfd)
            results.append(
                {
                    "reconciliation_status": "live",
                    "invocation_id": start["invocation_id"],
                    "proof": assessment.get("proof"),
                }
            )
            continue
        if status == "exact_service_stop_required":
            if not service_stopper(dict(start)):
                results.append(
                    {
                        "reconciliation_status": "service_stop_required",
                        "invocation_id": start["invocation_id"],
                        "proof": assessment.get("proof"),
                    }
                )
                continue
            if terminal_path.exists():
                terminal = _read_json(terminal_path)
                results.append(
                    {
                        "reconciliation_status": "terminal_available",
                        "invocation_id": start["invocation_id"],
                        "usage_event_id": terminal.get("usage_event_id"),
                        "terminal_path": str(terminal_path),
                    }
                )
                continue
            assessment = (
                fence_assessor(dict(start))
                if custom_fence_assessor
                else stopped_service_assessor(dict(start))
            )
            status = assessment.get("fence_status")
        if status != "death_proven":
            results.append(
                {
                    "reconciliation_status": "open_ambiguous",
                    "invocation_id": start["invocation_id"],
                    "proof": assessment.get("proof"),
                }
            )
            continue

        lifecycle = _attach_author_lifecycle(started_path, start)
        old_owner = start["lifecycle_owner_token"]
        lifecycle.context["lifecycle_owner_token"] = (
            f"AUTHOR-RECOVERY-{start['invocation_id']}"
        )
        revocation = lifecycle.revoke_writer(
            old_owner,
            revoked_by="author-recovery-controller",
            reason="author_process_death_confirmed",
        )
        supervisor_result = _read_json_if_exists(
            started_path.with_name("supervisor-result.json")
        )
        stdout = (
            supervisor_result.get("stdout", "")
            if isinstance(supervisor_result, dict)
            else ""
        )
        stderr = (
            supervisor_result.get("stderr", "")
            if isinstance(supervisor_result, dict)
            else ""
        )
        try:
            terminal = lifecycle.finalize(
                "recovered_orphan",
                stdout=stdout,
                stderr=stderr,
                terminal_writer="recovery_controller",
            )
        except ModelInvocationIntegrityError:
            terminal = _read_json_if_exists(terminal_path)
            if terminal is None:
                raise
        results.append(
            {
                "reconciliation_status": "recovered",
                "invocation_id": start["invocation_id"],
                "usage_event_id": terminal.get("usage_event_id"),
                "terminal_path": str(terminal_path),
                "proof": assessment.get("proof"),
                "writer_revocation": revocation,
            }
        )
    return results


def _attach_author_lifecycle(started_path, start):
    lifecycle = object.__new__(InvocationLifecycle)
    lifecycle.authority_root = Path(started_path).parents[2]
    lifecycle.invocation_id = start["invocation_id"]
    lifecycle.context = dict(start)
    lifecycle.started_at = start["started_at"]
    lifecycle.invocation_dir = Path(started_path).parent
    lifecycle.started_path = Path(started_path)
    lifecycle.revoked_path = lifecycle.invocation_dir / "revoked.json"
    lifecycle.terminal_path = lifecycle.invocation_dir / "terminal.json"
    lifecycle.terminal_lock_path = lifecycle.invocation_dir / "terminal.lock"
    lifecycle.stdout_path = lifecycle.invocation_dir / "stdout.jsonl"
    lifecycle.stderr_path = lifecycle.invocation_dir / "stderr.log"
    return lifecycle


def _read_json_if_exists(path):
    try:
        return _read_json(path)
    except FileNotFoundError:
        return None


def _codex_author_input_metrics(*, prompt, author_context_dir):
    prompt_text = str(prompt or "")
    prompt_bytes = len(prompt_text.encode("utf-8"))
    author_context_bytes = 0
    author_context_file_count = 0
    for path in sorted(Path(author_context_dir).rglob("*")):
        if not path.is_file():
            continue
        try:
            author_context_bytes += path.stat().st_size
        except OSError:
            continue
        author_context_file_count += 1
    return {
        "metrics_schema_version": "codex_author_input_metrics.v1",
        "prompt_chars": len(prompt_text),
        "prompt_bytes": prompt_bytes,
        "prompt_line_count": len(prompt_text.splitlines()),
        "prompt_estimated_tokens": _estimated_tokens_from_count(len(prompt_text)),
        "author_context_file_count": author_context_file_count,
        "author_context_bytes": author_context_bytes,
        "author_context_estimated_tokens": _estimated_tokens_from_count(author_context_bytes),
        "estimation_method": "ceil(count/4)",
    }


def _estimated_tokens_from_count(count):
    count = max(int(count or 0), 0)
    if count <= 0:
        return 0
    return (count + 3) // 4


def _salvage_timed_out_codex_taskpack(*, taskpack_dir, verification_profile=None):
    try:
        _verify_required_taskpack_files(taskpack_dir)
        _canonicalize_codex_taskpack_files(taskpack_dir)
        _apply_verification_profile_to_taskpack(taskpack_dir, verification_profile)
        validation = validate_taskpack(taskpack_dir)
    except Exception as exc:
        return {
            "accepted": False,
            "reason": "validation_failed_after_timeout",
            "error_class": exc.__class__.__name__,
            "error_summary": str(exc),
        }
    return {
        "accepted": True,
        "reason": "complete_valid_taskpack_written_before_timeout",
        "validation": validation,
    }


def _record_codex_author_salvage(result_path, state_path, salvage):
    result = _read_json(result_path)
    if not isinstance(result, dict):
        result = {}
    result["status"] = "accepted_after_timeout"
    result["salvage"] = salvage
    _write_json(result_path, result)

    state = _read_json(state_path)
    if not isinstance(state, dict):
        state = {}
    state["author_status"] = "accepted_after_timeout"
    state["salvage"] = salvage
    state["updated_at"] = _utc_now()
    _write_json(state_path, state)


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


def _write_author_output_summary(author_context_dir, stdout, stderr):
    author_context_dir = Path(author_context_dir)
    stdout_text = str(stdout or "")
    stderr_text = str(stderr or "")
    stdout_path = author_context_dir / "author_stdout.log"
    stderr_path = author_context_dir / "author_stderr.log"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    return {
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_bytes": len(stdout_text.encode("utf-8")),
        "stderr_bytes": len(stderr_text.encode("utf-8")),
        "stdout_excerpt": _bounded_author_output_excerpt(stdout_text),
        "stderr_excerpt": _bounded_author_output_excerpt(stderr_text),
    }


def _bounded_author_output_excerpt(text, limit=AUTHOR_OUTPUT_EXCERPT_CHARS):
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker = "[truncated output]\n"
    tail_limit = max(limit - len(marker), 0)
    omitted = len(text) - tail_limit
    marker = f"[truncated {omitted} chars]\n"
    tail_limit = max(limit - len(marker), 0)
    omitted = len(text) - tail_limit
    marker = f"[truncated {omitted} chars]\n"
    return marker + text[-tail_limit:]


def _utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_author_template_bundle(
    *,
    author_context_dir,
    taskpack_id,
    project_root,
    goal,
    verification_profile=None,
    repo_grounding=None,
):
    author_context_dir = Path(author_context_dir)
    project_root = Path(project_root).resolve()
    goal_kind = classify_goal_kind(goal)
    task_id = f"TASK-{taskpack_id.upper().replace('-', '_')}-001"
    repo_map_task_id = f"TASK-{taskpack_id.upper().replace('-', '_')}-REPO-MAP"
    role_routed = goal_kind in {"implementation", "optimization"}
    profile = _normalize_taskpack_verification_profile(
        verification_profile,
        project_root=project_root,
    )
    verification_command = profile["correctness"]["command"]
    agents = []
    role_runtime_profiles = {DEFAULT_WORKER_ROLE: {"adapter": "codex"}}
    items = []
    if role_routed:
        role_runtime_profiles[REPO_MAP_ROLE] = {"adapter": "codex"}
        agents.append(
            {
                "agent_id": "agent-repo-map-1",
                "role": REPO_MAP_ROLE,
                "status": "idle",
                "inbox_path": "mailboxes/agent-repo-map-1/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map-1/outbox.jsonl",
            }
        )
        items.append(
            {
                "task_id": repo_map_task_id,
                "objective": "map_repository_and_write_repo_map_handoff",
                "goal_alignment": _default_goal_alignment(goal),
                "work_type": "repository_mapping",
                "required_deliverables": [
                    "repository_understanding_summary",
                    "relevant_files_and_entry_points",
                    "repo_map_handoff",
                    "verification_candidates",
                    "implementation_risks",
                ],
                "read_scope": ["replace_with_narrow_read_scope"],
                "write_scope": [".agentteam/generated/"],
                "expected_output_artifacts": [REPO_MAP_HANDOFF_PATH],
                "required_role": REPO_MAP_ROLE,
                "backlog_status": "ready",
                "risk_target": "L0",
                "depends_on": [],
                "blockers": [],
            }
        )
    agents.append(
        {
            "agent_id": "agent-implementation-worker-1",
            "role": DEFAULT_WORKER_ROLE,
            "status": "idle",
            "inbox_path": "mailboxes/agent-implementation-worker-1/inbox.jsonl",
            "outbox_path": "mailboxes/agent-implementation-worker-1/outbox.jsonl",
        }
    )
    implementation_deliverables = list(_default_required_deliverables(goal))
    if role_routed and "repo_map_handoff" not in implementation_deliverables:
        implementation_deliverables.insert(0, "repo_map_handoff")
    items.append(
        {
            "task_id": task_id,
            "objective": "replace_with_bounded_executable_objective",
            "goal_alignment": _default_goal_alignment(goal),
            "work_type": _default_work_type(goal_kind),
            "required_deliverables": implementation_deliverables,
            "read_scope": ["replace_with_narrow_read_scope"],
            "write_scope": ["replace_with_narrow_write_scope"],
            "required_role": DEFAULT_WORKER_ROLE,
            "input_artifacts": [REPO_MAP_HANDOFF_PATH] if role_routed else [],
            "backlog_status": "ready",
            "risk_target": "L2" if role_routed else "L1",
            "depends_on": [repo_map_task_id] if role_routed else [],
            "blockers": [],
        }
    )

    templates = {
        "taskpack.yaml": {
            "taskpack_schema_version": "taskpack.v1",
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
                    "timeout_seconds": "replace_with_codex_timeout_seconds",
                },
            },
            "policy": {
                "allow_merge": False,
                "operator_review_required": True,
            },
            "files": {
                "agent_pool": "agent_pool.json",
                "backlog": "backlog.json",
                "verification": "verification.json",
            },
        },
        "agent_pool.json": {
            "scheduler_agent_id": "agent-scheduler",
            "agents": agents,
            "role_runtime_profiles": role_runtime_profiles,
        },
        "backlog.json": {
            "backlog_id": f"BL-{taskpack_id}",
            "items": items,
        },
        "verification.json": {
            "verification_schema_version": "taskpack_verification.v1",
            "command": verification_command,
            "verification_profile": profile,
            "success_criteria": [
                "verification command exits with code 0",
                "runtime validation accepts changed files inside declared write_scope",
            ],
        },
        "README.md": (
            "# Taskpack draft\n\n"
            "Replace this template with a concise summary of the taskpack goal, "
            "scopes, and verification command.\n"
        ),
    }
    path = author_context_dir / "required_file_templates.json"
    _write_json(
        path,
        {
            "template_schema_version": "agentteam_author_required_file_templates.v1",
            "taskpack_id": taskpack_id,
            "required_files": list(REQUIRED_TASKPACK_FILES),
            "instruction": (
                "Use these templates as structural scaffolds only; replace placeholder "
                "values with task-specific content before writing files into taskpack_dir."
            ),
            "repo_grounding_context": _compact_repo_grounding_context(repo_grounding),
            "templates": templates,
        },
    )
    return path


def _compact_repo_grounding_context(repo_grounding):
    if not isinstance(repo_grounding, dict):
        return {}
    structure = (
        repo_grounding.get("repository_structure")
        if isinstance(repo_grounding.get("repository_structure"), dict)
        else {}
    )
    budget = (
        structure.get("top_level_entry_budget")
        if isinstance(structure.get("top_level_entry_budget"), dict)
        else {}
    )
    return {
        "repo_grounding_schema_version": repo_grounding.get("grounding_schema_version")
        or "repo_grounding.v1",
        "repo_grounding_scan_status": repo_grounding.get("scan_status") or "unknown",
        "repo_grounding_tracked_file_count": repo_grounding.get("tracked_file_count", 0),
        "repo_grounding_languages": [
            {
                "language": item.get("language"),
                "file_count": item.get("file_count"),
            }
            for item in (repo_grounding.get("languages") or [])[:8]
            if isinstance(item, dict) and item.get("language")
        ],
        "repo_grounding_project_tools": [
            {
                "tool_id": item.get("tool_id"),
                "tool_type": item.get("tool_type"),
                "path": item.get("path"),
            }
            for item in (repo_grounding.get("project_tools") or [])[:8]
            if isinstance(item, dict) and item.get("tool_id") and item.get("path")
        ],
        "repo_grounding_test_entrypoints": [
            {
                "path": item.get("path"),
                "language": item.get("language"),
                "test_framework_hint": item.get("test_framework_hint"),
            }
            for item in (repo_grounding.get("test_entrypoints") or [])[:12]
            if isinstance(item, dict) and item.get("path")
        ],
        "repo_grounding_candidate_verification_commands": [
            {
                "command": item.get("command"),
                "reason": item.get("reason") or "deterministic repo grounding",
            }
            for item in (repo_grounding.get("candidate_verification_commands") or [])[:8]
            if isinstance(item, dict) and item.get("command")
        ],
        "repo_structure_schema_version": structure.get("structure_schema_version")
        or "repo_structure.v1",
        "repo_structure_top_level_entries": [
            {
                "path": item.get("path"),
                "entry_type": item.get("entry_type"),
                "file_count": item.get("file_count"),
            }
            for item in (structure.get("top_level_entries") or [])[:12]
            if isinstance(item, dict) and item.get("path")
        ],
        "repo_structure_budget": {
            "max_entries": budget.get("max_entries", 0),
            "total_entry_count": budget.get("total_entry_count", 0),
            "included_count": budget.get("included_count", 0),
            "omitted_count": budget.get("omitted_count", 0),
        },
    }


def _author_prompt(
    project_root,
    goal,
    taskpack_id,
    taskpack_dir,
    author_context_dir,
    repo_map,
    repo_grounding=None,
    verification_profile=None,
    template_bundle_path=None,
):
    repo_paths = repo_map["paths"]
    repo_grounding_context = _compact_repo_grounding_context(repo_grounding)
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
        *_author_template_bundle_prompt(template_bundle_path),
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
        *(
            [
                "Compact repo_grounding.v1 author context:",
                (
                    "- required_file_templates.json repo_grounding_context carries "
                    "language, tool, test-entrypoint, and candidate verification-command signals."
                ),
                (
                    "- Use repo_grounding_context to choose narrow read_scope, write_scope, "
                    "and verification guidance before broad source exploration."
                ),
                "",
            ]
            if repo_grounding_context
            else []
        ),
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
        (
            "- risk_target routing is mechanical: treat missing or unclear risk_target as L2; "
            "risk_target in L0 or L1 means route directly to implementation_worker without "
            "repo_map_agent, repo_map_handoff, or depends_on on a repo_map task"
        ),
        (
            "- risk_target L2 means the implementation_worker item must consume "
            "repo_map_handoff, either from a repository_mapping item with required_role "
            "repo_map_agent or from a reused integration baseline artifact"
        ),
        (
            "- risk_target L3 means do not dispatch a normal implementation_worker item; "
            "mark semantic_authoring_required with a semantic_authoring_required blocker "
            "so semantic or architecture review happens before implementation"
        ),
        f"- use {REPO_MAP_HANDOFF_PATH} as the repo_map_handoff artifact path",
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
        (
            "- broad framework enhancement goals must produce measurable code-facing or "
            "evidence-backed work, not tiny documentation-only tasks unless the original "
            "goal explicitly asks for documentation"
        ),
        (
            "- broad framework enhancement goals required_deliverables must include "
            f"{', '.join(BROAD_FRAMEWORK_REQUIRED_DELIVERABLES)}"
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
            "non_goals, implemented_changes_or_no_safe_change_rationale, "
            "metric_delta_or_no_safe_change_evidence, verification_summary, review_gate, "
            "and recommended_next_implementation_tasks"
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


def _author_template_bundle_prompt(template_bundle_path):
    if not template_bundle_path:
        return []
    return [
        "Required file template bundle:",
        f"- {template_bundle_path}",
        (
            "- Open this bundle first and use it as a structural scaffold for the "
            "five required files."
        ),
        (
            "- Do not copy placeholder values blindly; replace placeholder values "
            "with task-specific objective, scope, evidence, and verification content."
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
            "candidate_changes_or_no_safe_change_rationale, "
            "implemented_changes_or_no_safe_change_rationale, verification_summary, "
            "review_gate, and recommended_next_implementation_tasks."
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


def _apply_optimization_code_task_policy(item):
    _append_text_field(
        item,
        "goal_alignment",
        (
            "Optimization investigation must preserve optimization intent by recording "
            "baseline/current behavior, profiling or candidate matrix, metric/benchmark "
            "measurement, verification evidence, and a no-safe-change rationale when no "
            "code change is justified."
        ),
    )


def _append_text_field(item, field, addition):
    value = item.get(field)
    if not value:
        item[field] = addition
        return
    text = str(value)
    if addition not in text:
        item[field] = f"{text} {addition}"


def _canonicalize_risk_target_repo_map_routing(
    taskpack_data,
    agent_pool,
    backlog,
    *,
    goal_kind,
    effective_goal,
):
    if goal_kind not in {"implementation", "optimization"}:
        return
    if not isinstance(agent_pool, dict) or not isinstance(backlog, dict):
        return
    items = backlog.get("items")
    if not isinstance(items, list):
        return
    repo_map_item = _first_repo_map_item(items)
    for item in items:
        if not _is_implementation_worker_item(item):
            continue
        risk_target = _canonical_risk_target(item)
        item["risk_target"] = risk_target
        if risk_target in {"L0", "L1"}:
            _remove_repo_map_handoff_dependency(item)
            continue
        if risk_target == "L2":
            repo_map_item = repo_map_item or _insert_repo_map_item(
                items,
                taskpack_data,
                effective_goal,
                read_scope=item.get("read_scope"),
            )
            _ensure_repo_map_agent(agent_pool)
            _ensure_string_list_contains(item, "input_artifacts", REPO_MAP_HANDOFF_PATH)
            _ensure_string_list_contains(item, "depends_on", repo_map_item["task_id"])
            _ensure_string_list_contains(item, "required_deliverables", "repo_map_handoff")
            continue
        if risk_target == "L3":
            item["semantic_authoring_required"] = True
            _ensure_string_list_contains(item, "blockers", "semantic_authoring_required")
            item["backlog_status"] = "blocked"


def _is_implementation_worker_item(item):
    return (
        isinstance(item, dict)
        and item.get("required_role") == DEFAULT_WORKER_ROLE
        and not _is_repo_map_author_item(item)
    )


def _is_repo_map_author_item(item):
    if not isinstance(item, dict):
        return False
    expected = item.get("expected_output_artifacts")
    if not isinstance(expected, list):
        expected = []
    return (
        item.get("required_role") == REPO_MAP_ROLE
        or item.get("work_type") == "repository_mapping"
        or REPO_MAP_HANDOFF_PATH in expected
    )


def _canonical_risk_target(item):
    value = item.get("risk_target")
    if isinstance(value, str) and value.strip() in {"L0", "L1", "L2", "L3"}:
        return value.strip()
    return "L2"


def _first_repo_map_item(items):
    for item in items:
        if _is_repo_map_author_item(item):
            if not item.get("task_id"):
                item["task_id"] = "TASK-REPO-MAP"
            return item
    return None


def _insert_repo_map_item(items, taskpack_data, effective_goal, read_scope=None):
    taskpack_id = taskpack_data.get("taskpack_id") if isinstance(taskpack_data, dict) else None
    repo_map_task_id = _unique_repo_map_task_id(items, taskpack_id)
    goal = effective_goal or taskpack_data.get("goal") or "repository task"
    item = {
        "task_id": repo_map_task_id,
        "objective": "Map repository structure and write repo_map_handoff for the downstream L2 task.",
        "goal_alignment": _default_goal_alignment(goal),
        "work_type": "repository_mapping",
        "required_deliverables": [
            "repository_understanding_summary",
            "relevant_files_and_entry_points",
            "repo_map_handoff",
            "verification_candidates",
            "implementation_risks",
        ],
        "read_scope": _repo_map_read_scope(read_scope),
        "write_scope": [".agentteam/generated/"],
        "expected_output_artifacts": [REPO_MAP_HANDOFF_PATH],
        "required_role": REPO_MAP_ROLE,
        "backlog_status": "ready",
        "risk_target": "L0",
        "depends_on": [],
        "blockers": [],
    }
    items.insert(0, item)
    return item


def _unique_repo_map_task_id(items, taskpack_id):
    base = str(taskpack_id or "taskpack").upper()
    base = re.sub(r"[^A-Z0-9]+", "_", base).strip("_") or "TASKPACK"
    candidate = f"TASK-{base}-REPO-MAP"
    existing = {
        item.get("task_id")
        for item in items
        if isinstance(item, dict)
    }
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate}-{index}" in existing:
        index += 1
    return f"{candidate}-{index}"


def _repo_map_read_scope(read_scope):
    if isinstance(read_scope, list) and read_scope and all(isinstance(item, str) for item in read_scope):
        return list(read_scope)
    return ["."]


def _ensure_repo_map_agent(agent_pool):
    agents = agent_pool.get("agents")
    if not isinstance(agents, list):
        agent_pool["agents"] = []
        agents = agent_pool["agents"]
    for agent in agents:
        if isinstance(agent, dict) and agent.get("role") == REPO_MAP_ROLE:
            return
    agents.insert(
        0,
        {
            "agent_id": "agent-repo-map-1",
            "role": REPO_MAP_ROLE,
            "status": "idle",
            "inbox_path": "mailboxes/agent-repo-map-1/inbox.jsonl",
            "outbox_path": "mailboxes/agent-repo-map-1/outbox.jsonl",
        },
    )
    role_runtime_profiles = agent_pool.get("role_runtime_profiles")
    if isinstance(role_runtime_profiles, dict) and REPO_MAP_ROLE not in role_runtime_profiles:
        role_runtime_profiles[REPO_MAP_ROLE] = {"adapter": "codex"}


def _remove_repo_map_handoff_dependency(item):
    input_artifacts = item.get("input_artifacts")
    if isinstance(input_artifacts, list):
        item["input_artifacts"] = [
            artifact for artifact in input_artifacts if artifact != REPO_MAP_HANDOFF_PATH
        ]
        if not item["input_artifacts"]:
            item.pop("input_artifacts", None)
    depends_on = item.get("depends_on")
    if isinstance(depends_on, list):
        item["depends_on"] = [
            dependency
            for dependency in depends_on
            if "REPO-MAP" not in str(dependency).upper()
        ]


def _ensure_string_list_contains(item, key, value):
    current = item.get(key)
    if not isinstance(current, list):
        current = []
    if value not in current:
        current.append(value)
    item[key] = current


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
        quality_gate_goal = (
            goal_kind == "optimization"
            or _is_long_running_followup_goal(effective_goal)
            or _is_broad_framework_goal(effective_goal)
        )
        for item in backlog["items"]:
            if not isinstance(item, dict):
                continue
            if not item.get("task_id") and item.get("item_id"):
                item["task_id"] = item["item_id"]
            if not item.get("objective") and item.get("title"):
                item["objective"] = item["title"]
            if not item.get("work_type"):
                item["work_type"] = _default_work_type(goal_kind)
            elif (
                quality_gate_goal
                and item.get("work_type") == "audit"
                and not _write_scope_is_document_only(item.get("write_scope") or [])
            ):
                item["work_type"] = "code_investigation"
            if not item.get("goal_alignment"):
                item["goal_alignment"] = _default_goal_alignment(
                    taskpack_data.get("original_goal") or taskpack_data.get("goal") or item.get("objective")
                )
            if (
                goal_kind == "optimization"
                and item.get("work_type") in OPTIMIZATION_CODE_WORK_TYPES
                and not _write_scope_is_document_only(item.get("write_scope") or [])
            ):
                _apply_optimization_code_task_policy(item)
            if not item.get("required_deliverables"):
                item["required_deliverables"] = _default_required_deliverables(
                    taskpack_data.get("original_goal") or taskpack_data.get("goal") or item.get("objective")
                )
            elif quality_gate_goal and isinstance(item.get("required_deliverables"), list):
                for deliverable in _default_required_deliverables(effective_goal):
                    if deliverable not in item["required_deliverables"]:
                        item["required_deliverables"].append(deliverable)
            if agentteam_target:
                _apply_agentteam_target_task_policy(item)
            if not item.get("backlog_status") and item.get("status"):
                item["backlog_status"] = item["status"]
            if "blockers" not in item:
                item["blockers"] = []
        _canonicalize_risk_target_repo_map_routing(
            taskpack_data,
            agent_pool,
            backlog,
            goal_kind=goal_kind,
            effective_goal=effective_goal,
        )
        if isinstance(agent_pool, dict):
            _write_json(agent_pool_path, agent_pool)
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
    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    project_root = taskpack.get("project_root") if isinstance(taskpack, dict) else None
    profile = _normalize_taskpack_verification_profile(
        verification_profile,
        project_root=project_root,
    )
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
    return _canonical_taskpack_verification_command(command, project_root)


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

import json
import re
import sys
import uuid
from pathlib import Path

from .model_invocation import (
    ModelInvocationCall,
    ModelInvocationError,
    import_registered_controller_lifecycles,
    is_supported_codex_command,
)


DEFAULT_TOPIC = "runtime-diagnostic"
DEFAULT_CODEX_TIMEOUT_SECONDS = 600
_DEFAULT_TEXT_LIMIT = 2500
_FAILURE_TEST_PATTERNS = [
    re.compile(r"^(?:FAIL|ERROR):\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.MULTILINE),
    re.compile(r"^([A-Za-z_][A-Za-z0-9_\.]*)\s+\([^)]*\)\s+\.\.\.\s+(?:FAIL|ERROR)$", re.MULTILINE),
]


def build_runtime_diagnostic_context(run_dir, topic=None, text_limit=_DEFAULT_TEXT_LIMIT):
    run_dir = Path(run_dir).resolve()
    import_registered_controller_lifecycles(run_dir)
    events = _read_jsonl(run_dir / "events.jsonl")
    integration_queue = _read_json_if_exists(run_dir / "state" / "integration_queue.json")
    state = _read_json_if_exists(run_dir / "state" / "two_phase_scheduler_state.json")
    if not state:
        state = _read_json_if_exists(run_dir / "state" / "scheduler_state.json")
    worker_results = _worker_results(run_dir)
    integration_items = _integration_items(integration_queue)
    latest_failure = _latest_failure(events, integration_items, text_limit=text_limit)
    backlog_tasks = _backlog_tasks(run_dir)
    context = {
        "chat_status": "context_ready",
        "agent_role": "runtime_diagnostic_agent",
        "topic": topic or DEFAULT_TOPIC,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "scheduler_status": state.get("scheduler_status") if isinstance(state, dict) else None,
        "event_count": len(events),
        "latest_event_type": events[-1].get("event_type") if events else None,
        "latest_failure": latest_failure,
        "integration_items": integration_items,
        "worker_results": worker_results,
        "backlog_tasks": backlog_tasks,
    }
    controller_reference = _containing_experiment_controller_reference(
        run_dir
    )
    if controller_reference is not None:
        context.update(
            {
                "experiment_controller_reference": controller_reference,
                "experiment_authority_root": controller_reference[
                    "controller_root"
                ],
                "experiment_controller_required": True,
            }
        )
    context["prompt"] = render_runtime_diagnostic_context(context)
    return context


def render_runtime_diagnostic_context(context):
    lines = [
        "AgentTeam runtime_diagnostic_agent context",
        "",
        "Read-only role: explain run evidence, diagnose failures, and recommend next scheduler actions.",
        "Do not edit repository files, merge patches, or change runtime state from this diagnostic context.",
        "",
        f"Topic: {context.get('topic') or DEFAULT_TOPIC}",
        f"Run: {context.get('run_id') or 'unknown'}",
        f"Run dir: {context.get('run_dir') or 'unknown'}",
        f"Scheduler status: {context.get('scheduler_status') or 'unknown'}",
        f"Latest event: {context.get('latest_event_type') or 'unknown'}",
    ]
    failure = context.get("latest_failure") or {}
    if failure:
        lines.extend(
            [
                "",
                "Latest failure:",
                f"- task_id: {failure.get('task_id') or 'unknown'}",
                f"- attempt_id: {failure.get('attempt_id') or 'unknown'}",
                f"- event_type: {failure.get('event_type') or 'unknown'}",
                f"- failed_test: {failure.get('failed_test') or 'unknown'}",
                f"- status: {failure.get('status') or 'unknown'}",
            ]
        )
        stderr_excerpt = failure.get("stderr_excerpt")
        if stderr_excerpt:
            lines.extend(["- stderr_excerpt:", _indent(stderr_excerpt)])
    integration_items = context.get("integration_items") or []
    if integration_items:
        lines.extend(["", "Integration queue:"])
        for item in integration_items[:5]:
            lines.append(
                "- "
                f"task={item.get('task_id') or 'unknown'} "
                f"attempt={item.get('attempt_id') or 'unknown'} "
                f"queue={item.get('queue_status') or 'unknown'} "
                f"integration={item.get('integration_status') or 'unknown'} "
                f"verification={item.get('integration_verification_status') or 'unknown'}"
            )
            if item.get("integration_worktree_path"):
                lines.append(f"  worktree={item['integration_worktree_path']}")
    worker_results = context.get("worker_results") or []
    if worker_results:
        lines.extend(["", "Worker results:"])
        for result in worker_results[:5]:
            changed = result.get("changed_files") or []
            lines.append(
                "- "
                f"attempt={result.get('attempt_id') or 'unknown'} "
                f"status={result.get('result_status') or 'unknown'} "
                f"changed_files={', '.join(changed) if changed else 'none'}"
            )
            summary = result.get("operator_summary")
            if summary:
                lines.append(f"  operator_summary={_compact_json(summary, 500)}")
    backlog_tasks = context.get("backlog_tasks") or []
    if backlog_tasks:
        lines.extend(["", "Backlog tasks:"])
        for task in backlog_tasks[:5]:
            lines.append(
                "- "
                f"task={task.get('task_id') or 'unknown'} "
                f"title={task.get('title') or task.get('summary') or 'unknown'}"
            )
            if task.get("objective"):
                lines.append(f"  objective={task['objective']}")
    lines.extend(
        [
            "",
            "Recommended diagnostic questions:",
            "- What exact evidence explains the failure?",
            "- Which parts of the worker patch are safe to keep?",
            "- Should the scheduler reject, repair, or split the task?",
            "- Does this require an implementation repair task or a design escalation?",
        ]
    )
    return "\n".join(lines) + "\n"


def run_runtime_diagnostic_chat(
    context,
    codex_command=None,
    model=None,
    timeout_seconds=DEFAULT_CODEX_TIMEOUT_SECONDS,
):
    context = dict(context)
    command = _interactive_codex_command(
        context,
        codex_command=codex_command,
        model=model,
    )
    run_dir = Path(context["run_dir"]).resolve()
    controller_reference = _containing_experiment_controller_reference(
        run_dir
    )
    if controller_reference is not None:
        context.update(
            {
                "experiment_controller_reference": controller_reference,
                "experiment_authority_root": controller_reference[
                    "controller_root"
                ],
                "experiment_controller_required": True,
            }
        )
    runtime_session_id = f"DIAGNOSTIC-SESSION-{uuid.uuid4().hex}"
    lifecycle_owner_token = f"DIAGNOSTIC-OWNER-{uuid.uuid4().hex}"
    authority_root = (
        run_dir
        / "state"
        / "controller_invocations"
        / "runtime_diagnostic"
        / runtime_session_id
    )
    authority_root.mkdir(parents=True, exist_ok=False)
    claim = {
        "claim_schema_version": "model_invocation_controller_claim.v1",
        "project": context.get("project") or "agentteam",
        "run_id": context.get("run_id") or run_dir.name,
        "taskpack_id": context.get("taskpack_id") or run_dir.name,
        "usage_stage": "runtime_diagnostic",
        "runtime_execution_session_id": runtime_session_id,
        "lifecycle_owner_token": lifecycle_owner_token,
        "authority_root": str(authority_root),
    }
    _write_exclusive_json(authority_root / "controller_claim.json", claim)
    supported = is_supported_codex_command(command)
    invocation_context = {
        "project": claim["project"],
        "run_id": claim["run_id"],
        "pursue_id": context.get("pursue_id"),
        "round_index": context.get("round_index"),
        "taskpack_id": claim["taskpack_id"],
        "implementation_run_id": None,
        "gate_epoch": None,
        "task_id": None,
        "attempt_id": None,
        "runtime_execution_session_id": runtime_session_id,
        "requested_provider_session_id": None,
        "provider_resume_mode": "new",
        "provider_predecessor_invocation_id": None,
        "provider_predecessor_turn_id": None,
        "provider_predecessor_usage_snapshot": None,
        "lifecycle_owner_token": lifecycle_owner_token,
        "agent_id": "runtime-diagnostic-controller",
        "role": "runtime_diagnostic_agent",
        "usage_stage": "runtime_diagnostic",
        "backend": "codex",
        "model": model,
        "coverage_class": (
            "supported_model_invocation"
            if supported
            else "not_applicable_adapter"
        ),
        "provider_usage_scope": None,
        "experiment_controller_reference": context.get(
            "experiment_controller_reference"
        ),
        "experiment_authority_root": context.get(
            "experiment_authority_root"
        ),
        "experiment_controller_required": (
            context.get("experiment_controller_required") is True
        ),
    }
    invocation = ModelInvocationCall(
        authority_root,
        invocation_context,
        supported=supported,
    )
    try:
        execution = invocation.execute(
            command,
            cwd=run_dir,
            input_text="",
            timeout_seconds=timeout_seconds,
        )
    except ModelInvocationError as exc:
        canonical_events = import_registered_controller_lifecycles(run_dir)
        return {
            "chat_status": "failed",
            "agent_role": "runtime_diagnostic_agent",
            "exit_code": None,
            "run_dir": str(run_dir),
            "timeout_seconds": timeout_seconds,
            "runtime_execution_session_id": runtime_session_id,
            "controller_claim_path": str(
                authority_root / "controller_claim.json"
            ),
            "canonical_event_ids": [
                event["event_id"] for event in canonical_events
            ],
            "error": str(exc)[:500],
        }
    if execution.launch_failed:
        terminal_status = "launch_failed"
        chat_status = "failed"
        exit_code = None
    elif execution.timed_out:
        terminal_status = "timed_out"
        chat_status = "timed_out"
        exit_code = None
    else:
        exit_code = execution.returncode
        terminal_status = "completed" if exit_code == 0 else "failed"
        chat_status = terminal_status
    terminal = invocation.finalize(
        terminal_status,
        execution,
        terminal_writer="runtime_diagnostic_controller",
    )
    canonical_events = import_registered_controller_lifecycles(run_dir)
    if execution.stdout:
        sys.stdout.write(execution.stdout)
        sys.stdout.flush()
    if execution.stderr:
        sys.stderr.write(execution.stderr)
        sys.stderr.flush()
    return {
        "chat_status": chat_status,
        "agent_role": "runtime_diagnostic_agent",
        "exit_code": exit_code,
        "run_dir": str(run_dir),
        "runtime_execution_session_id": runtime_session_id,
        "controller_claim_path": str(authority_root / "controller_claim.json"),
        "canonical_event_ids": [
            event["event_id"] for event in canonical_events
        ],
        "model_invocation": {
            **invocation.lifecycle.summary(),
            "usage_event_id": terminal["usage_event_id"],
            "terminal_status": terminal["terminal_status"],
            "usage_status": terminal["usage_status"],
        },
    }


def _containing_experiment_controller_reference(run_dir):
    from .experiment_controller import (
        discover_experiment_controller_reference,
    )

    run_dir = Path(run_dir).resolve()
    for candidate in (run_dir, *run_dir.parents):
        reference = discover_experiment_controller_reference(candidate)
        if reference is not None:
            return reference
    return None


def _interactive_codex_command(context, codex_command=None, model=None):
    command = _codex_command(codex_command)
    command.extend(
        [
            "-C",
            context["run_dir"],
            "-s",
            "read-only",
            "--no-alt-screen",
        ]
    )
    if model:
        command.extend(["-m", model])
    command.append(context.get("prompt") or render_runtime_diagnostic_context(context))
    return command


def _latest_failure(events, integration_items, text_limit):
    for event in reversed(events):
        payload = event.get("payload") if isinstance(event, dict) else None
        if not isinstance(payload, dict):
            continue
        stderr = payload.get("integration_verification_stderr") or payload.get("stderr") or ""
        status = (
            payload.get("integration_verification_status")
            or payload.get("result_status")
            or payload.get("status")
        )
        if event.get("event_type") == "integration_verified" and status == "failed":
            return _failure_summary(event, payload, stderr, status, text_limit)
        if status in {"failed", "timed_out"}:
            return _failure_summary(event, payload, stderr, status, text_limit)
    for item in reversed(integration_items):
        status = item.get("integration_verification_status")
        if status == "failed":
            return {
                "event_type": "integration_queue",
                "task_id": item.get("task_id"),
                "attempt_id": item.get("attempt_id"),
                "status": status,
                "failed_test": None,
                "stderr_excerpt": None,
            }
    return None


def _failure_summary(event, payload, stderr, status, text_limit):
    return {
        "event_type": event.get("event_type"),
        "event_id": event.get("event_id"),
        "sequence": event.get("sequence"),
        "task_id": payload.get("task_id"),
        "attempt_id": payload.get("attempt_id"),
        "status": status,
        "failed_test": _failed_test_name(stderr),
        "stderr_excerpt": _failure_excerpt(stderr, text_limit),
    }


def _failed_test_name(stderr):
    if not isinstance(stderr, str) or not stderr:
        return None
    for pattern in _FAILURE_TEST_PATTERNS:
        match = pattern.search(stderr)
        if match:
            return match.group(1).rsplit(".", 1)[-1]
    return None


def _failure_excerpt(stderr, text_limit):
    if not isinstance(stderr, str) or not stderr:
        return None
    lines = stderr.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if "FAIL:" in line or "ERROR:" in line or "FAILED" in line:
            start = max(0, index - 3)
            break
    excerpt = "\n".join(lines[start : start + 35])
    if len(excerpt) > text_limit:
        return excerpt[: text_limit - 14].rstrip() + "\n[truncated]"
    return excerpt


def _integration_items(integration_queue):
    items = integration_queue.get("items") if isinstance(integration_queue, dict) else []
    if not isinstance(items, list):
        return []
    keys = [
        "task_id",
        "attempt_id",
        "queue_status",
        "integration_status",
        "integration_verification_status",
        "integration_verification_exit_code",
        "integration_worktree_path",
    ]
    return [
        {key: item.get(key) for key in keys if item.get(key) is not None}
        for item in items
        if isinstance(item, dict)
    ]


def _worker_results(run_dir):
    results_dir = Path(run_dir) / "codex_results"
    if not results_dir.exists():
        return []
    results = []
    for path in sorted(results_dir.glob("codex_result_*.json")):
        payload = _read_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        results.append(
            {
                "path": str(path),
                "attempt_id": _attempt_id_from_result_path(path),
                "result_status": payload.get("result_status"),
                "changed_files": payload.get("changed_files") or [],
                "operator_summary": output.get("operator_summary") or output.get("summary"),
            }
        )
    return results


def _attempt_id_from_result_path(path):
    name = Path(path).stem
    prefix = "codex_result_"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _backlog_tasks(run_dir):
    tasks = []
    for path in sorted((Path(run_dir) / "steps").glob("*/backlog.json")):
        payload = _read_json_if_exists(path)
        items = payload.get("items") if isinstance(payload, dict) else []
        if isinstance(items, list):
            tasks.extend(item for item in items if isinstance(item, dict))
    return tasks


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return records


def _indent(text):
    return "\n".join(f"  {line}" for line in str(text).splitlines())


def _compact_json(value, limit):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[: limit - 14].rstrip() + " [truncated]"


def _codex_command(codex_command):
    if codex_command is None:
        return ["codex"]
    if isinstance(codex_command, str):
        raise ValueError("codex_command must be a string array, not a bare string")
    command = list(codex_command)
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("codex_command must be a non-empty string array")
    return command


def _write_exclusive_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
    except FileExistsError:
        if path.read_text(encoding="utf-8") != payload:
            raise RuntimeError(f"conflicting controller claim: {path}")

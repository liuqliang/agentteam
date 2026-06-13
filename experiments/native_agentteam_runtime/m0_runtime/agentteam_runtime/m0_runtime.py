import json
import os
import sqlite3
import subprocess
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from .integration_queue import integration_queue_path, upsert_integration_queue_item
from .planner_context import build_artifact_context
from .repo_map import (
    build_repo_context,
    build_repository_map,
    REPO_CONTEXT_SCHEMA_VERSION,
)


class SystemClock:
    def now(self):
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeRuntimeAdapter:
    def run(self, message, worktree_path=None):
        if message["payload"].get("task_kind") == "decompose_backlog":
            context = _read_planner_context_if_present(message["payload"])
            milestone_id = context.get(
                "milestone_id",
                message["payload"].get("milestone_id", "M21"),
            )
            default_worker_role = context.get(
                "default_worker_role",
                message["payload"].get("default_worker_role", "repo_map_agent"),
            )
            allowed_write_scopes = context.get("allowed_write_scopes") or ["generated/"]
            return {
                "result_status": "completed",
                "changed_files": [],
                "output": {
                    "adapter": "fake",
                    "task_proposal": {
                        "milestone_id": milestone_id,
                        "tasks": [
                            {
                                "task_id": f"TASK-{milestone_id}-GENERATED-001",
                                "objective": f"Run generated worker task for {milestone_id}.",
                                "goal_alignment": f"Advance milestone {milestone_id} with a bounded generated task.",
                                "required_deliverables": [
                                    "goal_alignment_summary",
                                    "implemented_changes_or_no_safe_change_rationale",
                                    "verification_summary",
                                    "next_steps",
                                ],
                                "read_scope": ["."],
                                "write_scope": [allowed_write_scopes[0]],
                                "required_role": default_worker_role,
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    },
                },
            }
        changed_files = _fake_changed_files(message["payload"]["write_scope"])
        if worktree_path and changed_files:
            target = Path(worktree_path) / changed_files[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "task_id": message["payload"]["task_id"],
                        "attempt_id": message["payload"]["attempt_id"],
                        "generated_by": "FakeRuntimeAdapter",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        evidence_summary = _fake_evidence_summary(message["payload"], changed_files)
        return {
            "result_status": "completed",
            "changed_files": changed_files,
            "output": {
                "adapter": "fake",
                "operator_summary": _fake_operator_summary(message["payload"], changed_files),
                "evidence_summary": evidence_summary,
            },
        }


def _read_planner_context_if_present(payload):
    context_path = payload.get("planner_context_path")
    if not context_path:
        return {}
    return json.loads(Path(context_path).read_text(encoding="utf-8"))


class ShellRuntimeAdapter:
    def __init__(self, command, timeout_seconds=60):
        if not command:
            raise ValueError("ShellRuntimeAdapter command must not be empty")
        self.command = list(command)
        self.timeout_seconds = timeout_seconds

    def run(self, message, worktree_path=None):
        try:
            completed = subprocess.run(
                self.command,
                cwd=worktree_path,
                input=json.dumps(message, sort_keys=True),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "result_status": "timed_out",
                "changed_files": [],
                "output": {
                    "adapter": "shell",
                    "error": "timeout",
                    "timeout_seconds": self.timeout_seconds,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                },
            }

        if completed.returncode != 0:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {
                    "adapter": "shell",
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            }

        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {
                    "adapter": "shell",
                    "error": "invalid_json_stdout",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            }

        return _normalize_runtime_result(result, adapter="shell", stderr=completed.stderr)


class CodexRuntimeAdapter:
    def __init__(
        self,
        command=None,
        model=None,
        sandbox="workspace-write",
        timeout_seconds=300,
        extra_args=None,
        fallback_worktree_path=None,
        output_dir=None,
    ):
        self.command = list(command or ["codex", "exec"])
        self.model = model
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.fallback_worktree_path = (
            str(fallback_worktree_path) if fallback_worktree_path else None
        )
        self.output_dir = Path(output_dir) if output_dir else None

    def bind_output_dir(self, output_dir):
        return CodexRuntimeAdapter(
            command=self.command,
            model=self.model,
            sandbox=self.sandbox,
            timeout_seconds=self.timeout_seconds,
            extra_args=self.extra_args,
            fallback_worktree_path=self.fallback_worktree_path,
            output_dir=output_dir,
        )

    def run(self, message, worktree_path=None):
        runtime_worktree_path = worktree_path or self.fallback_worktree_path
        using_fallback = worktree_path is None and self.fallback_worktree_path is not None
        if not runtime_worktree_path:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "codex", "error": "missing_worktree_path"},
            }

        fallback_status_before = (
            _git_status_signature(runtime_worktree_path)
            if using_fallback
            else None
        )
        temporary_result_dir = None
        result_path = self._result_path(
            runtime_worktree_path,
            message["payload"]["attempt_id"],
            using_fallback=using_fallback,
        )
        if result_path is None:
            temporary_result_dir = tempfile.TemporaryDirectory()
            result_path = (
                Path(temporary_result_dir.name)
                / f"codex_result_{message['payload']['attempt_id']}.json"
            )
        try:
            result_path.parent.mkdir(parents=True, exist_ok=True)

            command = self._build_command(runtime_worktree_path, result_path)
            prompt = self._build_prompt(message)
            try:
                completed = subprocess.run(
                    command,
                    cwd=runtime_worktree_path,
                    input=prompt,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "result_status": "timed_out",
                    "changed_files": [],
                    "output": {
                        "adapter": "codex",
                        "error": "timeout",
                        "timeout_seconds": self.timeout_seconds,
                        "stdout": exc.stdout or "",
                        "stderr": exc.stderr or "",
                    },
                }

            fallback_modification = self._fallback_modification_result(
                runtime_worktree_path,
                fallback_status_before,
            )
            if fallback_modification:
                return fallback_modification

            if completed.returncode != 0:
                permission_request = _codex_permission_request_from_failure(
                    command,
                    completed.returncode,
                    completed.stdout,
                    completed.stderr,
                    self.sandbox,
                )
                if permission_request:
                    return {
                        "result_status": "blocked",
                        "changed_files": [],
                        "output": {
                            "adapter": "codex",
                            "error": "permission_required",
                            "permission_request": permission_request,
                            "exit_code": completed.returncode,
                            "stdout": completed.stdout,
                            "stderr": completed.stderr,
                        },
                    }
                return {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "adapter": "codex",
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    },
                }

            if not result_path.exists():
                return {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "adapter": "codex",
                        "error": "missing_output_last_message",
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    },
                }

            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {
                    "result_status": "failed",
                    "changed_files": [],
                    "output": {
                        "adapter": "codex",
                        "error": "invalid_output_last_message_json",
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                    },
                }

            return _normalize_runtime_result(
                result,
                adapter="codex",
                stderr=completed.stderr,
            )
        finally:
            if temporary_result_dir:
                temporary_result_dir.cleanup()

    def _result_path(self, runtime_worktree_path, attempt_id, using_fallback=False):
        if self.output_dir:
            return self.output_dir / "codex_results" / f"codex_result_{attempt_id}.json"
        if using_fallback:
            return None
        return Path(runtime_worktree_path) / ".agentteam" / f"codex_result_{attempt_id}.json"

    def _fallback_modification_result(self, runtime_worktree_path, status_before):
        if status_before is None:
            return None
        status_after = _git_status_signature(runtime_worktree_path)
        if status_after == status_before:
            return None
        changed_files = sorted(
            {
                path
                for status, path in status_after
                if (status, path) not in status_before
            }
        )
        return {
            "result_status": "failed",
            "changed_files": [],
            "output": {
                "adapter": "codex",
                "error": "fallback_worktree_modified",
                "changed_files": changed_files,
            },
        }

    def _build_command(self, worktree_path, result_path):
        command = [
            *self.command,
            "-C",
            str(worktree_path),
            "-s",
            self.sandbox,
        ]
        if self.model:
            command.extend(["-m", self.model])
        command.extend(self.extra_args)
        command.extend(["--output-last-message", str(result_path), "-"])
        return command

    def _build_prompt(self, message):
        if message["payload"].get("task_kind") == "decompose_backlog":
            return self._build_planner_prompt(message)
        return "\n".join(
            [
                "You are an AgentTeam runtime worker.",
                "Execute only the bounded task described by this mailbox message.",
                *self._role_prompt_contract_lines(message),
                *self._role_context_package_lines(message),
                *self._repo_context_package_lines(message),
                *self._evidence_policy_lines(message),
                "Return exactly one JSON object as the final response.",
                "The JSON object must have this shape:",
                '{"result_status":"completed|blocked|failed|cancelled","changed_files":["path"],"output":{}}',
                "For completed or failed work, output must include operator_summary for the human operator.",
                "operator_summary must be natural language, not a patch dump: include what_changed, measured_result, verification_summary, merge_recommendation, and next_steps.",
                "operator_summary natural-language fields must be written in Chinese (zh-CN); keep code symbols, file paths, commands, and metric names literal.",
                "If mailbox payload has required_deliverables, operator_summary.deliverables must be a list of objects.",
                "Each deliverables item must use the exact required deliverable string in its deliverable field and include summary plus evidence.",
                "All changed_files entries must be relative paths inside the declared write_scope.",
                "Use result_status blocked only when operator guidance is required before continuing.",
                "For blocked results, include output.manual_gate with question, optional options, and optional reason.",
                "If execution needs a sandbox or capability approval, use result_status blocked with output.permission_request.",
                "Mailbox message:",
                json.dumps(message, sort_keys=True),
            ]
        )

    def _build_planner_prompt(self, message):
        payload = message["payload"]
        result_schema = {
            "result_status": "completed",
            "changed_files": [],
            "output": {
                "task_proposal": {
                    "milestone_id": payload.get("milestone_id"),
                    "tasks": [
                        {
                            "task_id": "TASK-<MILESTONE>-001",
                            "objective": "One bounded executable task.",
                            "goal_alignment": "How this task advances the current milestone and original goal.",
                            "required_deliverables": [
                                "goal_alignment_summary",
                                "implemented_changes_or_no_safe_change_rationale",
                                "verification_summary",
                                "next_steps",
                            ],
                            "read_scope": ["."],
                            "write_scope": ["generated/"],
                            "required_role": payload.get("default_worker_role"),
                            "risk_target": "L0",
                            "depends_on": [],
                            "blockers": [],
                        }
                    ],
                }
            },
        }
        return "\n".join(
            [
                "You are an AgentTeam planner.",
                "Generate bounded executable backlog tasks for the requested milestone.",
                "Read the planner context from planner_context_path before proposing tasks.",
                *self._role_prompt_contract_lines(message),
                *self._role_context_package_lines(message),
                *self._repo_context_package_lines(message),
                "Do not modify files. Planner tasks must report an empty changed_files list.",
                "Return exactly one JSON object as the final response.",
                "The JSON object must match this planner result shape:",
                json.dumps(result_schema, sort_keys=True),
                "Each proposed task must include task_id, objective, read_scope, write_scope, required_role, risk_target, depends_on, and blockers.",
                "Do not generate tasks with task_kind=decompose_backlog.",
                "Mailbox message:",
                json.dumps(message, sort_keys=True),
            ]
        )

    def _role_prompt_contract_lines(self, message):
        contract = message["payload"].get("role_prompt_contract")
        if not contract:
            return []
        return [
            "Role prompt contract:",
            json.dumps(contract, sort_keys=True),
            "Follow this role contract as additional constraints without expanding scope.",
        ]

    def _role_context_package_lines(self, message):
        role_context_path = message["payload"].get("role_context_path")
        if not role_context_path:
            return []
        return [
            "Role context package:",
            str(role_context_path),
            "Read role_context_path before using role-specific context.",
        ]

    def _repo_context_package_lines(self, message):
        repo_context_path = message["payload"].get("repo_context_path")
        if not repo_context_path:
            return []
        return [
            "Repo context package:",
            str(repo_context_path),
            "Read repo_context_path before selecting implementation files.",
        ]

    def _evidence_policy_lines(self, message):
        policy = message["payload"].get("evidence_policy")
        if not isinstance(policy, dict):
            return []
        return [
            "Evidence policy:",
            json.dumps(policy, sort_keys=True),
            "Final JSON output must include output.evidence_summary with evidence_level, evidence_status, trace_carrier, and missing_evidence.",
            "trace_carrier must be a list of objects, for example [{'type':'command','command':'...','result':'...'}].",
            "Use evidence_status complete only when trace_carrier records concrete evidence for this task.",
            "For L2 or L3 evidence, incomplete evidence blocks patch integration; list missing_evidence instead of omitting uncertain proof.",
        ]


def _codex_permission_request_from_failure(command, exit_code, stdout, stderr, sandbox):
    combined = f"{stdout or ''}\n{stderr or ''}".strip()
    lowered = combined.lower()
    indicators = [
        "operation not permitted",
        "permission denied",
        "requires approval",
        "network is restricted",
        "sandbox",
    ]
    if not any(indicator in lowered for indicator in indicators):
        return None
    if "permission" not in lowered and "sandbox" not in lowered and "approval" not in lowered:
        return None
    reason = _bounded_runtime_text(combined or f"codex exited with {exit_code}", 500)
    return {
        "request_type": "sandbox_permission",
        "requested_capability": "sandbox_escalation",
        "reason": reason,
        "command": list(command),
        "sandbox": sandbox,
        "scope": "next_attempt",
        "exit_code": exit_code,
    }


def _bounded_runtime_text(value, limit):
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 15)] + "...[truncated]"


def run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    clock=None,
    project_root=None,
    runtime_adapter=None,
    runtime_adapter_factory=None,
    runtime_profile_defaults=None,
    max_attempts=1,
    cleanup_accepted_worktrees=False,
    integrate_accepted_patch=False,
    integration_verification_command=None,
    commit_verified_integration=False,
    attempt_id_prefix=None,
):
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    clock = clock or SystemClock()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    agent_pool = _read_json(agent_pool_path)
    backlog = _read_json(backlog_path)

    task = _select_ready_task(backlog)
    agent = _find_idle_agent(agent_pool, task["required_role"])
    runtime_adapter_selection = _resolve_runtime_adapter_selection(
        runtime_adapter,
        runtime_adapter_factory,
        agent,
        task,
        agent_pool=agent_pool,
        project_root=project_root,
        runtime_profile_defaults=runtime_profile_defaults,
    )
    runtime_adapter = runtime_adapter_selection["runtime_adapter"]
    runtime_profile_source = runtime_adapter_selection["runtime_profile_source"]

    inbox_path = output_dir / agent["inbox_path"]
    events_path = output_dir / "events.jsonl"
    events = []
    attempts = []
    sequence = 1

    def append_event(event_type, actor, target_agent_id, idempotency_key, correlation_id, payload):
        nonlocal sequence
        events.append(
            _event(
                sequence,
                clock.now(),
                event_type,
                actor,
                target_agent_id,
                idempotency_key,
                correlation_id,
                payload,
            )
        )
        sequence += 1

    final_attempt = None
    for attempt_number in range(1, max_attempts + 1):
        attempt_id = _scoped_attempt_id(attempt_number, attempt_id_prefix)
        lease_id = _scoped_id("LEASE", attempt_number, attempt_id_prefix)
        message_id = _scoped_id("MSG", attempt_number, attempt_id_prefix, width=4)
        worktree_id = f"WT-{attempt_id}" if task.get("write_scope") else None
        runtime_session_id = f"SESSION-{attempt_id}"
        worktree_path = None
        branch = None
        correlation_id = f"{task['task_id']}:{attempt_id}"

        if project_root and worktree_id:
            worktree_path, branch = _create_git_worktree(
                project_root,
                output_dir,
                attempt_id,
                worktree_id,
            )

        message = {
            "message_id": message_id,
            "from_agent": agent_pool["scheduler_agent_id"],
            "to_agent": agent["agent_id"],
            "message_type": "dispatch_task",
            "correlation_id": correlation_id,
            "created_at": clock.now(),
            "lease_expires_at": "2026-05-31T00:15:00Z",
            "payload": {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "worktree_id": worktree_id,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "branch": branch,
                "objective": task["objective"],
                "goal_alignment": task.get("goal_alignment"),
                "required_deliverables": task.get("required_deliverables", []),
                "read_scope": task["read_scope"],
                "write_scope": task["write_scope"],
                **_role_prompt_fields(agent_pool, agent, task),
                **_role_context_fields(
                    agent_pool,
                    agent,
                    output_dir,
                    attempt_id,
                    project_root=project_root,
                ),
                **_repo_context_fields(project_root, output_dir, task, agent, attempt_id),
            },
        }
        _append_jsonl(inbox_path, [message])

        append_event(
            "task_selected",
            agent_pool["scheduler_agent_id"],
            None,
            f"select:{task['task_id']}:{attempt_id}",
            correlation_id,
            {"task_id": task["task_id"], "attempt_id": attempt_id},
        )
        append_event(
            "lease_acquired",
            agent_pool["scheduler_agent_id"],
            agent["agent_id"],
            f"lease:{lease_id}",
            correlation_id,
            {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "lease_status": "active",
            },
        )

        if worktree_id:
            append_event(
                "worktree_created",
                agent_pool["scheduler_agent_id"],
                agent["agent_id"],
                f"worktree:{worktree_id}",
                correlation_id,
                {
                    "task_id": task["task_id"],
                    "attempt_id": attempt_id,
                    "worktree_id": worktree_id,
                    "worktree_path": str(worktree_path) if worktree_path else None,
                    "branch": branch,
                    "write_scope": task["write_scope"],
                },
            )

        append_event(
            "message_dispatched",
            agent_pool["scheduler_agent_id"],
            agent["agent_id"],
            f"dispatch:{message_id}",
            correlation_id,
            {
                "message_id": message_id,
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                **_message_context_event_fields(message["payload"]),
            },
        )
        append_event(
            "runtime_session_started",
            agent_pool["scheduler_agent_id"],
            agent["agent_id"],
            f"runtime-session-started:{runtime_session_id}",
            correlation_id,
            {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "runtime_session_id": runtime_session_id,
                **_runtime_adapter_metadata(runtime_adapter),
                "runtime_profile_source": runtime_profile_source,
                "worktree_id": worktree_id,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "session_status": "started",
            },
        )
        runtime_adapter_for_attempt = _bind_runtime_adapter_output_dir(runtime_adapter, output_dir)
        runtime_result = runtime_adapter_for_attempt.run(message, worktree_path=worktree_path)
        append_event(
            "runtime_session_observed",
            agent["agent_id"],
            agent_pool["scheduler_agent_id"],
            f"runtime-session-observed:{runtime_session_id}",
            correlation_id,
            {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "runtime_session_id": runtime_session_id,
                "result_status": runtime_result["result_status"],
                "changed_file_count": len(runtime_result["changed_files"]),
                "session_status": "observed",
            },
        )
        diff_audit = (
            audit_worktree_diff(worktree_path, runtime_result["changed_files"])
            if worktree_path
            else None
        )
        patch_path = (
            write_patch_artifact(
                worktree_path,
                output_dir / "attempts" / attempt_id,
                diff_audit["actual_changed_files"],
            )
            if worktree_path and diff_audit and diff_audit["actual_changed_files"]
            else None
        )
        append_event(
            "runtime_output_received",
            agent["agent_id"],
            agent_pool["scheduler_agent_id"],
            f"runtime-result:{attempt_id}",
            correlation_id,
            {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "result_status": runtime_result["result_status"],
                "changed_files": runtime_result["changed_files"],
                "output": runtime_result.get("output", {}),
                "diff_audit": diff_audit,
                "patch_path": str(patch_path) if patch_path else None,
            },
        )
        append_event(
            "runtime_session_stopped",
            agent_pool["scheduler_agent_id"],
            agent["agent_id"],
            f"runtime-session-stopped:{runtime_session_id}",
            correlation_id,
            {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "runtime_session_id": runtime_session_id,
                "result_status": runtime_result["result_status"],
                "session_status": "stopped",
            },
        )

        outcome = classify_attempt_outcome(runtime_result, task, diff_audit=diff_audit)
        append_event(
            "validation_accepted"
            if outcome["validation_status"] == "accepted"
            else "validation_rejected",
            agent_pool["scheduler_agent_id"],
            agent["agent_id"],
            f"validate:{attempt_id}",
            correlation_id,
            {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "validation_status": outcome["validation_status"],
                "failure_category": outcome["failure_category"],
                "retryable": outcome["retryable"],
                "lease_id": lease_id,
                "diff_audit": diff_audit,
                "patch_path": str(patch_path) if patch_path else None,
                "semantic_validation": outcome.get("semantic_validation"),
            },
        )

        final_attempt = {
            "task_id": task["task_id"],
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "message_id": message_id,
            "runtime_session_id": runtime_session_id,
            "runtime_session_status": "stopped",
            "worktree_id": worktree_id,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "branch": branch,
            "validation_status": outcome["validation_status"],
            "failure_category": outcome["failure_category"],
            "retryable": outcome["retryable"],
            "semantic_validation": outcome.get("semantic_validation"),
            "diff_audit": diff_audit,
            "patch_path": str(patch_path) if patch_path else None,
            "integration_status": "not_requested",
            "integration_branch": None,
            "integration_worktree_path": None,
            "integration_verification_status": "not_requested",
            "integration_verification_exit_code": None,
            "integration_verification_stdout": "",
            "integration_verification_stderr": "",
            "integration_commit_status": "not_requested",
            "integration_commit_sha": None,
            "integration_commit_message": None,
            "integration_commit_reason": None,
            "integration_commit_stdout": "",
            "integration_commit_stderr": "",
            "integration_queue_status": "not_queued",
            "integration_queue_item_id": None,
            "integration_queue_path": str(integration_queue_path(output_dir)),
            "worktree_removed": False,
        }
        attempts.append(final_attempt)

        if outcome["validation_status"] == "accepted":
            if patch_path:
                queue = upsert_integration_queue_item(output_dir, final_attempt)
                final_attempt.update(queue)
                append_event(
                    "integration_queued",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"integration-queued:{attempt_id}",
                    correlation_id,
                    {
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        "patch_path": str(patch_path),
                        **queue,
                    },
                )
            if integrate_accepted_patch and project_root and patch_path:
                integration = apply_patch_to_integration_worktree(
                    project_root,
                    output_dir,
                    task["task_id"],
                    patch_path,
                )
                final_attempt.update(integration)
                append_event(
                    "patch_integrated",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"patch-integrated:{attempt_id}",
                    correlation_id,
                    {
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        "patch_path": str(patch_path),
                        **integration,
                    },
                )
                if integration_verification_command:
                    verification = run_integration_verification(
                        integration_verification_command,
                        integration["integration_worktree_path"],
                    )
                    final_attempt.update(verification)
                    append_event(
                        "integration_verified",
                        agent_pool["scheduler_agent_id"],
                        agent["agent_id"],
                        f"integration-verified:{attempt_id}",
                        correlation_id,
                        {
                            "task_id": task["task_id"],
                            "attempt_id": attempt_id,
                            "lease_id": lease_id,
                            **verification,
                        },
                    )
            if commit_verified_integration:
                integration_commit = evaluate_integration_commit(
                    final_attempt,
                    task["task_id"],
                    attempt_id,
                )
                final_attempt.update(integration_commit)
                append_event(
                    "integration_commit_evaluated",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"integration-commit:{attempt_id}",
                    correlation_id,
                    {
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "lease_id": lease_id,
                        **integration_commit,
                    },
                )
            if patch_path:
                final_attempt.update(
                    upsert_integration_queue_item(output_dir, final_attempt)
                )
            if cleanup_accepted_worktrees and project_root and worktree_path:
                _remove_git_worktree(project_root, worktree_path)
                final_attempt["worktree_removed"] = True
                append_event(
                    "worktree_removed",
                    agent_pool["scheduler_agent_id"],
                    agent["agent_id"],
                    f"worktree-removed:{worktree_id}",
                    correlation_id,
                    {
                        "task_id": task["task_id"],
                        "attempt_id": attempt_id,
                        "worktree_id": worktree_id,
                        "worktree_path": str(worktree_path),
                        "cleanup_status": "removed",
                    },
                )
            append_event(
                "backlog_updated",
                agent_pool["scheduler_agent_id"],
                None,
                f"backlog-done:{task['task_id']}",
                correlation_id,
                {
                    "task_id": task["task_id"],
                    "attempt_id": attempt_id,
                    "task_status": "done",
                    "lease_id": lease_id,
                },
            )
            break

        if outcome["retryable"] and attempt_number < max_attempts:
            append_event(
                "recovery_routed",
                agent_pool["scheduler_agent_id"],
                agent["agent_id"],
                f"recovery:{attempt_id}",
                correlation_id,
                {
                    "task_id": task["task_id"],
                    "attempt_id": attempt_id,
                    "lease_id": lease_id,
                    "failure_category": outcome["failure_category"],
                    "next_attempt_id": _scoped_attempt_id(
                        attempt_number + 1,
                        attempt_id_prefix,
                    ),
                    "recovery_action": "retry",
                },
            )
            continue
        break

    _append_jsonl(events_path, events)

    return {
        "task_id": task["task_id"],
        "attempt_id": final_attempt["attempt_id"],
        "lease_id": final_attempt["lease_id"],
        "message_id": final_attempt["message_id"],
        "runtime_session_id": final_attempt["runtime_session_id"],
        "runtime_session_status": final_attempt["runtime_session_status"],
        "worktree_id": final_attempt["worktree_id"],
        "worktree_path": final_attempt["worktree_path"],
        "branch": final_attempt["branch"],
        "validation_status": final_attempt["validation_status"],
        "failure_category": final_attempt["failure_category"],
        "retryable": final_attempt["retryable"],
        "diff_audit": final_attempt["diff_audit"],
        "patch_path": final_attempt["patch_path"],
        "integration_status": final_attempt["integration_status"],
        "integration_branch": final_attempt["integration_branch"],
        "integration_worktree_path": final_attempt["integration_worktree_path"],
        "integration_verification_status": final_attempt["integration_verification_status"],
        "integration_verification_exit_code": final_attempt["integration_verification_exit_code"],
        "integration_verification_stdout": final_attempt["integration_verification_stdout"],
        "integration_verification_stderr": final_attempt["integration_verification_stderr"],
        "integration_commit_status": final_attempt["integration_commit_status"],
        "integration_commit_sha": final_attempt["integration_commit_sha"],
        "integration_commit_message": final_attempt["integration_commit_message"],
        "integration_commit_reason": final_attempt["integration_commit_reason"],
        "integration_commit_stdout": final_attempt["integration_commit_stdout"],
        "integration_commit_stderr": final_attempt["integration_commit_stderr"],
        "integration_queue_status": final_attempt["integration_queue_status"],
        "integration_queue_item_id": final_attempt["integration_queue_item_id"],
        "integration_queue_path": final_attempt["integration_queue_path"],
        "attempt_count": len(attempts),
        "attempts": attempts,
        "worktree_removed": final_attempt["worktree_removed"],
        "events_path": str(events_path),
        "mailbox_path": str(inbox_path),
    }


class FileScheduler:
    def __init__(
        self,
        agent_pool_path,
        backlog_path,
        output_dir,
        clock=None,
        project_root=None,
        runtime_adapter=None,
        max_attempts=1,
        cleanup_accepted_worktrees=False,
        integrate_accepted_patch=False,
        integration_verification_command=None,
        commit_verified_integration=False,
        state_path=None,
        runtime_adapter_factory=None,
        runtime_profile_defaults=None,
    ):
        self.agent_pool_path = agent_pool_path
        self.backlog_path = backlog_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.project_root = project_root
        self.runtime_adapter = runtime_adapter
        self.runtime_adapter_factory = runtime_adapter_factory
        self.runtime_profile_defaults = runtime_profile_defaults
        self.max_attempts = max_attempts
        self.cleanup_accepted_worktrees = cleanup_accepted_worktrees
        self.integrate_accepted_patch = integrate_accepted_patch
        self.integration_verification_command = integration_verification_command
        self.commit_verified_integration = commit_verified_integration
        self.state_path = Path(state_path or self.output_dir / "state" / "scheduler_state.json")
        self.state_db_path = self.output_dir / "state" / "scheduler_state.sqlite"
        self.events_path = self.output_dir / "events.jsonl"
        self.run_id = "RUN-FILE-SCHEDULER"
        self.state = self._load_or_create_state()

    def step_once(self):
        task = self._select_ready_task()
        if not task:
            self.state["scheduler_status"] = "idle"
            self._write_state()
            return {
                "step_status": "idle",
                "reason": "no_ready_task",
                "state_path": str(self.state_path),
            }

        step_number = len(self.state["steps"]) + 1
        step_id = f"STEP-{step_number:04d}-{task['task_id']}"
        step_dir = self.output_dir / "steps" / step_id
        step_dir.mkdir(parents=True, exist_ok=True)
        step_backlog_path = step_dir / "backlog.json"
        step_backlog_path.write_text(
            json.dumps(
                {
                    "backlog_id": self.state["backlog"].get("backlog_id", "BL-SCHEDULER-STEP"),
                    "items": [deepcopy(task)],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        result = run_simulation(
            self.agent_pool_path,
            step_backlog_path,
            step_dir,
            clock=self.clock,
            project_root=self.project_root,
            runtime_adapter=self.runtime_adapter,
            runtime_adapter_factory=self.runtime_adapter_factory,
            runtime_profile_defaults=self.runtime_profile_defaults,
            max_attempts=self.max_attempts,
            cleanup_accepted_worktrees=self.cleanup_accepted_worktrees,
            integrate_accepted_patch=self.integrate_accepted_patch,
            integration_verification_command=self.integration_verification_command,
            commit_verified_integration=self.commit_verified_integration,
            attempt_id_prefix=task["task_id"],
        )
        self._update_task_from_result(task["task_id"], result)
        step_summary = {
            "step_id": step_id,
            "step_status": "processed",
            "task_id": task["task_id"],
            "validation_status": result["validation_status"],
            "failure_category": result["failure_category"],
            "result": result,
        }
        self.state["steps"].append(step_summary)
        self.state["scheduler_status"] = "running"
        self._append_step_events_to_canonical_log(step_id, result["events_path"])
        rebuild_sqlite_state_index(self.state_db_path, self.events_path)
        self._write_state()
        return step_summary

    def run_until_idle(self, max_steps=100):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        for _ in range(max_steps):
            step = self.step_once()
            if step["step_status"] == "idle":
                return self._summary("idle")
        self.state["scheduler_status"] = "max_steps_reached"
        self._write_state()
        return self._summary("max_steps_reached")

    def _load_or_create_state(self):
        if self.state_path.exists():
            return _read_json(self.state_path)
        return {
            "scheduler_status": "initialized",
            "backlog": _read_json(self.backlog_path),
            "steps": [],
        }

    def _select_ready_task(self):
        done_by_id = {
            item["task_id"]: item.get("backlog_status") == "done"
            for item in self.state["backlog"]["items"]
        }
        for task in self.state["backlog"]["items"]:
            if task.get("backlog_status") != "ready":
                continue
            if task.get("blockers"):
                continue
            if not all(done_by_id.get(dep_id, False) for dep_id in task.get("depends_on", [])):
                continue
            return task
        return None

    def _update_task_from_result(self, task_id, result):
        for task in self.state["backlog"]["items"]:
            if task["task_id"] != task_id:
                continue
            if result["validation_status"] == "accepted":
                task["backlog_status"] = "done"
                task["blockers"] = []
            else:
                task["backlog_status"] = "blocked"
                task["blockers"] = [result["failure_category"] or "validation_rejected"]
            return
        raise ValueError(f"task not found in scheduler state: {task_id}")

    def _summary(self, scheduler_status):
        processed_task_ids = [
            step["task_id"]
            for step in self.state["steps"]
            if step["step_status"] == "processed"
        ]
        return {
            "scheduler_status": scheduler_status,
            "processed_task_ids": processed_task_ids,
            "step_count": len(self.state["steps"]),
            "steps": self.state["steps"],
            "events_path": str(self.events_path),
            "state_path": str(self.state_path),
            "state_db_path": str(self.state_db_path),
        }

    def _write_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")

    def _append_step_events_to_canonical_log(self, step_id, step_events_path):
        sequence = self._next_canonical_sequence()
        canonical_events = []
        for step_event in _read_jsonl(step_events_path):
            source_event_id = step_event["event_id"]
            source_sequence = step_event["sequence"]
            canonical_event = {
                **step_event,
                "event_id": f"EVT-{sequence:04d}",
                "sequence": sequence,
                "run_id": self.run_id,
                "step_id": step_id,
                "source_event_id": source_event_id,
                "source_event_sequence": source_sequence,
            }
            canonical_events.append(canonical_event)
            sequence += 1
        _append_jsonl(self.events_path, canonical_events)

    def _next_canonical_sequence(self):
        if not self.events_path.exists():
            return 1
        existing_sequences = [
            event["sequence"]
            for event in _read_jsonl(self.events_path)
        ]
        if not existing_sequences:
            return 1
        return max(existing_sequences) + 1


def run_scheduler_loop(
    agent_pool_path,
    backlog_path,
    output_dir,
    clock=None,
    project_root=None,
    runtime_adapter=None,
    runtime_adapter_factory=None,
    runtime_profile_defaults=None,
    max_attempts=1,
    cleanup_accepted_worktrees=False,
    integrate_accepted_patch=False,
    integration_verification_command=None,
    commit_verified_integration=False,
    max_steps=100,
):
    scheduler = FileScheduler(
        agent_pool_path,
        backlog_path,
        output_dir,
        clock=clock,
        project_root=project_root,
        runtime_adapter=runtime_adapter,
        runtime_adapter_factory=runtime_adapter_factory,
        runtime_profile_defaults=runtime_profile_defaults,
        max_attempts=max_attempts,
        cleanup_accepted_worktrees=cleanup_accepted_worktrees,
        integrate_accepted_patch=integrate_accepted_patch,
        integration_verification_command=integration_verification_command,
        commit_verified_integration=commit_verified_integration,
    )
    return scheduler.run_until_idle(max_steps=max_steps)


def rebuild_sqlite_state_index(db_path, events_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = replay_events(events_path)
    events = list(_read_jsonl(events_path))
    with sqlite3.connect(db_path) as connection:
        _create_sqlite_state_schema(connection)
        connection.execute("delete from tasks")
        connection.execute("delete from attempts")
        connection.execute("delete from leases")
        connection.execute("delete from runtime_sessions")
        connection.execute("delete from events")
        connection.executemany(
            "insert into tasks(task_id, task_status) values(?, ?)",
            [
                (task_id, task_state.get("task_status"))
                for task_id, task_state in sorted(snapshot["tasks"].items())
            ],
        )
        connection.executemany(
            """
            insert into attempts(
                attempt_id,
                task_id,
                attempt_status,
                validation_status,
                repo_context_path
            ) values(?, ?, ?, ?, ?)
            """,
            [
                (
                    attempt_id,
                    attempt_state.get("task_id"),
                    attempt_state.get("attempt_status"),
                    attempt_state.get("validation_status"),
                    attempt_state.get("repo_context_path"),
                )
                for attempt_id, attempt_state in sorted(snapshot["attempts"].items())
            ],
        )
        connection.executemany(
            "insert into leases(lease_id, task_id, attempt_id, lease_status) values(?, ?, ?, ?)",
            [
                (
                    lease_id,
                    lease_state.get("task_id"),
                    lease_state.get("attempt_id"),
                    lease_state.get("lease_status"),
                )
                for lease_id, lease_state in sorted(snapshot["leases"].items())
            ],
        )
        connection.executemany(
            """
            insert into runtime_sessions(
                runtime_session_id,
                task_id,
                attempt_id,
                lease_id,
                session_status,
                result_status,
                runtime_adapter,
                changed_file_count,
                runtime_model,
                runtime_sandbox,
                runtime_timeout_seconds,
                runtime_profile_source
            ) values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    runtime_session_id,
                    session_state.get("task_id"),
                    session_state.get("attempt_id"),
                    session_state.get("lease_id"),
                    session_state.get("session_status"),
                    session_state.get("result_status"),
                    session_state.get("runtime_adapter"),
                    session_state.get("changed_file_count"),
                    session_state.get("runtime_model"),
                    session_state.get("runtime_sandbox"),
                    session_state.get("runtime_timeout_seconds"),
                    session_state.get("runtime_profile_source"),
                )
                for runtime_session_id, session_state in sorted(
                    snapshot["runtime_sessions"].items()
                )
            ],
        )
        connection.executemany(
            """
            insert into events(
                sequence,
                event_id,
                event_type,
                task_id,
                attempt_id,
                lease_id,
                step_id,
                time
            ) values(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event["sequence"],
                    event["event_id"],
                    event["event_type"],
                    event["payload"].get("task_id"),
                    event["payload"].get("attempt_id"),
                    event["payload"].get("lease_id"),
                    event.get("step_id"),
                    event["time"],
                )
                for event in events
            ],
        )
    return str(db_path)


def answer_manual_gate(output_dir, question_id, answer, operator="operator", clock=None):
    if not question_id:
        raise ValueError("question_id must be a non-empty string")
    if not answer:
        raise ValueError("answer must be a non-empty string")
    if not operator:
        raise ValueError("operator must be a non-empty string")

    clock = clock or SystemClock()
    output_dir = Path(output_dir)
    events_path = output_dir / "events.jsonl"
    state_path = output_dir / "state" / "two_phase_scheduler_state.json"
    if not events_path.exists():
        raise FileNotFoundError(f"missing runtime events: {events_path}")
    if not state_path.exists():
        raise FileNotFoundError(f"missing two-phase scheduler state: {state_path}")

    events = list(_read_jsonl(events_path))
    gate_event = _find_manual_gate_event(events, question_id)
    if gate_event is None:
        raise ValueError(f"manual gate question not found: {question_id}")
    if _manual_gate_has_answer(events, question_id):
        return {
            "answer_status": "already_answered",
            "question_id": question_id,
            "task_id": gate_event["payload"]["task_id"],
            "events_path": str(events_path),
            "state_path": str(state_path),
        }

    task_id = gate_event["payload"]["task_id"]
    state = _read_json(state_path)
    task = _state_task_by_id(state, task_id)
    task["backlog_status"] = "ready"
    task["blockers"] = []
    guidance = task.setdefault("operator_guidance", [])
    guidance.append(
        {
            "question_id": question_id,
            "answer": answer,
            "operator": operator,
        }
    )
    state["scheduler_status"] = "running"
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    sequence = _next_event_sequence(events)
    now = clock.now()
    run_id = gate_event.get("run_id")
    step_id = f"OPERATOR-ANSWER-{question_id}"
    answer_event = {
        **_event(
            sequence,
            now,
            "operator_answer_received",
            operator,
            "agent-scheduler",
            f"operator-answer:{question_id}",
            f"operator:{question_id}",
            {
                "question_id": question_id,
                "task_id": task_id,
                "answer": answer,
                "answer_status": "accepted",
                "operator": operator,
            },
        ),
        "run_id": run_id,
        "step_id": step_id,
    }
    backlog_event = {
        **_event(
            sequence + 1,
            now,
            "backlog_updated",
            "agent-scheduler",
            None,
            f"backlog-ready:{task_id}:{question_id}",
            f"operator:{question_id}",
            {
                "task_id": task_id,
                "task_status": "ready",
                "update_type": "operator_answer_applied",
                "question_id": question_id,
                "blockers": [],
            },
        ),
        "run_id": run_id,
        "step_id": step_id,
    }
    _append_jsonl(events_path, [answer_event, backlog_event])
    rebuild_sqlite_state_index(output_dir / "state" / "scheduler_state.sqlite", events_path)
    return {
        "answer_status": "accepted",
        "question_id": question_id,
        "task_id": task_id,
        "events_path": str(events_path),
        "state_path": str(state_path),
    }


def list_permission_requests(output_dir):
    output_dir = Path(output_dir)
    events_path = output_dir / "events.jsonl"
    if not events_path.exists():
        raise FileNotFoundError(f"missing runtime events: {events_path}")
    snapshot = replay_events(events_path)
    requests = snapshot.get("permission_requests", {})
    waiting = [
        request
        for _request_id, request in sorted(requests.items())
        if isinstance(request, dict) and request.get("request_status") == "waiting"
    ]
    return {
        "permission_status": "waiting_permission_requests"
        if waiting
        else "no_waiting_permission_requests",
        "waiting_count": len(waiting),
        "waiting": waiting,
        "run_dir": str(output_dir.resolve()),
    }


def resolve_permission_request(
    output_dir,
    request_id,
    decision,
    operator="operator",
    reason=None,
    clock=None,
):
    if not request_id:
        raise ValueError("request_id must be a non-empty string")
    if decision not in {"approved", "denied"}:
        raise ValueError("decision must be approved or denied")
    if not operator:
        raise ValueError("operator must be a non-empty string")

    clock = clock or SystemClock()
    output_dir = Path(output_dir)
    events_path = output_dir / "events.jsonl"
    state_path = output_dir / "state" / "two_phase_scheduler_state.json"
    if not events_path.exists():
        raise FileNotFoundError(f"missing runtime events: {events_path}")
    if not state_path.exists():
        raise FileNotFoundError(f"missing two-phase scheduler state: {state_path}")

    events = list(_read_jsonl(events_path))
    request_event = _find_permission_request_event(events, request_id)
    if request_event is None:
        raise ValueError(f"permission request not found: {request_id}")
    task_id = request_event["payload"]["task_id"]
    if _permission_request_has_resolution(events, request_id):
        return {
            "permission_status": "already_resolved",
            "request_id": request_id,
            "task_id": task_id,
            "events_path": str(events_path),
            "state_path": str(state_path),
        }

    state = _read_json(state_path)
    task = _state_task_by_id(state, task_id)
    requested_capability = request_event["payload"].get("requested_capability")
    if decision == "approved":
        task["backlog_status"] = "ready"
        task["blockers"] = []
        grants = task.setdefault("permission_grants", [])
        grants.append(
            {
                "request_id": request_id,
                "requested_capability": requested_capability,
                "operator": operator,
                "reason": reason,
            }
        )
        update_type = "permission_request_approved"
        task_status = "ready"
        blockers = []
    else:
        task["backlog_status"] = "blocked"
        task["blockers"] = [request_id]
        decisions = task.setdefault("permission_decisions", [])
        decisions.append(
            {
                "request_id": request_id,
                "requested_capability": requested_capability,
                "operator": operator,
                "decision": "denied",
                "reason": reason,
            }
        )
        update_type = "permission_request_denied"
        task_status = "blocked"
        blockers = [request_id]
    state["scheduler_status"] = "running" if decision == "approved" else "blocked"
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    sequence = _next_event_sequence(events)
    now = clock.now()
    run_id = request_event.get("run_id")
    step_id = f"OPERATOR-PERMISSION-{request_id}"
    resolved_event = {
        **_event(
            sequence,
            now,
            "permission_request_resolved",
            operator,
            "agent-scheduler",
            f"permission-request-resolved:{request_id}",
            f"operator:{request_id}",
            {
                "request_id": request_id,
                "task_id": task_id,
                "attempt_id": request_event["payload"].get("attempt_id"),
                "lease_id": request_event["payload"].get("lease_id"),
                "requested_capability": requested_capability,
                "permission_status": decision,
                "operator": operator,
                "reason": reason,
            },
        ),
        "run_id": run_id,
        "step_id": step_id,
    }
    backlog_event = {
        **_event(
            sequence + 1,
            now,
            "backlog_updated",
            "agent-scheduler",
            None,
            f"backlog-permission:{task_id}:{request_id}:{decision}",
            f"operator:{request_id}",
            {
                "task_id": task_id,
                "task_status": task_status,
                "update_type": update_type,
                "request_id": request_id,
                "blockers": blockers,
            },
        ),
        "run_id": run_id,
        "step_id": step_id,
    }
    _append_jsonl(events_path, [resolved_event, backlog_event])
    rebuild_sqlite_state_index(output_dir / "state" / "scheduler_state.sqlite", events_path)
    return {
        "permission_status": decision,
        "request_id": request_id,
        "task_id": task_id,
        "events_path": str(events_path),
        "state_path": str(state_path),
    }


def _find_manual_gate_event(events, question_id):
    for event in reversed(events):
        if event.get("event_type") != "manual_gate_required":
            continue
        if event.get("payload", {}).get("question_id") == question_id:
            return event
    return None


def _manual_gate_has_answer(events, question_id):
    return any(
        event.get("event_type") == "operator_answer_received"
        and event.get("payload", {}).get("question_id") == question_id
        for event in events
    )


def _find_permission_request_event(events, request_id):
    for event in reversed(events):
        if event.get("event_type") != "permission_request_required":
            continue
        if event.get("payload", {}).get("request_id") == request_id:
            return event
    return None


def _permission_request_has_resolution(events, request_id):
    return any(
        event.get("event_type") == "permission_request_resolved"
        and event.get("payload", {}).get("request_id") == request_id
        for event in events
    )


def _state_task_by_id(state, task_id):
    for task in state.get("backlog", {}).get("items", []):
        if task.get("task_id") == task_id:
            return task
    raise ValueError(f"task not found in two-phase scheduler state: {task_id}")


def _next_event_sequence(events):
    return max((event.get("sequence", 0) for event in events), default=0) + 1


def read_scheduler_state_index(output_dir):
    output_dir = Path(output_dir)
    db_path = output_dir / "state" / "scheduler_state.sqlite"
    events_path = output_dir / "events.jsonl"
    if not db_path.exists() or (
        events_path.exists() and _sqlite_state_index_is_stale(db_path, events_path)
    ):
        if not events_path.exists():
            raise FileNotFoundError(f"missing scheduler state index: {db_path}")
        rebuild_sqlite_state_index(db_path, events_path)
    return read_sqlite_state_index(db_path, events_path=events_path)


def read_sqlite_state_index(db_path, events_path=None):
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        tasks = _fetch_sqlite_dicts(
            connection,
            "select task_id, task_status from tasks order by task_id",
        )
        attempts = _fetch_sqlite_dicts(
            connection,
            """
            select
                attempt_id,
                task_id,
                attempt_status,
                validation_status,
                repo_context_path
            from attempts
            order by attempt_id
            """,
        )
        leases = _fetch_sqlite_dicts(
            connection,
            """
            select lease_id, task_id, attempt_id, lease_status
            from leases
            order by lease_id
            """,
        )
        runtime_sessions = _fetch_sqlite_dicts(
            connection,
            """
            select
                runtime_session_id,
                task_id,
                attempt_id,
                lease_id,
                session_status,
                result_status,
                runtime_adapter,
                changed_file_count,
                runtime_model,
                runtime_sandbox,
                runtime_timeout_seconds,
                runtime_profile_source
            from runtime_sessions
            order by runtime_session_id
            """,
        )
        event_count = connection.execute("select count(*) from events").fetchone()[0]
        latest_event = connection.execute(
            """
            select sequence, event_id, event_type, task_id, attempt_id, lease_id, step_id, time
            from events
            order by sequence desc
            limit 1
            """
        ).fetchone()
    return {
        "state_db_path": str(db_path),
        "events_path": str(events_path) if events_path is not None else None,
        "tasks": tasks,
        "attempts": attempts,
        "leases": leases,
        "runtime_sessions": runtime_sessions,
        "event_count": event_count,
        "latest_event": dict(latest_event) if latest_event else None,
    }


def _fetch_sqlite_dicts(connection, query):
    return [dict(row) for row in connection.execute(query)]


def _ensure_sqlite_column(connection, table, column, column_type):
    columns = {
        row[1]
        for row in connection.execute(f"pragma table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"alter table {table} add column {column} {column_type}")


def _sqlite_state_index_is_stale(db_path, events_path):
    try:
        if _sqlite_missing_required_tables(db_path):
            return True
        indexed_event_count = _sqlite_event_count(db_path)
    except sqlite3.DatabaseError:
        return True
    canonical_event_count = sum(1 for _ in _read_jsonl(events_path))
    return indexed_event_count != canonical_event_count


def _sqlite_event_count(db_path):
    with sqlite3.connect(db_path) as connection:
        return connection.execute("select count(*) from events").fetchone()[0]


def _sqlite_missing_required_tables(db_path):
    required_tables = {"tasks", "attempts", "leases", "runtime_sessions", "events"}
    with sqlite3.connect(db_path) as connection:
        table_rows = connection.execute(
            "select name from sqlite_master where type = 'table'"
        ).fetchall()
    actual_tables = {row[0] for row in table_rows}
    if not required_tables.issubset(actual_tables):
        return True
    with sqlite3.connect(db_path) as connection:
        runtime_session_columns = {
            row[1]
            for row in connection.execute("pragma table_info(runtime_sessions)").fetchall()
        }
        attempt_columns = {
            row[1]
            for row in connection.execute("pragma table_info(attempts)").fetchall()
        }
    required_runtime_session_columns = {
        "runtime_model",
        "runtime_sandbox",
        "runtime_timeout_seconds",
        "runtime_profile_source",
    }
    required_attempt_columns = {"repo_context_path"}
    return not (
        required_runtime_session_columns.issubset(runtime_session_columns)
        and required_attempt_columns.issubset(attempt_columns)
    )


def _create_sqlite_state_schema(connection):
    connection.execute(
        """
        create table if not exists tasks(
            task_id text primary key,
            task_status text not null
        )
        """
    )
    connection.execute(
        """
        create table if not exists attempts(
            attempt_id text primary key,
            task_id text,
            attempt_status text,
            validation_status text,
            repo_context_path text
        )
        """
    )
    _ensure_sqlite_column(connection, "attempts", "repo_context_path", "text")
    connection.execute(
        """
        create table if not exists leases(
            lease_id text primary key,
            task_id text,
            attempt_id text,
            lease_status text
        )
        """
    )
    connection.execute(
        """
        create table if not exists runtime_sessions(
            runtime_session_id text primary key,
            task_id text,
            attempt_id text,
            lease_id text,
            session_status text,
            result_status text,
            runtime_adapter text,
            changed_file_count integer,
            runtime_model text,
            runtime_sandbox text,
            runtime_timeout_seconds integer,
            runtime_profile_source text
        )
        """
    )
    _ensure_sqlite_column(
        connection,
        "runtime_sessions",
        "runtime_profile_source",
        "text",
    )
    connection.execute(
        """
        create table if not exists events(
            sequence integer primary key,
            event_id text not null,
            event_type text not null,
            task_id text,
            attempt_id text,
            lease_id text,
            step_id text,
            time text not null
        )
        """
    )


def replay_events(events_path):
    return replay_event_records(_read_jsonl(events_path))


def replay_event_records(events):
    snapshot = {
        "tasks": {},
        "attempts": {},
        "leases": {},
        "runtime_sessions": {},
        "integration_queue": {},
        "manual_gates": {},
        "permission_requests": {},
    }
    for event in events:
        payload = event["payload"]
        task_id = payload.get("task_id")
        attempt_id = payload.get("attempt_id")
        lease_id = payload.get("lease_id")

        if event["event_type"] == "task_selected":
            snapshot["tasks"][task_id] = {"task_status": "running"}
            snapshot["attempts"][attempt_id] = {
                "attempt_status": "created",
                "task_id": task_id,
            }
        elif event["event_type"] == "task_reassigned":
            attempt_state = snapshot["attempts"].setdefault(attempt_id, {})
            attempt_state.setdefault("task_id", task_id)
            attempt_state["reassignment"] = {
                "reassignment_reason": payload["reassignment_reason"],
                "required_role": payload["required_role"],
                "unavailable_agent_ids": payload["unavailable_agent_ids"],
                "selected_agent_id": payload["selected_agent_id"],
            }
        elif event["event_type"] == "lease_acquired":
            snapshot["leases"][lease_id] = {
                "lease_status": "active",
                "attempt_id": attempt_id,
                "task_id": task_id,
            }
            attempt_state = snapshot["attempts"].setdefault(attempt_id, {})
            attempt_state["attempt_status"] = "dispatched"
            attempt_state.setdefault("task_id", task_id)
        elif event["event_type"] == "message_dispatched":
            attempt_state = snapshot["attempts"].setdefault(attempt_id, {})
            attempt_state.setdefault("task_id", task_id)
            if payload.get("repo_context_path"):
                attempt_state["repo_context_path"] = payload["repo_context_path"]
            if payload.get("repo_context_schema_version"):
                attempt_state["repo_context_schema_version"] = payload[
                    "repo_context_schema_version"
                ]
        elif event["event_type"] == "worktree_created":
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_id"] = payload["worktree_id"]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_path"] = payload[
                "worktree_path"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["branch"] = payload["branch"]
            _copy_optional_payload_fields(
                snapshot["attempts"].setdefault(attempt_id, {}),
                payload,
                [
                    "integration_base_ref",
                    "integration_base_sha",
                    "integration_baseline_branch",
                    "integration_baseline_worktree_path",
                ],
            )
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_status"] = "created"
        elif event["event_type"] == "worktree_removed":
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_id"] = payload["worktree_id"]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_path"] = payload[
                "worktree_path"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_status"] = "removed"
        elif event["event_type"] == "runtime_session_started":
            runtime_session_id = payload["runtime_session_id"]
            snapshot["runtime_sessions"][runtime_session_id] = {
                "session_status": "started",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "runtime_adapter": payload["runtime_adapter"],
                "runtime_model": payload.get("runtime_model"),
                "runtime_sandbox": payload.get("runtime_sandbox"),
                "runtime_timeout_seconds": payload.get("runtime_timeout_seconds"),
                "runtime_profile_source": payload.get("runtime_profile_source"),
                "worktree_id": payload.get("worktree_id"),
                "worktree_path": payload.get("worktree_path"),
            }
            _copy_optional_payload_fields(
                snapshot["runtime_sessions"][runtime_session_id],
                payload,
                [
                    "integration_base_ref",
                    "integration_base_sha",
                    "integration_baseline_branch",
                    "integration_baseline_worktree_path",
                ],
            )
            snapshot["attempts"].setdefault(attempt_id, {})[
                "runtime_session_id"
            ] = runtime_session_id
        elif event["event_type"] == "runtime_session_observed":
            runtime_session_id = payload["runtime_session_id"]
            session_state = snapshot["runtime_sessions"].setdefault(runtime_session_id, {})
            session_state["session_status"] = "observed"
            session_state["result_status"] = payload["result_status"]
            session_state["changed_file_count"] = payload["changed_file_count"]
        elif event["event_type"] == "runtime_session_stopped":
            runtime_session_id = payload["runtime_session_id"]
            session_state = snapshot["runtime_sessions"].setdefault(runtime_session_id, {})
            session_state["session_status"] = "stopped"
            session_state["result_status"] = payload["result_status"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "runtime_session_status"
            ] = "stopped"
        elif event["event_type"] == "runtime_output_received":
            snapshot["attempts"].setdefault(attempt_id, {})["attempt_status"] = payload[
                "result_status"
            ]
        elif event["event_type"] in {"validation_accepted", "validation_rejected"}:
            snapshot["attempts"].setdefault(attempt_id, {})["validation_status"] = payload[
                "validation_status"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["failure_category"] = payload.get(
                "failure_category"
            )
            snapshot["attempts"].setdefault(attempt_id, {})["retryable"] = payload.get(
                "retryable"
            )
            snapshot["attempts"].setdefault(attempt_id, {})["diff_audit"] = payload.get(
                "diff_audit"
            )
            snapshot["attempts"].setdefault(attempt_id, {})["patch_path"] = payload.get(
                "patch_path"
            )
            if lease_id in snapshot["leases"]:
                snapshot["leases"][lease_id]["lease_status"] = "released"
        elif event["event_type"] == "manual_gate_required":
            question_id = payload["question_id"]
            snapshot["manual_gates"][question_id] = {
                "question_id": question_id,
                "gate_status": "waiting",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "question": payload.get("question"),
                "options": payload.get("options", []),
                "reason": payload.get("reason"),
            }
            snapshot["tasks"].setdefault(task_id, {})["task_status"] = "blocked"
            snapshot["tasks"].setdefault(task_id, {})["blockers"] = [question_id]
        elif event["event_type"] == "operator_answer_received":
            question_id = payload["question_id"]
            gate = snapshot["manual_gates"].setdefault(
                question_id,
                {"question_id": question_id},
            )
            gate.update(
                {
                    "gate_status": "answered",
                    "answer": payload["answer"],
                    "operator": payload.get("operator"),
                    "answer_status": payload.get("answer_status"),
                }
            )
        elif event["event_type"] == "permission_request_required":
            request_id = payload["request_id"]
            snapshot["permission_requests"][request_id] = {
                "request_id": request_id,
                "request_status": "waiting",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "request_type": payload.get("request_type"),
                "requested_capability": payload.get("requested_capability"),
                "reason": payload.get("reason"),
                "scope": payload.get("scope"),
                "sandbox": payload.get("sandbox"),
                "command": payload.get("command"),
            }
            snapshot["tasks"].setdefault(task_id, {})["task_status"] = "blocked"
            snapshot["tasks"].setdefault(task_id, {})["blockers"] = [request_id]
        elif event["event_type"] == "permission_request_resolved":
            request_id = payload["request_id"]
            request = snapshot["permission_requests"].setdefault(
                request_id,
                {"request_id": request_id},
            )
            request.update(
                {
                    "request_status": payload.get("permission_status"),
                    "operator": payload.get("operator"),
                    "reason": payload.get("reason"),
                    "requested_capability": payload.get("requested_capability"),
                }
            )
        elif event["event_type"] == "integration_queued":
            queue_status = payload.get("integration_queue_status", "pending")
            queue_item = _integration_queue_snapshot_item(snapshot, task_id, attempt_id)
            queue_item.update(
                {
                    "queue_status": queue_status,
                    "patch_path": payload.get("patch_path"),
                    "integration_queue_path": payload.get("integration_queue_path"),
                    "integration_status": "not_requested",
                    "integration_verification_status": "not_requested",
                    "integration_commit_status": "not_requested",
                }
            )
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_queue_status"
            ] = queue_status
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_queue_item_id"
            ] = queue_item["queue_item_id"]
        elif event["event_type"] == "patch_integrated":
            snapshot["attempts"].setdefault(attempt_id, {})["integration_status"] = payload[
                "integration_status"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["integration_branch"] = payload[
                "integration_branch"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["integration_worktree_path"] = payload[
                "integration_worktree_path"
            ]
            _copy_optional_payload_fields(
                snapshot["attempts"].setdefault(attempt_id, {}),
                payload,
                [
                    "integration_base_sha",
                    "integration_baseline_branch",
                    "integration_baseline_worktree_path",
                    "integration_baseline_head_sha",
                ],
            )
            queue_item = _integration_queue_snapshot_item(snapshot, task_id, attempt_id)
            queue_item.update(
                {
                    "queue_status": "applied",
                    "patch_path": payload.get("patch_path", queue_item.get("patch_path")),
                    "integration_status": payload["integration_status"],
                    "integration_branch": payload["integration_branch"],
                    "integration_worktree_path": payload["integration_worktree_path"],
                }
            )
            _copy_optional_payload_fields(
                queue_item,
                payload,
                [
                    "integration_base_sha",
                    "integration_baseline_branch",
                    "integration_baseline_worktree_path",
                    "integration_baseline_head_sha",
                ],
            )
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_queue_status"
            ] = "applied"
        elif event["event_type"] == "integration_verified":
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_verification_status"
            ] = payload["integration_verification_status"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_verification_exit_code"
            ] = payload["integration_verification_exit_code"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_verification_stdout"
            ] = payload["integration_verification_stdout"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_verification_stderr"
            ] = payload["integration_verification_stderr"]
            queue_status = (
                "verified"
                if payload["integration_verification_status"] == "passed"
                else "blocked"
            )
            queue_item = _integration_queue_snapshot_item(snapshot, task_id, attempt_id)
            queue_item.update(
                {
                    "queue_status": queue_status,
                    "integration_verification_status": payload[
                        "integration_verification_status"
                    ],
                    "integration_verification_exit_code": payload[
                        "integration_verification_exit_code"
                    ],
                }
            )
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_queue_status"
            ] = queue_status
        elif event["event_type"] == "integration_baseline_commit_evaluated":
            attempt_state = snapshot["attempts"].setdefault(attempt_id, {})
            _copy_optional_payload_fields(
                attempt_state,
                payload,
                [
                    "integration_baseline_commit_status",
                    "integration_baseline_commit_sha",
                    "integration_baseline_commit_message",
                    "integration_baseline_commit_reason",
                    "integration_baseline_rollback_status",
                ],
            )
            queue_item = _existing_integration_queue_snapshot_item(
                snapshot,
                task_id,
                attempt_id,
            )
            if queue_item:
                _copy_optional_payload_fields(
                    queue_item,
                    payload,
                    [
                        "integration_baseline_commit_status",
                        "integration_baseline_commit_sha",
                        "integration_baseline_commit_message",
                        "integration_baseline_commit_reason",
                        "integration_baseline_rollback_status",
                    ],
                )
        elif event["event_type"] == "integration_commit_evaluated":
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_commit_status"
            ] = payload["integration_commit_status"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_commit_sha"
            ] = payload["integration_commit_sha"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_commit_message"
            ] = payload["integration_commit_message"]
            snapshot["attempts"].setdefault(attempt_id, {})[
                "integration_commit_reason"
            ] = payload["integration_commit_reason"]
            queue_status = _queue_status_from_commit_event(payload)
            queue_item = _existing_integration_queue_snapshot_item(
                snapshot,
                task_id,
                attempt_id,
            )
            if queue_item:
                queue_item.update(
                    {
                        "integration_commit_status": payload["integration_commit_status"],
                        "integration_commit_sha": payload["integration_commit_sha"],
                        "integration_commit_message": payload["integration_commit_message"],
                        "integration_commit_reason": payload["integration_commit_reason"],
                    }
                )
                if queue_status:
                    queue_item["queue_status"] = queue_status
                    snapshot["attempts"].setdefault(attempt_id, {})[
                        "integration_queue_status"
                    ] = queue_status
        elif event["event_type"] == "backlog_updated":
            snapshot["tasks"].setdefault(task_id, {})["task_status"] = payload["task_status"]
            if "blockers" in payload:
                snapshot["tasks"].setdefault(task_id, {})["blockers"] = payload["blockers"]

    return snapshot


def _integration_queue_snapshot_item(snapshot, task_id, attempt_id):
    queue_item_id = f"{task_id}:{attempt_id}"
    return snapshot["integration_queue"].setdefault(
        queue_item_id,
        {
            "queue_item_id": queue_item_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "queue_status": "pending",
        },
    )


def _existing_integration_queue_snapshot_item(snapshot, task_id, attempt_id):
    return snapshot["integration_queue"].get(f"{task_id}:{attempt_id}")


def _copy_optional_payload_fields(target, payload, field_names):
    for field_name in field_names:
        value = payload.get(field_name)
        if value is not None:
            target[field_name] = value


def _queue_status_from_commit_event(payload):
    if payload["integration_commit_status"] == "committed":
        return "committed"
    if payload["integration_commit_status"] == "failed":
        return "blocked"
    return None


def _select_ready_task(backlog):
    for task in backlog["items"]:
        if task["backlog_status"] == "ready" and not task.get("blockers"):
            return task
    raise ValueError("no ready task found")


def _find_idle_agent(agent_pool, role):
    for agent in agent_pool["agents"]:
        if agent["role"] == role and agent["status"] == "idle":
            return agent
    raise ValueError(f"no idle agent found for role {role}")


def _resolve_runtime_adapter(
    runtime_adapter,
    runtime_adapter_factory,
    agent,
    task,
    agent_pool=None,
    project_root=None,
    runtime_profile_defaults=None,
):
    return _resolve_runtime_adapter_selection(
        runtime_adapter,
        runtime_adapter_factory,
        agent,
        task,
        agent_pool=agent_pool,
        project_root=project_root,
        runtime_profile_defaults=runtime_profile_defaults,
    )["runtime_adapter"]


def _resolve_runtime_adapter_selection(
    runtime_adapter,
    runtime_adapter_factory,
    agent,
    task,
    agent_pool=None,
    project_root=None,
    runtime_profile_defaults=None,
):
    if runtime_adapter_factory:
        return {
            "runtime_adapter": runtime_adapter_factory(agent, task)
            or FakeRuntimeAdapter(),
            "runtime_profile_source": "runtime_adapter_factory",
        }
    if runtime_adapter:
        return {
            "runtime_adapter": runtime_adapter,
            "runtime_profile_source": "explicit_runtime_adapter",
        }
    profile = agent.get("runtime_profile")
    if profile:
        return {
            "runtime_adapter": _runtime_adapter_from_profile(
                profile,
                defaults=runtime_profile_defaults,
                project_root=project_root,
            )
            or FakeRuntimeAdapter(),
            "runtime_profile_source": "agent_runtime_profile",
        }
    role_profile = _role_runtime_profile(agent_pool, agent)
    if role_profile:
        return {
            "runtime_adapter": _runtime_adapter_from_profile(
                role_profile,
                defaults=runtime_profile_defaults,
                project_root=project_root,
            )
            or FakeRuntimeAdapter(),
            "runtime_profile_source": "role_runtime_profile",
        }
    if runtime_profile_defaults:
        return {
            "runtime_adapter": _runtime_adapter_from_profile(
                runtime_profile_defaults,
                defaults={},
                project_root=project_root,
            )
            or FakeRuntimeAdapter(),
            "runtime_profile_source": "runtime_profile_defaults",
        }
    return {
        "runtime_adapter": FakeRuntimeAdapter(),
        "runtime_profile_source": "default_fake",
    }


def _role_runtime_profile(agent_pool, agent):
    if not agent_pool:
        return None
    role_profiles = agent_pool.get("role_runtime_profiles", {})
    if not isinstance(role_profiles, dict):
        raise ValueError("role_runtime_profiles must be an object")
    return role_profiles.get(agent.get("role"))


def _role_prompt_fields(agent_pool, agent, task):
    fields = {
        "agent_role": agent.get("role"),
        "required_role": task.get("required_role"),
    }
    contract = _role_prompt_contract(agent_pool, agent)
    if contract:
        fields["role_prompt_contract"] = contract
    return fields


def _role_prompt_contract(agent_pool, agent):
    if not agent_pool:
        return None
    contracts = agent_pool.get("role_prompt_contracts", {})
    if not isinstance(contracts, dict):
        raise ValueError("role_prompt_contracts must be an object")
    contract = contracts.get(agent.get("role"))
    if contract and not isinstance(contract, dict):
        raise ValueError("role prompt contract must be an object")
    return deepcopy(contract) if contract else None


def _role_context_fields(agent_pool, agent, output_dir, attempt_id, project_root=None):
    package = _role_context_package(agent_pool, agent)
    if not package:
        return {}
    context_path = _write_role_context_package(
        output_dir,
        attempt_id,
        agent,
        package,
        project_root=project_root,
    )
    return {
        "role_context_path": str(context_path),
        "role_context_schema_version": "role_context.v1",
    }


def _message_context_event_fields(payload):
    fields = {}
    if payload.get("repo_context_path"):
        fields["repo_context_path"] = payload["repo_context_path"]
    if payload.get("repo_context_schema_version"):
        fields["repo_context_schema_version"] = payload[
            "repo_context_schema_version"
        ]
    return fields


def _repo_context_fields(project_root, output_dir, task, agent, attempt_id):
    if not project_root:
        return {}
    context = build_repo_context(
        project_root,
        output_dir,
        task,
        agent_role=agent.get("role"),
        context_id=attempt_id,
    )
    return {
        "repo_context_path": context["repo_context_path"],
        "repo_context_schema_version": REPO_CONTEXT_SCHEMA_VERSION,
    }


def _role_context_package(agent_pool, agent):
    if not agent_pool:
        return None
    packages = agent_pool.get("role_context_packages", {})
    if not isinstance(packages, dict):
        raise ValueError("role_context_packages must be an object")
    package = packages.get(agent.get("role"))
    if package and not isinstance(package, dict):
        raise ValueError("role context package must be an object")
    return deepcopy(package) if package else None


def _write_role_context_package(
    output_dir,
    attempt_id,
    agent,
    package,
    project_root=None,
):
    context_notes = _role_context_string_list(package, "context_notes")
    artifact_paths = _role_context_string_list(package, "context_artifacts")
    context = {
        "context_schema_version": "role_context.v1",
        "agent_id": agent.get("agent_id"),
        "agent_role": agent.get("role"),
        "context_notes": context_notes,
    }
    repo_map_reference = _role_context_repo_map_reference(
        package,
        project_root,
        output_dir,
    )
    if repo_map_reference:
        context["repo_map_reference"] = repo_map_reference
    if artifact_paths:
        context["artifact_context"] = build_artifact_context(
            artifact_paths,
            excerpt_chars=package.get("excerpt_chars", 1200),
        )
    context_path = (
        Path(output_dir)
        / "role_contexts"
        / f"{attempt_id}-{_safe_role_context_name(agent.get('role'))}.json"
    )
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, sort_keys=True), encoding="utf-8")
    return context_path


def _role_context_repo_map_reference(package, project_root, output_dir):
    include_references = package.get("include_repo_map_references", False)
    if not isinstance(include_references, bool):
        raise ValueError(
            "role context package include_repo_map_references must be a boolean"
        )
    if not include_references or not project_root:
        return None
    repo_map = build_repository_map(project_root, output_dir)
    return {
        "boundary": "navigation_reference_only",
        "manifest_path": repo_map["paths"]["manifest_path"],
        "inventory_path": repo_map["paths"]["inventory_path"],
        "symbols_path": repo_map["paths"]["symbols_path"],
        "read_policy": (
            "Use repo map paths for coarse repository navigation only. "
            "For task-specific implementation file selection, read "
            "repo_context_path from the dispatch payload when present."
        ),
    }


def _role_context_string_list(package, key):
    values = package.get(key, [])
    if values is None:
        return []
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value
        for value in values
    ):
        raise ValueError(f"role context package {key} must be a string array")
    return list(values)


def _safe_role_context_name(role):
    role = str(role or "agent")
    return "".join(
        character
        if character.isalnum() or character in {"-", "_"}
        else "_"
        for character in role
    )


def _runtime_adapter_from_profile(profile, defaults=None, project_root=None):
    defaults = defaults or {}
    if not isinstance(profile, dict):
        raise ValueError("runtime_profile must be an object")
    adapter = profile.get("adapter", "fake")
    if adapter == "fake":
        return None
    if adapter == "shell":
        command = profile.get("command") or defaults.get("command")
        if not _is_string_list(command):
            raise ValueError("shell runtime_profile requires command as a string array")
        return ShellRuntimeAdapter(command)
    if adapter == "codex":
        if not project_root:
            raise ValueError("project_root is required for Codex runtime_profile")
        command = defaults.get("command") or profile.get("command")
        if command is not None and not _is_string_list(command):
            raise ValueError("codex runtime_profile command must be a string array")
        timeout_seconds = profile.get(
            "timeout_seconds",
            defaults.get("timeout_seconds", 300),
        )
        if not isinstance(timeout_seconds, int) or timeout_seconds < 1:
            raise ValueError("codex runtime_profile timeout_seconds must be an integer >= 1")
        return CodexRuntimeAdapter(
            command=command or None,
            model=profile.get("model", defaults.get("model")),
            sandbox=profile.get("sandbox", defaults.get("sandbox", "workspace-write")),
            timeout_seconds=timeout_seconds,
            fallback_worktree_path=profile.get(
                "fallback_worktree_path",
                defaults.get("fallback_worktree_path", project_root),
            ),
        )
    raise ValueError(f"unsupported runtime_profile adapter: {adapter}")


def _is_string_list(value):
    return isinstance(value, list) and value and all(isinstance(part, str) for part in value)


def _runtime_adapter_metadata(runtime_adapter):
    return {
        "runtime_adapter": runtime_adapter.__class__.__name__,
        "runtime_model": getattr(runtime_adapter, "model", None),
        "runtime_sandbox": getattr(runtime_adapter, "sandbox", None),
        "runtime_timeout_seconds": getattr(runtime_adapter, "timeout_seconds", None),
    }


def _bind_runtime_adapter_output_dir(runtime_adapter, output_dir):
    binder = getattr(runtime_adapter, "bind_output_dir", None)
    if not binder:
        return runtime_adapter
    return binder(output_dir)


def _fake_changed_files(write_scope):
    if not write_scope:
        return []
    return [f"{write_scope[0].rstrip('/')}/m0_generated_repo_index.json"]


def _fake_operator_summary(payload, changed_files):
    deliverables = [
        {
            "deliverable": deliverable,
            "summary": f"Fake runtime 已满足 {deliverable}。",
            "evidence": changed_files or ["未生成变更文件"],
        }
        for deliverable in _coerce_text_list(payload.get("required_deliverables"))
    ]
    return {
        "what_changed": [
            "Fake runtime 为任务生成了确定性的有界结果。"
        ],
        "verification_summary": ["fake_runtime: passed"],
        "deliverables": deliverables,
        "merge_recommendation": "人工审阅通过后再合并已接受的补丁。",
        "next_steps": [],
    }


def _fake_evidence_summary(payload, changed_files):
    policy = payload.get("evidence_policy")
    evidence_level = "L1"
    if isinstance(policy, dict) and policy.get("evidence_level"):
        evidence_level = policy["evidence_level"]
    return {
        "evidence_level": evidence_level,
        "evidence_status": "complete",
        "trace_carrier": [
            {
                "kind": "fake_runtime",
                "path": changed_files[0] if changed_files else "no_patch",
                "summary": "Fake runtime produced deterministic test evidence.",
            }
        ],
        "missing_evidence": [],
    }


def _scoped_attempt_id(attempt_number, attempt_id_prefix=None):
    return _scoped_id("ATTEMPT", attempt_number, attempt_id_prefix)


def _scoped_id(kind, number, id_prefix=None, width=3):
    local_id = f"{kind}-{number:0{width}d}"
    if not id_prefix:
        return local_id
    safe_prefix = str(id_prefix).replace("/", "-")
    return f"{safe_prefix}-{local_id}"


def _changed_files_in_scope(changed_files, task):
    write_scope = [scope for scope in task.get("write_scope", []) if scope]
    return all(
        any(_path_matches_write_scope(path, scope) for scope in write_scope)
        for path in changed_files
    )


def _path_matches_write_scope(path, scope):
    if path == scope:
        return True
    return path.startswith(scope.rstrip("/") + "/")


def _validate_runtime_result(runtime_result, task):
    if runtime_result["result_status"] != "completed":
        return "rejected"
    return "accepted" if _changed_files_in_scope(runtime_result["changed_files"], task) else "rejected"


def classify_attempt_outcome(runtime_result, task, diff_audit=None):
    validation_status = _validate_runtime_result(runtime_result, task)
    if validation_status == "accepted" and diff_audit and diff_audit["diff_status"] != "matched":
        return {
            "validation_status": "rejected",
            "failure_category": "diff_mismatch",
            "retryable": False,
        }
    if validation_status == "accepted":
        semantic_validation = _semantic_deliverable_validation(runtime_result, task)
        if semantic_validation["missing_required_deliverables"]:
            return {
                "validation_status": "rejected",
                "failure_category": "missing_required_deliverables",
                "retryable": False,
                "semantic_validation": semantic_validation,
            }
        return {
            "validation_status": "accepted",
            "failure_category": None,
            "retryable": False,
            "semantic_validation": semantic_validation,
        }
    if runtime_result["result_status"] == "timed_out":
        return {
            "validation_status": "rejected",
            "failure_category": "timeout",
            "retryable": True,
        }
    if runtime_result["result_status"] == "completed":
        return {
            "validation_status": "rejected",
            "failure_category": "scope_violation",
            "retryable": False,
        }
    if runtime_result["result_status"] in {"blocked", "cancelled"}:
        return {
            "validation_status": "rejected",
            "failure_category": runtime_result["result_status"],
            "retryable": False,
        }
    return {
        "validation_status": "rejected",
        "failure_category": "runtime_error",
        "retryable": True,
    }


def _semantic_deliverable_validation(runtime_result, task):
    required = _coerce_text_list(task.get("required_deliverables"))
    delivered = _delivered_deliverable_names(runtime_result)
    return {
        "required_deliverables": required,
        "delivered_deliverables": sorted(delivered),
        "missing_required_deliverables": [
            deliverable for deliverable in required if deliverable not in delivered
        ],
    }


def _delivered_deliverable_names(runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    if not isinstance(output, dict):
        return set()
    operator_summary = output.get("operator_summary")
    if not isinstance(operator_summary, dict):
        operator_summary = {}
    deliverables = operator_summary.get("deliverables") or output.get("deliverables")
    if isinstance(deliverables, dict):
        return {str(key) for key, value in deliverables.items() if value}
    if isinstance(deliverables, list):
        names = set()
        for item in deliverables:
            if isinstance(item, dict):
                name = item.get("deliverable") or item.get("name") or item.get("id")
                if name and (item.get("summary") or item.get("evidence") or item.get("status")):
                    names.add(str(name))
            elif item:
                names.add(str(item))
        return names
    return set()


def _coerce_text_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None and str(item)]
    if isinstance(value, tuple):
        return [str(item) for item in value if item is not None and str(item)]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


def audit_worktree_diff(worktree_path, declared_changed_files):
    declared = sorted(set(declared_changed_files))
    actual = sorted(set(_git_changed_files(worktree_path)))
    missing = sorted(path for path in declared if path not in actual)
    undeclared = sorted(path for path in actual if path not in declared)
    return {
        "diff_status": "matched" if not missing and not undeclared else "mismatch",
        "declared_changed_files": declared,
        "actual_changed_files": actual,
        "missing_declared_files": missing,
        "undeclared_changed_files": undeclared,
    }


def write_patch_artifact(worktree_path, artifact_dir, actual_changed_files):
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifact_dir / "worktree.patch"
    status_entries = _git_status_entries(worktree_path)
    untracked = {
        entry["path"]
        for entry in status_entries
        if entry["status"] == "??"
    }
    tracked_paths = [path for path in actual_changed_files if path not in untracked]
    chunks = []
    if tracked_paths:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "--binary",
                "--no-ext-diff",
                "HEAD",
                "--",
                *tracked_paths,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.stdout:
            chunks.append(completed.stdout)
    for path in actual_changed_files:
        if path not in untracked:
            continue
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-index",
                "--",
                "/dev/null",
                path,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(
                completed.returncode,
                completed.args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        if completed.stdout:
            chunks.append(completed.stdout)
    patch_path.write_text("\n".join(chunks), encoding="utf-8")
    return patch_path


def ensure_integration_baseline_worktree(project_root, output_dir):
    integration_branch = _integration_baseline_branch_name(output_dir)
    integration_worktree = Path(output_dir) / "integration-baseline"
    integration_worktree.parent.mkdir(parents=True, exist_ok=True)
    recovery_status = "created"
    if integration_worktree.exists():
        recovery_status = "reused_existing"
    elif _git_ref_exists(project_root, integration_branch):
        recovery_status = "reused_branch"
        subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "worktree",
                "add",
                str(integration_worktree),
                integration_branch,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    else:
        subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "worktree",
                "add",
                "-b",
                integration_branch,
                str(integration_worktree),
                "HEAD",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    return {
        "integration_baseline_status": "ready",
        "integration_baseline_branch": integration_branch,
        "integration_baseline_worktree_path": str(integration_worktree),
        "integration_baseline_recovery_status": recovery_status,
        "integration_baseline_head_sha": _git_rev_parse(integration_worktree, "HEAD"),
    }


def apply_patch_to_integration_baseline_worktree(project_root, output_dir, patch_path):
    baseline = ensure_integration_baseline_worktree(project_root, output_dir)
    integration_worktree = Path(baseline["integration_baseline_worktree_path"])
    recovery_status = baseline["integration_baseline_recovery_status"]
    if _git_apply_check(integration_worktree, patch_path):
        subprocess.run(
            ["git", "-C", str(integration_worktree), "apply", str(patch_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    elif _git_apply_reverse_check(integration_worktree, patch_path):
        recovery_status = (
            "already_applied"
            if recovery_status == "created"
            else "reused_existing"
        )
    else:
        subprocess.run(
            ["git", "-C", str(integration_worktree), "apply", str(patch_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    return {
        **baseline,
        "integration_status": "applied",
        "integration_branch": baseline["integration_baseline_branch"],
        "integration_worktree_path": str(integration_worktree),
        "integration_recovery_status": recovery_status,
        "integration_base_sha": baseline["integration_baseline_head_sha"],
    }


def commit_integration_baseline_worktree(integration_worktree_path, task_id, attempt_id):
    commit = commit_integration_worktree(integration_worktree_path, task_id, attempt_id)
    sha = commit["integration_commit_sha"] or _git_rev_parse(
        integration_worktree_path,
        "HEAD",
    )
    return {
        "integration_baseline_commit_status": commit["integration_commit_status"],
        "integration_baseline_commit_sha": sha,
        "integration_baseline_commit_message": commit["integration_commit_message"],
        "integration_baseline_commit_reason": commit["integration_commit_reason"],
        "integration_baseline_commit_stdout": commit["integration_commit_stdout"],
        "integration_baseline_commit_stderr": commit["integration_commit_stderr"],
    }


def skip_integration_baseline_commit(reason):
    return {
        "integration_baseline_commit_status": "skipped",
        "integration_baseline_commit_sha": None,
        "integration_baseline_commit_message": None,
        "integration_baseline_commit_reason": reason,
        "integration_baseline_commit_stdout": "",
        "integration_baseline_commit_stderr": "",
    }


def reset_integration_baseline_worktree(integration_worktree_path):
    integration_worktree_path = Path(integration_worktree_path)
    reset = subprocess.run(
        ["git", "-C", str(integration_worktree_path), "reset", "--hard", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    clean = subprocess.run(
        ["git", "-C", str(integration_worktree_path), "clean", "-fd"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status = "reset" if reset.returncode == 0 and clean.returncode == 0 else "failed"
    return {
        "integration_baseline_rollback_status": status,
        "integration_baseline_rollback_stdout": reset.stdout + clean.stdout,
        "integration_baseline_rollback_stderr": reset.stderr + clean.stderr,
    }


def apply_patch_to_integration_worktree(project_root, output_dir, task_id, patch_path):
    integration_branch = f"agentteam/integration/{task_id}"
    integration_worktree = Path(output_dir) / "integration" / task_id
    integration_worktree.parent.mkdir(parents=True, exist_ok=True)
    recovery_status = "created"
    if integration_worktree.exists():
        recovery_status = "reused_existing"
    else:
        subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "worktree",
                "add",
                "-b",
                integration_branch,
                str(integration_worktree),
                "HEAD",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    if _git_apply_check(integration_worktree, patch_path):
        subprocess.run(
            ["git", "-C", str(integration_worktree), "apply", str(patch_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    elif _git_apply_reverse_check(integration_worktree, patch_path):
        recovery_status = (
            "already_applied"
            if recovery_status == "created"
            else "reused_existing"
        )
    else:
        subprocess.run(
            ["git", "-C", str(integration_worktree), "apply", str(patch_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    return {
        "integration_status": "applied",
        "integration_branch": integration_branch,
        "integration_worktree_path": str(integration_worktree),
        "integration_recovery_status": recovery_status,
    }


def _git_apply_check(worktree, patch_path):
    completed = subprocess.run(
        ["git", "-C", str(worktree), "apply", "--check", str(patch_path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0


def _git_apply_reverse_check(worktree, patch_path):
    completed = subprocess.run(
        ["git", "-C", str(worktree), "apply", "--reverse", "--check", str(patch_path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0


def run_integration_verification(command, integration_worktree_path):
    env = os.environ.copy()
    native_runtime_path = _native_runtime_pythonpath(integration_worktree_path)
    if native_runtime_path:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            native_runtime_path
            if not existing
            else native_runtime_path + os.pathsep + existing
        )
    completed = subprocess.run(
        list(command),
        cwd=integration_worktree_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return {
        "integration_verification_status": "passed" if completed.returncode == 0 else "failed",
        "integration_verification_exit_code": completed.returncode,
        "integration_verification_stdout": completed.stdout,
        "integration_verification_stderr": completed.stderr,
    }


def _native_runtime_pythonpath(integration_worktree_path):
    runtime_root = (
        Path(integration_worktree_path)
        / "experiments"
        / "native_agentteam_runtime"
        / "m0_runtime"
    )
    if (runtime_root / "agentteam_runtime" / "__init__.py").is_file():
        return str(runtime_root.resolve())
    return None


def evaluate_integration_commit(attempt, task_id, attempt_id):
    if attempt["integration_status"] != "applied" or not attempt["integration_worktree_path"]:
        return _integration_commit_result("skipped", reason="integration_not_applied")

    verification_status = attempt["integration_verification_status"]
    if verification_status == "not_requested":
        return _integration_commit_result("skipped", reason="verification_not_requested")
    if verification_status != "passed":
        return _integration_commit_result("skipped", reason="verification_failed")

    return commit_integration_worktree(
        attempt["integration_worktree_path"],
        task_id,
        attempt_id,
    )


def commit_integration_worktree(integration_worktree_path, task_id, attempt_id):
    if not _git_changed_files(integration_worktree_path):
        return _integration_commit_result("skipped", reason="no_changes")

    message = f"AgentTeam integration {task_id} {attempt_id}"
    subprocess.run(
        ["git", "-C", str(integration_worktree_path), "add", "--all"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    completed = subprocess.run(
        ["git", "-C", str(integration_worktree_path), "commit", "-m", message],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        return _integration_commit_result(
            "failed",
            reason="git_commit_failed",
            message=message,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    rev_parse = subprocess.run(
        ["git", "-C", str(integration_worktree_path), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return _integration_commit_result(
        "committed",
        sha=rev_parse.stdout.strip(),
        message=message,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _integration_commit_result(
    status,
    sha=None,
    reason=None,
    message=None,
    stdout="",
    stderr="",
):
    return {
        "integration_commit_status": status,
        "integration_commit_sha": sha,
        "integration_commit_message": message,
        "integration_commit_reason": reason,
        "integration_commit_stdout": stdout,
        "integration_commit_stderr": stderr,
    }


def _git_changed_files(worktree_path):
    return [entry["path"] for entry in _git_status_entries(worktree_path)]


def _git_status_signature(worktree_path):
    return sorted(
        (entry["status"], entry["path"])
        for entry in _git_status_entries(worktree_path)
    )


def _git_status_entries(worktree_path):
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    entries = []
    for line in completed.stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(".agentteam/"):
            continue
        entries.append({"status": status, "path": path})
    return entries


def _normalize_runtime_result(result, adapter, stderr=""):
    changed_files = result.get("changed_files", [])
    if not isinstance(changed_files, list) or not all(isinstance(path, str) for path in changed_files):
        return {
            "result_status": "failed",
            "changed_files": [],
            "output": {
                "adapter": adapter,
                "error": "invalid_changed_files",
                "stderr": stderr,
            },
        }
    normalized = {
        "result_status": result.get("result_status", "failed"),
        "changed_files": changed_files,
        "output": result.get("output", {}),
    }
    if isinstance(result.get("token_usage"), dict):
        normalized["token_usage"] = result["token_usage"]
    elif isinstance(result.get("usage"), dict):
        normalized["usage"] = result["usage"]
    return normalized


def _event(
    sequence,
    time,
    event_type,
    actor,
    target_agent_id,
    idempotency_key,
    correlation_id,
    payload,
):
    return {
        "event_id": f"EVT-{sequence:04d}",
        "sequence": sequence,
        "time": time,
        "event_type": event_type,
        "actor": actor,
        "target_agent_id": target_agent_id,
        "idempotency_key": idempotency_key,
        "correlation_id": correlation_id,
        "payload": payload,
    }


def _create_git_worktree(project_root, output_dir, attempt_id, worktree_id, base_ref=None):
    project_root = Path(project_root)
    worktree_path = Path(output_dir) / "worktrees" / worktree_id
    branch = _worktree_branch_name(output_dir, attempt_id)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            base_ref or "HEAD",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return worktree_path, branch


def _worktree_branch_name(output_dir, attempt_id):
    output_dir = Path(output_dir)
    if output_dir.parent.name == "steps" and output_dir.parent.parent.name:
        run_component = output_dir.parent.parent.name
    else:
        run_component = output_dir.name
    return "agentteam/{run}/{attempt}".format(
        run=_safe_git_ref_component(run_component),
        attempt=_safe_git_ref_component(attempt_id),
    )


def _integration_baseline_branch_name(output_dir):
    output_dir = Path(output_dir)
    return "agentteam/run/{run}/integration".format(
        run=_safe_git_ref_component(output_dir.name),
    )


def _safe_git_ref_component(value):
    component = "".join(
        char if char.isalnum() or char in {".", "_", "-"} else "-"
        for char in str(value)
    )
    component = component.strip(".-")
    return component or "run"


def _git_ref_exists(repo, ref):
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0


def _git_rev_parse(repo, ref):
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _remove_git_worktree(project_root, worktree_path):
    subprocess.run(
        [
            "git",
            "-C",
            str(project_root),
            "worktree",
            "remove",
            "--force",
            str(worktree_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _append_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _read_jsonl(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)

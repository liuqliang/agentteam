import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .m0_runtime import (
    SystemClock,
    _append_jsonl,
    _create_git_worktree,
    _event,
    _find_idle_agent,
    _message_context_event_fields,
    _read_json,
    _repo_context_fields,
    _role_context_fields,
    _role_prompt_fields,
    _runtime_adapter_metadata,
    _scoped_id,
    apply_patch_to_integration_worktree,
    audit_worktree_diff,
    classify_attempt_outcome,
    evaluate_integration_commit,
    rebuild_sqlite_state_index,
    run_integration_verification,
    write_patch_artifact,
)
from .integration_queue import integration_queue_path, upsert_integration_queue_item
from .notifications import DEFAULT_NOTIFICATION_EVENT_TYPES
from .planner_context import build_planner_context
from .task_proposal import normalize_task_proposal
from .token_usage import aggregate_token_usage, token_usage_from_result


class TwoPhaseFileScheduler:
    def __init__(
        self,
        agent_pool_path,
        backlog_path,
        output_dir,
        clock=None,
        project_root=None,
        runtime_adapter=None,
        max_inflight=2,
        max_attempts=1,
        lease_timeout_seconds=900,
        integrate_accepted_patch=False,
        integration_verification_command=None,
        commit_verified_integration=False,
        state_path=None,
        auto_decompose=False,
        decomposition_milestone_id="M21",
        decomposition_planner_role="task_planner",
        decomposition_default_worker_role="repo_map_agent",
        decomposition_allowed_read_scopes=None,
        decomposition_allowed_write_scopes=None,
        decomposition_context_artifact_paths=None,
        decomposition_context_excerpt_chars=1200,
        decomposition_max_waves=1,
        unavailable_agent_ids=None,
        notification_sink=None,
    ):
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if lease_timeout_seconds < 0:
            raise ValueError("lease_timeout_seconds must be at least 0")
        if decomposition_max_waves < 1:
            raise ValueError("decomposition_max_waves must be at least 1")
        self.agent_pool_path = Path(agent_pool_path)
        self.backlog_path = Path(backlog_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.project_root = Path(project_root) if project_root else None
        self.runtime_adapter = runtime_adapter
        self.max_inflight = max_inflight
        self.max_attempts = max_attempts
        self.lease_timeout_seconds = lease_timeout_seconds
        self.integrate_accepted_patch = integrate_accepted_patch
        self.integration_verification_command = integration_verification_command
        self.commit_verified_integration = commit_verified_integration
        self.auto_decompose = auto_decompose
        self.decomposition_milestone_id = decomposition_milestone_id
        self.decomposition_planner_role = decomposition_planner_role
        self.decomposition_default_worker_role = decomposition_default_worker_role
        self.decomposition_allowed_read_scopes = list(
            decomposition_allowed_read_scopes or ["."]
        )
        self.decomposition_allowed_write_scopes = list(
            decomposition_allowed_write_scopes or ["generated/"]
        )
        self.decomposition_context_artifact_paths = list(
            decomposition_context_artifact_paths or []
        )
        self.decomposition_context_excerpt_chars = decomposition_context_excerpt_chars
        self.decomposition_max_waves = decomposition_max_waves
        self.unavailable_agent_ids = set(unavailable_agent_ids or [])
        self.notification_sink = notification_sink
        self.state_path = Path(
            state_path or self.output_dir / "state" / "two_phase_scheduler_state.json"
        )
        self.state_db_path = self.output_dir / "state" / "scheduler_state.sqlite"
        self.events_path = self.output_dir / "events.jsonl"
        self.run_id = "RUN-TWO-PHASE-SCHEDULER"
        self.state = self._load_or_create_state()

    def dispatch_ready(self):
        self._ensure_decomposition_task()
        capacity = self.max_inflight - len(self.state["inflight_attempts"])
        if capacity <= 0:
            self.state["scheduler_status"] = "waiting"
            self._write_state()
            return {
                "dispatch_status": "at_capacity",
                "dispatched_task_ids": [],
                "dispatch_count": 0,
                "inflight_count": len(self.state["inflight_attempts"]),
            }

        agent_pool = _read_json(self.agent_pool_path)
        self._mark_inflight_agents_busy(agent_pool)
        self._mark_unavailable_agents(agent_pool)
        dispatched = []
        for task in self._ready_tasks():
            if len(dispatched) >= capacity:
                break
            try:
                dispatch = self._dispatch_task(agent_pool, task)
            except ValueError as exc:
                if str(exc).startswith("no idle agent found for role "):
                    continue
                raise
            dispatched.append(dispatch)

        self.state["scheduler_status"] = "running" if dispatched else self._status_without_dispatch()
        self._write_state()
        if dispatched:
            rebuild_sqlite_state_index(self.state_db_path, self.events_path)
        return {
            "dispatch_status": "dispatched" if dispatched else "idle",
            "dispatched_task_ids": [item["task_id"] for item in dispatched],
            "dispatch_count": len(dispatched),
            "inflight_count": len(self.state["inflight_attempts"]),
        }

    def collect_ready_results(self):
        collected = []
        remaining = []
        for inflight in self.state["inflight_attempts"]:
            result = _runtime_result_from_outbox(
                inflight["outbox_path"],
                inflight["message_id"],
            )
            if result is None:
                if not self._lease_expired(inflight):
                    remaining.append(inflight)
                    continue
                result = self._timeout_runtime_result(inflight)
            collected.append(self._collect_result(inflight, result))

        self.state["inflight_attempts"] = remaining
        self.state["scheduler_status"] = self._status_without_dispatch()
        self._write_state()
        if collected:
            rebuild_sqlite_state_index(self.state_db_path, self.events_path)
        return {
            "collect_status": "collected" if collected else "idle",
            "collected_task_ids": [item["task_id"] for item in collected],
            "collected_count": len(collected),
            "inflight_count": len(self.state["inflight_attempts"]),
            "results": collected,
        }

    def tick(self):
        collect = self.collect_ready_results()
        dispatch = self.dispatch_ready()
        if collect["collected_count"] or dispatch["dispatch_count"]:
            tick_status = "running"
        elif self.state["inflight_attempts"]:
            tick_status = "waiting"
        else:
            tick_status = "idle"
        return {
            "tick_status": tick_status,
            "collect": collect,
            "dispatch": dispatch,
            "inflight_count": len(self.state["inflight_attempts"]),
            "processed_task_ids": self.summary()["processed_task_ids"],
        }

    def set_unavailable_agent_ids(self, agent_ids):
        self.unavailable_agent_ids = set(agent_ids or [])

    def run_until_idle(self, max_ticks=100, poll_interval_seconds=0.02):
        if max_ticks < 1:
            raise ValueError("max_ticks must be at least 1")
        self._emit_run_event_once(
            "run_started",
            self._run_event_payload("running", {"max_ticks": max_ticks}),
        )
        tick_count = 0
        last_tick = None
        for _ in range(max_ticks):
            tick_count += 1
            last_tick = self.tick()
            if last_tick["tick_status"] == "idle":
                summary = {
                    **self.summary(),
                    "scheduler_status": "idle",
                    "tick_count": tick_count,
                    "last_tick": last_tick,
                }
                self._emit_run_event_once(
                    "run_completed",
                    self._run_event_payload("completed", {"tick_count": tick_count}),
                )
                return summary
            if last_tick["tick_status"] == "waiting":
                time.sleep(poll_interval_seconds)
        self.state["scheduler_status"] = "max_ticks_reached"
        self._write_state()
        self._emit_run_event_once(
            "run_timed_out",
            self._run_event_payload("max_ticks_reached", {"tick_count": tick_count}),
        )
        return {
            **self.summary(),
            "scheduler_status": "max_ticks_reached",
            "tick_count": tick_count,
            "last_tick": last_tick,
        }

    def summary(self):
        processed_task_ids = [
            step["task_id"]
            for step in self.state["steps"]
            if step["step_status"] == "processed"
        ]
        return {
            "scheduler_status": self.state["scheduler_status"],
            "processed_task_ids": processed_task_ids,
            "step_count": len(self.state["steps"]),
            "inflight_count": len(self.state["inflight_attempts"]),
            "max_attempts": self.state["max_attempts"],
            "lease_timeout_seconds": self.state["lease_timeout_seconds"],
            "steps": self.state["steps"],
            "events_path": str(self.events_path),
            "state_path": str(self.state_path),
            "state_db_path": str(self.state_db_path),
        }

    def _dispatch_task(self, agent_pool, task):
        step_id = self._next_step_id(task["task_id"])
        step_dir = self.output_dir / "steps" / step_id
        step_dir.mkdir(parents=True, exist_ok=True)
        step_backlog_path = step_dir / "backlog.json"
        step_backlog_path.write_text(
            json.dumps(
                {
                    "backlog_id": self.state["backlog"].get(
                        "backlog_id",
                        "BL-TWO-PHASE-STEP",
                    ),
                    "items": [deepcopy(task)],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        unavailable_agent_ids = self._unavailable_agent_ids_for_role(
            agent_pool,
            task["required_role"],
        )
        agent = _find_idle_agent(agent_pool, task["required_role"])
        attempt_number = self._next_attempt_number(task["task_id"])
        attempt_id = f"{task['task_id']}-ATTEMPT-{attempt_number:03d}"
        lease_id = f"{task['task_id']}-LEASE-{attempt_number:03d}"
        message_id = _scoped_id("MSG", attempt_number, task["task_id"], width=4)
        worktree_id = f"WT-{attempt_id}" if task.get("write_scope") else None
        runtime_session_id = f"SESSION-{attempt_id}"
        worktree_path = None
        branch = None
        correlation_id = f"{task['task_id']}:{attempt_id}"
        created_at = self.clock.now()
        lease_expires_at = _timestamp_after(
            created_at,
            self.state["lease_timeout_seconds"],
        )
        agent["status"] = "busy"
        agent["lease"] = {
            "lease_id": lease_id,
            "task_id": task["task_id"],
            "expires_at": lease_expires_at,
        }

        if self.project_root and worktree_id:
            worktree_path, branch = _create_git_worktree(
                self.project_root,
                step_dir,
                attempt_id,
                worktree_id,
            )

        message = {
            "message_id": message_id,
            "from_agent": agent_pool["scheduler_agent_id"],
            "to_agent": agent["agent_id"],
            "message_type": "dispatch_task",
            "correlation_id": correlation_id,
            "created_at": created_at,
            "lease_expires_at": lease_expires_at,
            "payload": {
                "task_id": task["task_id"],
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "worktree_id": worktree_id,
                "worktree_path": str(worktree_path) if worktree_path else None,
                "branch": branch,
                "task_kind": task.get("task_kind", "implementation"),
                "milestone_id": task.get("milestone_id"),
                "default_worker_role": task.get("default_worker_role"),
                "planner_context_path": task.get("planner_context_path"),
                "objective": task["objective"],
                "goal_alignment": task.get("goal_alignment"),
                "required_deliverables": task.get("required_deliverables", []),
                "read_scope": task["read_scope"],
                "write_scope": task["write_scope"],
                **_operator_guidance_fields(task),
                **_permission_grant_fields(task),
                **_role_prompt_fields(agent_pool, agent, task),
                **_role_context_fields(
                    agent_pool,
                    agent,
                    step_dir,
                    attempt_id,
                    project_root=self.project_root,
                ),
                **_repo_context_fields(
                    self.project_root,
                    self.output_dir,
                    task,
                    agent,
                    attempt_id,
                ),
            },
        }
        inbox_path = step_dir / agent["inbox_path"]
        _append_jsonl(inbox_path, [message])

        if self.runtime_adapter:
            metadata = {
                **_runtime_adapter_metadata(self.runtime_adapter),
                "runtime_profile_source": "explicit_runtime_adapter",
            }
        else:
            metadata = {
                "runtime_adapter": "FileMailboxExternalRuntimeAdapter",
                "runtime_model": None,
                "runtime_sandbox": None,
                "runtime_timeout_seconds": None,
                "runtime_profile_source": "external_mailbox_adapter",
            }
        self._append_events(
            step_id,
            [
                self._event(
                    "task_selected",
                    agent_pool["scheduler_agent_id"],
                    None,
                    f"select:{task['task_id']}:{attempt_id}",
                    correlation_id,
                    {"task_id": task["task_id"], "attempt_id": attempt_id},
                ),
                *(
                    [
                        self._event(
                            "task_reassigned",
                            agent_pool["scheduler_agent_id"],
                            agent["agent_id"],
                            f"reassign:{task['task_id']}:{attempt_id}",
                            correlation_id,
                            {
                                "task_id": task["task_id"],
                                "attempt_id": attempt_id,
                                "lease_id": lease_id,
                                "required_role": task["required_role"],
                                "unavailable_agent_ids": unavailable_agent_ids,
                                "selected_agent_id": agent["agent_id"],
                                "reassignment_reason": "agent_unavailable",
                            },
                        )
                    ]
                    if unavailable_agent_ids
                    else []
                ),
                self._event(
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
                        "lease_expires_at": lease_expires_at,
                    },
                ),
                *(
                    [
                        self._event(
                            "worktree_created",
                            agent_pool["scheduler_agent_id"],
                            agent["agent_id"],
                            f"worktree:{worktree_id}",
                            correlation_id,
                            {
                                "task_id": task["task_id"],
                                "attempt_id": attempt_id,
                                "worktree_id": worktree_id,
                                "worktree_path": str(worktree_path),
                                "branch": branch,
                                "write_scope": task["write_scope"],
                            },
                        )
                    ]
                    if worktree_id and worktree_path
                    else []
                ),
                self._event(
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
                ),
                self._event(
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
                        **metadata,
                        "worktree_id": worktree_id,
                        "worktree_path": str(worktree_path) if worktree_path else None,
                        "session_status": "started",
                    },
                ),
            ],
        )

        inflight = {
            "step_id": step_id,
            "step_dir": str(step_dir),
            "task_id": task["task_id"],
            "attempt_number": attempt_number,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "lease_expires_at": lease_expires_at,
            "message_id": message_id,
            "runtime_session_id": runtime_session_id,
            "agent_id": agent["agent_id"],
            "outbox_path": str(step_dir / agent["outbox_path"]),
            "worktree_id": worktree_id,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "branch": branch,
            "correlation_id": correlation_id,
        }
        self.state["inflight_attempts"].append(inflight)
        return {"task_id": task["task_id"], "step_id": step_id}

    def _collect_result(self, inflight, runtime_result):
        task = self._task_by_id(inflight["task_id"])
        diff_audit = (
            audit_worktree_diff(inflight["worktree_path"], runtime_result["changed_files"])
            if inflight["worktree_path"]
            else None
        )
        patch_path = (
            write_patch_artifact(
                inflight["worktree_path"],
                self.output_dir / "attempts" / inflight["attempt_id"],
                diff_audit["actual_changed_files"],
            )
            if inflight["worktree_path"]
            and diff_audit
            and diff_audit["actual_changed_files"]
            else None
        )
        outcome = classify_attempt_outcome(runtime_result, task, diff_audit=diff_audit)
        permission_request = (
            _permission_request_payload(inflight, runtime_result)
            if runtime_result["result_status"] == "blocked"
            and not _runtime_result_has_manual_gate(runtime_result)
            else None
        )
        manual_gate = (
            _manual_gate_payload(inflight, runtime_result)
            if runtime_result["result_status"] == "blocked" and not permission_request
            else None
        )
        retry_allowed = (
            outcome["retryable"]
            and inflight["attempt_number"] < self.state["max_attempts"]
        )
        next_attempt_id = (
            f"{inflight['task_id']}-ATTEMPT-{inflight['attempt_number'] + 1:03d}"
        )
        result_actor = (
            "agent-scheduler"
            if runtime_result["result_status"] == "timed_out"
            else inflight["agent_id"]
        )
        result = {
            "task_id": inflight["task_id"],
            "attempt_number": inflight["attempt_number"],
            "attempt_id": inflight["attempt_id"],
            "lease_id": inflight["lease_id"],
            "message_id": inflight["message_id"],
            "runtime_session_id": inflight["runtime_session_id"],
            "runtime_session_status": "stopped",
            "changed_files": list(runtime_result["changed_files"]),
            "runtime_output": runtime_result.get("output", {}),
            "token_usage": token_usage_from_result(runtime_result),
            "worktree_id": inflight["worktree_id"],
            "worktree_path": inflight["worktree_path"],
            "branch": inflight["branch"],
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
            "integration_queue_path": str(integration_queue_path(self.output_dir)),
        }
        integration_events = self._integrate_accepted_result(
            inflight,
            result,
            patch_path,
            outcome,
        )
        decomposition_events = self._apply_decomposition_result(
            inflight,
            runtime_result,
            result,
            outcome,
        )
        runtime_events = [
                self._event(
                    "runtime_session_observed",
                    result_actor,
                    "agent-scheduler",
                    f"runtime-session-observed:{inflight['runtime_session_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "runtime_session_id": inflight["runtime_session_id"],
                        "result_status": runtime_result["result_status"],
                        "changed_file_count": len(runtime_result["changed_files"]),
                        "session_status": "observed",
                    },
                ),
                self._event(
                    "runtime_output_received",
                    result_actor,
                    "agent-scheduler",
                    f"runtime-result:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "result_status": runtime_result["result_status"],
                        "changed_files": runtime_result["changed_files"],
                        "output": runtime_result.get("output", {}),
                        "diff_audit": diff_audit,
                        "patch_path": str(patch_path) if patch_path else None,
                    },
                ),
                self._event(
                    "runtime_session_stopped",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"runtime-session-stopped:{inflight['runtime_session_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "runtime_session_id": inflight["runtime_session_id"],
                        "result_status": runtime_result["result_status"],
                        "session_status": "stopped",
                    },
                ),
                self._event(
                    "validation_accepted"
                    if outcome["validation_status"] == "accepted"
                    else "validation_rejected",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"validate:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "validation_status": outcome["validation_status"],
                        "failure_category": outcome["failure_category"],
                        "retryable": outcome["retryable"],
                        "lease_id": inflight["lease_id"],
                        "diff_audit": diff_audit,
                        "patch_path": str(patch_path) if patch_path else None,
                        "semantic_validation": outcome.get("semantic_validation"),
                        **_decomposition_validation_payload(result),
                    },
                ),
                *(
                    [
                        self._event(
                            "manual_gate_required",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"manual-gate:{manual_gate['question_id']}",
                            inflight["correlation_id"],
                            manual_gate,
                        )
                    ]
                    if manual_gate
                    else []
                ),
                *(
                    [
                        self._event(
                            "permission_request_required",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"permission-request:{permission_request['request_id']}",
                            inflight["correlation_id"],
                            permission_request,
                        )
                    ]
                    if permission_request
                    else []
                ),
                *integration_events,
                *decomposition_events,
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            f"backlog-done:{inflight['task_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": "done",
                                "lease_id": inflight["lease_id"],
                            },
                        )
                    ]
                    if outcome["validation_status"] == "accepted"
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            f"backlog-blocked:{inflight['task_id']}:{manual_gate['question_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": "blocked",
                                "lease_id": inflight["lease_id"],
                                "update_type": "manual_gate_required",
                                "question_id": manual_gate["question_id"],
                                "blockers": [manual_gate["question_id"]],
                            },
                        )
                    ]
                    if manual_gate
                    else []
                ),
                *(
                    [
                        self._event(
                            "backlog_updated",
                            "agent-scheduler",
                            None,
                            f"backlog-blocked:{inflight['task_id']}:{permission_request['request_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "task_status": "blocked",
                                "lease_id": inflight["lease_id"],
                                "update_type": "permission_request_required",
                                "request_id": permission_request["request_id"],
                                "blockers": [permission_request["request_id"]],
                            },
                        )
                    ]
                    if permission_request
                    else []
                ),
                *(
                    [
                        self._event(
                            "recovery_routed",
                            "agent-scheduler",
                            inflight["agent_id"],
                            f"recovery:{inflight['attempt_id']}",
                            inflight["correlation_id"],
                            {
                                "task_id": inflight["task_id"],
                                "attempt_id": inflight["attempt_id"],
                                "lease_id": inflight["lease_id"],
                                "failure_category": outcome["failure_category"],
                                "next_attempt_id": next_attempt_id,
                                "recovery_action": "retry",
                            },
                        )
                    ]
                    if retry_allowed
                    else []
                ),
            ]
        canonical_events = self._append_events(inflight["step_id"], runtime_events)
        self._notify_canonical_events(inflight["step_id"], canonical_events)
        self._update_task_from_outcome(
            inflight["task_id"],
            outcome["validation_status"],
            outcome["failure_category"],
            retry_allowed,
            manual_gate_question_id=manual_gate["question_id"] if manual_gate else None,
            permission_request_id=permission_request["request_id"]
            if permission_request
            else None,
        )
        self.state["steps"].append(
            {
                "step_id": inflight["step_id"],
                "step_status": "retry_routed" if retry_allowed else "processed",
                "task_id": inflight["task_id"],
                "attempt_number": inflight["attempt_number"],
                "validation_status": outcome["validation_status"],
                "failure_category": outcome["failure_category"],
                "retryable": outcome["retryable"],
                "result": result,
            }
        )
        return result

    def _apply_decomposition_result(self, inflight, runtime_result, result, outcome):
        task = self._task_by_id(inflight["task_id"])
        if task.get("task_kind") != "decompose_backlog":
            return []
        if outcome["validation_status"] != "accepted":
            result["decomposition_status"] = "not_applied"
            result["generated_task_ids"] = []
            return []
        try:
            context = self._read_planner_context(task)
            normalized = normalize_task_proposal(
                runtime_result.get("output", {}).get("task_proposal"),
                existing_task_ids={
                    item["task_id"]
                    for item in self.state["backlog"]["items"]
                },
                allowed_roles=context["available_agent_roles"],
                allowed_write_scopes=context["allowed_write_scopes"],
            )
        except ValueError as exc:
            result["decomposition_status"] = "rejected"
            result["generated_task_ids"] = []
            result["failure_category"] = "invalid_task_proposal"
            result["decomposition_error"] = str(exc)
            outcome["validation_status"] = "rejected"
            outcome["failure_category"] = "invalid_task_proposal"
            outcome["retryable"] = False
            return []

        decomposition_wave = task.get(
            "decomposition_wave",
            _decomposition_wave_from_task_id(task["task_id"]),
        )
        for generated_task in normalized["tasks"]:
            generated_task["generated_by_decomposition_task_id"] = task["task_id"]
            generated_task["decomposition_wave"] = decomposition_wave
        self.state["backlog"]["items"].extend(normalized["tasks"])
        self._record_decomposition_applied(
            task,
            normalized["generated_task_ids"],
            decomposition_wave,
        )
        result["decomposition_status"] = "applied"
        result["generated_task_ids"] = normalized["generated_task_ids"]
        result["generated_task_count"] = len(normalized["generated_task_ids"])
        return [
            self._event(
                "backlog_updated",
                "agent-scheduler",
                None,
                f"backlog-decomposition:{inflight['attempt_id']}",
                inflight["correlation_id"],
                {
                    "task_id": inflight["task_id"],
                    "attempt_id": inflight["attempt_id"],
                    "lease_id": inflight["lease_id"],
                    "task_status": "done",
                    "update_type": "decomposition_applied",
                    "generated_task_ids": normalized["generated_task_ids"],
                },
            )
        ]

    def _read_planner_context(self, task):
        context_path = task.get("planner_context_path")
        if not context_path:
            return {
                "available_agent_roles": None,
                "allowed_write_scopes": None,
            }
        return json.loads(Path(context_path).read_text(encoding="utf-8"))

    def _milestone_state(self, milestone_id):
        milestones = self.state.setdefault("milestones", {})
        return milestones.setdefault(
            milestone_id,
            {
                "milestone_id": milestone_id,
                "milestone_status": "active",
                "decomposition_status": "idle",
                "decomposition_wave_count": 0,
                "current_decomposition_task_id": None,
                "generated_task_ids": [],
            },
        )

    def _record_decomposition_applied(self, task, generated_task_ids, decomposition_wave):
        milestone = self._milestone_state(task["milestone_id"])
        generated = list(milestone.get("generated_task_ids", []))
        for task_id in generated_task_ids:
            if task_id not in generated:
                generated.append(task_id)
        milestone.update(
            {
                "milestone_status": "active",
                "decomposition_status": "batch_active",
                "decomposition_wave_count": max(
                    milestone.get("decomposition_wave_count", 0),
                    decomposition_wave,
                ),
                "current_decomposition_task_id": task["task_id"],
                "generated_task_ids": generated,
            }
        )

    def _integrate_accepted_result(self, inflight, result, patch_path, outcome):
        if outcome["validation_status"] != "accepted":
            return []
        events = []
        if patch_path:
            queue = upsert_integration_queue_item(self.output_dir, result)
            result.update(queue)
            events.append(
                self._event(
                    "integration_queued",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"integration-queued:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "patch_path": str(patch_path),
                        **queue,
                    },
                )
            )
        if self.integrate_accepted_patch and self.project_root and patch_path:
            integration = apply_patch_to_integration_worktree(
                self.project_root,
                self.output_dir,
                inflight["task_id"],
                patch_path,
            )
            result.update(integration)
            events.append(
                self._event(
                    "patch_integrated",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"patch-integrated:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        "patch_path": str(patch_path),
                        **integration,
                    },
                )
            )
            if self.integration_verification_command:
                verification = run_integration_verification(
                    self.integration_verification_command,
                    integration["integration_worktree_path"],
                )
                result.update(verification)
                events.append(
                    self._event(
                        "integration_verified",
                        "agent-scheduler",
                        inflight["agent_id"],
                        f"integration-verified:{inflight['attempt_id']}",
                        inflight["correlation_id"],
                        {
                            "task_id": inflight["task_id"],
                            "attempt_id": inflight["attempt_id"],
                            "lease_id": inflight["lease_id"],
                            **verification,
                        },
                    )
                )
        if self.commit_verified_integration:
            integration_commit = evaluate_integration_commit(
                result,
                inflight["task_id"],
                inflight["attempt_id"],
            )
            result.update(integration_commit)
            events.append(
                self._event(
                    "integration_commit_evaluated",
                    "agent-scheduler",
                    inflight["agent_id"],
                    f"integration-commit:{inflight['attempt_id']}",
                    inflight["correlation_id"],
                    {
                        "task_id": inflight["task_id"],
                        "attempt_id": inflight["attempt_id"],
                        "lease_id": inflight["lease_id"],
                        **integration_commit,
                    },
                )
            )
        if patch_path:
            result.update(upsert_integration_queue_item(self.output_dir, result))
        return events

    def _lease_expired(self, inflight):
        now = _parse_utc_timestamp(self.clock.now())
        lease_expires_at = _parse_utc_timestamp(inflight["lease_expires_at"])
        return now >= lease_expires_at

    def _timeout_runtime_result(self, inflight):
        return {
            "result_status": "timed_out",
            "changed_files": [],
            "output": {
                "adapter": "two_phase_scheduler",
                "error": "lease_timeout",
                "lease_expires_at": inflight["lease_expires_at"],
                "message_id": inflight["message_id"],
            },
        }

    def _ready_tasks(self):
        inflight_task_ids = {
            attempt["task_id"]
            for attempt in self.state["inflight_attempts"]
        }
        done_by_id = {
            item["task_id"]: item.get("backlog_status") == "done"
            for item in self.state["backlog"]["items"]
        }
        ready = []
        for task in self.state["backlog"]["items"]:
            if task["task_id"] in inflight_task_ids:
                continue
            if task.get("backlog_status") != "ready":
                continue
            if task.get("blockers"):
                continue
            if not all(done_by_id.get(dep_id, False) for dep_id in task.get("depends_on", [])):
                continue
            ready.append(task)
        return ready

    def _mark_inflight_agents_busy(self, agent_pool):
        inflight_agent_ids = {
            attempt["agent_id"]
            for attempt in self.state["inflight_attempts"]
        }
        for agent in agent_pool["agents"]:
            if agent["agent_id"] in inflight_agent_ids:
                agent["status"] = "busy"

    def _mark_unavailable_agents(self, agent_pool):
        for agent in agent_pool["agents"]:
            if agent["agent_id"] in self.unavailable_agent_ids:
                agent["status"] = "unavailable"

    def _unavailable_agent_ids_for_role(self, agent_pool, role):
        return [
            agent["agent_id"]
            for agent in agent_pool["agents"]
            if agent.get("role") == role
            and agent["agent_id"] in self.unavailable_agent_ids
        ]

    def _status_without_dispatch(self):
        if self.state["inflight_attempts"]:
            return "waiting"
        if self._ready_tasks():
            return "running"
        return "idle"

    def _ensure_decomposition_task(self):
        if not self.auto_decompose:
            return
        if self.state["inflight_attempts"] or self._ready_tasks():
            return
        milestone_id = self.decomposition_milestone_id
        decomposition_tasks = self._decomposition_tasks_for_milestone(milestone_id)
        if any(not _is_terminal_backlog_status(task) for task in decomposition_tasks):
            return
        if decomposition_tasks:
            latest = decomposition_tasks[-1]
            if latest.get("backlog_status") != "done":
                self._mark_milestone_terminal(milestone_id, "blocked")
                return
            if not self._generated_batch_terminal(latest["task_id"]):
                milestone = self._milestone_state(milestone_id)
                milestone["decomposition_status"] = "batch_active"
                return
        if len(decomposition_tasks) >= self.decomposition_max_waves:
            self._mark_milestone_terminal(
                milestone_id,
                self._terminal_milestone_status(milestone_id),
            )
            return
        decomposition_wave = len(decomposition_tasks) + 1
        task_id = f"DECOMPOSE-{milestone_id}-{decomposition_wave:03d}"
        planner_context_path = self._write_planner_context(task_id)
        self.state["backlog"]["items"].append(
            {
                "task_id": task_id,
                "task_kind": "decompose_backlog",
                "milestone_id": milestone_id,
                "decomposition_wave": decomposition_wave,
                "objective": (
                    "Generate the next bounded executable backlog tasks for "
                    f"{milestone_id}."
                ),
                "backlog_status": "ready",
                "risk_target": "L0",
                "depends_on": [],
                "read_scope": ["."],
                "write_scope": [],
                "required_role": self.decomposition_planner_role,
                "default_worker_role": self.decomposition_default_worker_role,
                "planner_context_path": str(planner_context_path),
                "allowed_read_scopes": self.decomposition_allowed_read_scopes,
                "allowed_write_scopes": self.decomposition_allowed_write_scopes,
                "blockers": [],
            }
        )
        milestone = self._milestone_state(milestone_id)
        milestone.update(
            {
                "milestone_status": "active",
                "decomposition_status": "decomposition_ready",
                "decomposition_wave_count": max(
                    milestone.get("decomposition_wave_count", 0),
                    decomposition_wave,
                ),
                "current_decomposition_task_id": task_id,
            }
        )

    def _write_planner_context(self, task_id):
        context = build_planner_context(
            _read_json(self.agent_pool_path),
            self.state,
            milestone_id=self.decomposition_milestone_id,
            default_worker_role=self.decomposition_default_worker_role,
            allowed_read_scopes=self.decomposition_allowed_read_scopes,
            allowed_write_scopes=self.decomposition_allowed_write_scopes,
            context_artifact_paths=self.decomposition_context_artifact_paths,
            context_artifact_excerpt_chars=self.decomposition_context_excerpt_chars,
        )
        context_path = self.output_dir / "planner_contexts" / f"{task_id}.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(json.dumps(context, sort_keys=True), encoding="utf-8")
        return context_path

    def _task_by_id(self, task_id):
        for task in self.state["backlog"]["items"]:
            if task["task_id"] == task_id:
                return task
        raise ValueError(f"task not found in two-phase scheduler state: {task_id}")

    def _decomposition_tasks_for_milestone(self, milestone_id):
        return sorted(
            [
                task
                for task in self.state["backlog"]["items"]
                if task.get("task_kind") == "decompose_backlog"
                and task.get("milestone_id") == milestone_id
            ],
            key=lambda task: task.get(
                "decomposition_wave",
                _decomposition_wave_from_task_id(task["task_id"]),
            ),
        )

    def _generated_batch_terminal(self, decomposition_task_id):
        generated_tasks = [
            task
            for task in self.state["backlog"]["items"]
            if task.get("generated_by_decomposition_task_id") == decomposition_task_id
        ]
        return all(_is_terminal_backlog_status(task) for task in generated_tasks)

    def _terminal_milestone_status(self, milestone_id):
        generated_ids = self._milestone_state(milestone_id).get("generated_task_ids", [])
        generated_tasks = [
            self._task_by_id(task_id)
            for task_id in generated_ids
            if any(item["task_id"] == task_id for item in self.state["backlog"]["items"])
        ]
        if any(task.get("backlog_status") == "blocked" for task in generated_tasks):
            return "blocked"
        return "completed"

    def _mark_milestone_terminal(self, milestone_id, milestone_status):
        milestone = self._milestone_state(milestone_id)
        milestone["milestone_status"] = milestone_status
        milestone["decomposition_status"] = "max_waves_reached"
        milestone["terminal_reason"] = "max_waves_reached"

    def _update_task_from_outcome(
        self,
        task_id,
        validation_status,
        failure_category,
        retry_allowed,
        manual_gate_question_id=None,
        permission_request_id=None,
    ):
        task = self._task_by_id(task_id)
        if validation_status == "accepted":
            task["backlog_status"] = "done"
            task["blockers"] = []
        elif retry_allowed:
            task["backlog_status"] = "ready"
            task["blockers"] = []
        else:
            task["backlog_status"] = "blocked"
            task["blockers"] = [
                manual_gate_question_id
                or permission_request_id
                or failure_category
                or "validation_rejected"
            ]

    def _next_attempt_number(self, task_id):
        attempt_numbers = [
            item.get("attempt_number", 1)
            for item in [
                *self.state["steps"],
                *self.state["inflight_attempts"],
            ]
            if item["task_id"] == task_id
        ]
        return max(attempt_numbers, default=0) + 1

    def _next_step_id(self, task_id):
        step_number = len(self.state["steps"]) + len(self.state["inflight_attempts"]) + 1
        return f"STEP-{step_number:04d}-{task_id}"

    def _append_events(self, step_id, events):
        sequence = self._next_sequence()
        canonical = []
        for event in events:
            canonical.append(
                {
                    **event,
                    "event_id": f"EVT-{sequence:04d}",
                    "sequence": sequence,
                    "run_id": self.run_id,
                    "step_id": step_id,
                }
            )
            sequence += 1
        _append_jsonl(self.events_path, canonical)
        return canonical

    def _notify_canonical_events(self, step_id, events):
        if not self.notification_sink:
            return []
        telemetry_events = []
        allowed_event_types = getattr(
            self.notification_sink,
            "allowed_event_types",
            DEFAULT_NOTIFICATION_EVENT_TYPES,
        )
        for event in events:
            if event.get("event_type") not in allowed_event_types:
                continue
            try:
                result = self.notification_sink.notify(
                    event,
                    {"run_dir": str(self.output_dir)},
                )
            except Exception as exc:
                result = [
                    self._event(
                        "notification_failed",
                        "agent-notifier",
                        None,
                        f"notification:{event['event_id']}:failed",
                        event["correlation_id"],
                        {
                            "provider": "unknown",
                            "source_event_type": event.get("event_type"),
                            "source_event_id": event.get("event_id"),
                            "source_event_sequence": event.get("sequence"),
                            "notification_status": "failed",
                            "error_class": exc.__class__.__name__,
                            "error_summary": str(exc)[:300],
                        },
                    )
                ]
            if isinstance(result, dict):
                telemetry_events.append(self._notification_event_from_spec(result, event))
            elif isinstance(result, list):
                telemetry_events.extend(
                    self._notification_event_from_spec(item, event)
                    for item in result
                    if isinstance(item, dict)
                )
        if telemetry_events:
            return self._append_events(step_id, telemetry_events)
        return []

    def _emit_run_event_once(self, event_type, payload):
        run_event_ids = self.state.setdefault("run_event_ids", {})
        if run_event_ids.get(event_type):
            return []
        canonical = self._append_events(
            "STEP-RUN",
            [
                self._event(
                    event_type,
                    "agent-scheduler",
                    None,
                    f"{event_type}:{self.run_id}",
                    f"run:{self.run_id}",
                    payload,
                )
            ],
        )
        run_event_ids[event_type] = canonical[0]["event_id"]
        self._write_state()
        self._notify_canonical_events("STEP-RUN", canonical)
        return canonical

    def _run_event_payload(self, run_status, extra=None):
        payload = {
            "run_status": run_status,
            "scheduler_status": self.state.get("scheduler_status"),
            "processed_task_count": len(self.summary()["processed_task_ids"]),
            "inflight_count": len(self.state["inflight_attempts"]),
            "step_count": len(self.state["steps"]),
        }
        if run_status != "running":
            operator_report = _operator_report_from_state(self.state)
            if operator_report["task_reports"]:
                payload["operator_report"] = operator_report
        if extra:
            payload.update(extra)
        return payload

    def _notification_event_from_spec(self, spec, source_event):
        if "time" in spec:
            return spec
        return self._event(
            spec["event_type"],
            spec.get("actor", "agent-notifier"),
            spec.get("target_agent_id"),
            spec.get("idempotency_key", f"notification:{source_event['event_id']}"),
            spec.get("correlation_id", source_event["correlation_id"]),
            spec.get("payload", {}),
        )

    def _event(self, event_type, actor, target_agent_id, idempotency_key, correlation_id, payload):
        return _event(
            0,
            self.clock.now(),
            event_type,
            actor,
            target_agent_id,
            idempotency_key,
            correlation_id,
            payload,
        )

    def _next_sequence(self):
        if not self.events_path.exists():
            return 1
        existing = [
            event["sequence"]
            for event in _read_jsonl_if_exists(self.events_path)
        ]
        return max(existing) + 1 if existing else 1

    def _load_or_create_state(self):
        if self.state_path.exists():
            state = _read_json(self.state_path)
            state.setdefault("max_attempts", self.max_attempts)
            state.setdefault("lease_timeout_seconds", self.lease_timeout_seconds)
            state.setdefault("milestones", {})
            return state
        return {
            "scheduler_status": "initialized",
            "max_attempts": self.max_attempts,
            "lease_timeout_seconds": self.lease_timeout_seconds,
            "backlog": _read_json(self.backlog_path),
            "steps": [],
            "inflight_attempts": [],
            "milestones": {},
        }

    def _write_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")


def run_two_phase_scheduler_loop(*args, max_ticks=100, poll_interval_seconds=0.02, **kwargs):
    scheduler = TwoPhaseFileScheduler(*args, **kwargs)
    return scheduler.run_until_idle(
        max_ticks=max_ticks,
        poll_interval_seconds=poll_interval_seconds,
    )


def _decomposition_validation_payload(result):
    payload = {}
    if "decomposition_status" in result:
        payload["decomposition_status"] = result["decomposition_status"]
    if "decomposition_error" in result:
        payload["decomposition_error"] = result["decomposition_error"]
    return payload


def _decomposition_wave_from_task_id(task_id):
    try:
        return int(str(task_id).rsplit("-", 1)[1])
    except (TypeError, ValueError):
        return 1


def _is_terminal_backlog_status(task):
    return task.get("backlog_status") in {"done", "blocked"}


def _operator_report_from_state(state):
    task_reports = []
    for step in state.get("steps", []):
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        task_reports.append(_operator_task_report(step, result))
    token_usages = [report.get("token_usage") for report in task_reports]
    return {
        "report_schema_version": "operator_run_report.v1",
        "task_count": len(task_reports),
        "blocked_count": sum(
            1 for report in task_reports if "blocked" in report.get("status", "")
        ),
        "token_usage": aggregate_token_usage(token_usages, expected_count=len(task_reports)),
        "task_reports": task_reports,
    }


def _operator_task_report(step, result):
    output = result.get("runtime_output") if isinstance(result.get("runtime_output"), dict) else {}
    operator_summary = (
        output.get("operator_summary")
        if isinstance(output.get("operator_summary"), dict)
        else {}
    )
    return {
        "task_id": result.get("task_id") or step.get("task_id") or "unknown",
        "attempt_id": result.get("attempt_id"),
        "status": _operator_task_status(result),
        "what_changed": _operator_what_changed(output, operator_summary),
        "changed_files": _operator_changed_files(result),
        "verification": _operator_verification(output, operator_summary),
        "integration": _operator_integration_summary(result),
        "merge_recommendation": _operator_merge_recommendation(result, operator_summary),
        "next_steps": _operator_next_steps(result, operator_summary),
        "token_usage": token_usage_from_result(result),
    }


def _operator_task_status(result):
    validation = result.get("validation_status")
    integration = result.get("integration_verification_status")
    if integration == "failed":
        return "implementation completed, integration blocked"
    if validation == "accepted":
        return "implementation completed"
    if validation == "rejected":
        return "implementation rejected"
    return result.get("failure_category") or validation or "unknown"


def _operator_what_changed(output, operator_summary):
    for key in ["what_changed", "summary"]:
        values = _coerce_text_list(operator_summary.get(key))
        if values:
            return values
    values = _coerce_text_list(output.get("summary"))
    if values:
        return values
    behavior_change = operator_summary.get("behavior_change")
    if behavior_change:
        return [str(behavior_change)]
    return ["Worker did not provide a natural-language change summary."]


def _operator_changed_files(result):
    changed_files = _coerce_text_list(result.get("changed_files"))
    if changed_files:
        return changed_files
    diff_audit = result.get("diff_audit") if isinstance(result.get("diff_audit"), dict) else {}
    return _coerce_text_list(
        diff_audit.get("actual_changed_files") or diff_audit.get("declared_changed_files")
    )


def _operator_verification(output, operator_summary):
    explicit = _coerce_text_list(operator_summary.get("verification_summary"))
    if explicit:
        return explicit
    verification = output.get("verification")
    if not isinstance(verification, dict):
        return []
    lines = []
    for name, item in verification.items():
        if isinstance(item, dict):
            status = item.get("status") or item.get("result") or "unknown"
        else:
            status = str(item)
        lines.append(f"{name}: {status}")
    return lines


def _operator_integration_summary(result):
    status = result.get("integration_verification_status")
    if status == "failed":
        failure = _first_failure_line(result.get("integration_verification_stderr", ""))
        return f"failed: {failure}" if failure else "failed"
    if status in {None, "not_requested"}:
        return "not requested"
    if status == "passed":
        return "passed"
    return str(status)


def _operator_merge_recommendation(result, operator_summary):
    explicit = operator_summary.get("merge_recommendation")
    if explicit:
        return str(explicit)
    if result.get("integration_verification_status") == "failed":
        return "Do not merge until integration passes."
    if result.get("validation_status") == "accepted":
        return "Review accepted patch before merging."
    return "Do not merge until the task is accepted."


def _operator_next_steps(result, operator_summary):
    explicit = _coerce_text_list(operator_summary.get("next_steps"))
    if explicit:
        return explicit
    if result.get("integration_verification_status") == "failed":
        return ["Review the failing integration test and update the patch or generated artifacts."]
    return []


def _first_failure_line(text):
    for line in reversed(str(text or "").splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("FAILED")
            or stripped.startswith("FAIL:")
            or stripped.startswith("ERROR:")
            or "ModuleNotFoundError" in stripped
        ):
            return stripped
    return None


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


def _manual_gate_payload(inflight, runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    if not isinstance(output, dict):
        output = {}
    manual_gate = output.get("manual_gate")
    if not isinstance(manual_gate, dict):
        manual_gate = {}
    question = _first_non_empty_string(
        manual_gate.get("question"),
        output.get("question"),
        "Worker requested operator guidance before continuing.",
    )
    options = manual_gate.get("options", [])
    if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
        options = []
    return {
        "task_id": inflight["task_id"],
        "attempt_id": inflight["attempt_id"],
        "lease_id": inflight["lease_id"],
        "question_id": f"Q-{inflight['attempt_id']}",
        "gate_status": "waiting",
        "question": question,
        "options": options,
        "reason": _first_non_empty_string(
            manual_gate.get("reason"),
            output.get("reason"),
            None,
        ),
        "guidance_scope": _first_non_empty_string(
            manual_gate.get("guidance_scope"),
            "next_attempt",
        ),
    }


def _runtime_result_has_manual_gate(runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    return isinstance(output, dict) and isinstance(output.get("manual_gate"), dict)


def _permission_request_payload(inflight, runtime_result):
    output = runtime_result.get("output") if isinstance(runtime_result, dict) else {}
    if not isinstance(output, dict):
        return None
    request = output.get("permission_request")
    if not isinstance(request, dict):
        return None
    requested_capability = _first_non_empty_string(
        request.get("requested_capability"),
        request.get("capability"),
        "runtime_permission",
    )
    reason = _first_non_empty_string(
        request.get("reason"),
        output.get("reason"),
        "Worker requested an operator-approved runtime capability before continuing.",
    )
    payload = {
        "task_id": inflight["task_id"],
        "attempt_id": inflight["attempt_id"],
        "lease_id": inflight["lease_id"],
        "request_id": f"PERM-{inflight['attempt_id']}",
        "request_status": "waiting",
        "request_type": _first_non_empty_string(
            request.get("request_type"),
            "runtime_permission",
        ),
        "requested_capability": requested_capability,
        "reason": reason,
        "scope": _first_non_empty_string(request.get("scope"), "next_attempt"),
    }
    for key in ["command", "sandbox", "exit_code"]:
        if key in request:
            payload[key] = request[key]
    return payload


def _operator_guidance_fields(task):
    guidance = task.get("operator_guidance")
    if not guidance:
        return {}
    if not isinstance(guidance, list):
        return {}
    safe_guidance = [
        {
            "question_id": item.get("question_id"),
            "answer": item.get("answer"),
            "operator": item.get("operator"),
        }
        for item in guidance
        if isinstance(item, dict)
    ]
    return {"operator_guidance": safe_guidance} if safe_guidance else {}


def _permission_grant_fields(task):
    grants = task.get("permission_grants")
    if not grants:
        return {}
    if not isinstance(grants, list):
        return {}
    safe_grants = [
        {
            "request_id": item.get("request_id"),
            "requested_capability": item.get("requested_capability"),
            "operator": item.get("operator"),
            "reason": item.get("reason"),
        }
        for item in grants
        if isinstance(item, dict)
    ]
    return {"permission_grants": safe_grants} if safe_grants else {}


def _first_non_empty_string(*values):
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _runtime_result_from_outbox(outbox_path, source_message_id):
    for record in _read_jsonl_if_exists(outbox_path):
        if record.get("message_type") != "runtime_result":
            continue
        payload = record.get("payload", {})
        if payload.get("source_message_id") != source_message_id:
            continue
        return {
            "result_status": payload.get("result_status", "failed"),
            "changed_files": payload.get("changed_files", []),
            "output": payload.get("output", {}),
            "usage": payload.get("usage"),
            "token_usage": payload.get("token_usage"),
        }
    return None


def _read_jsonl_if_exists(path):
    path = Path(path)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _timestamp_after(timestamp, seconds):
    return _format_utc_timestamp(
        _parse_utc_timestamp(timestamp) + timedelta(seconds=seconds)
    )


def _parse_utc_timestamp(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)


def _format_utc_timestamp(timestamp):
    return timestamp.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

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
    _read_json,
    _runtime_adapter_metadata,
    _scoped_id,
    classify_attempt_outcome,
    rebuild_sqlite_state_index,
)


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
        state_path=None,
    ):
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if lease_timeout_seconds < 0:
            raise ValueError("lease_timeout_seconds must be at least 0")
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
        self.state_path = Path(
            state_path or self.output_dir / "state" / "two_phase_scheduler_state.json"
        )
        self.state_db_path = self.output_dir / "state" / "scheduler_state.sqlite"
        self.events_path = self.output_dir / "events.jsonl"
        self.run_id = "RUN-TWO-PHASE-SCHEDULER"
        self.state = self._load_or_create_state()

    def dispatch_ready(self):
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

    def run_until_idle(self, max_ticks=100, poll_interval_seconds=0.02):
        if max_ticks < 1:
            raise ValueError("max_ticks must be at least 1")
        tick_count = 0
        last_tick = None
        for _ in range(max_ticks):
            tick_count += 1
            last_tick = self.tick()
            if last_tick["tick_status"] == "idle":
                return {
                    **self.summary(),
                    "scheduler_status": "idle",
                    "tick_count": tick_count,
                    "last_tick": last_tick,
                }
            if last_tick["tick_status"] == "waiting":
                time.sleep(poll_interval_seconds)
        self.state["scheduler_status"] = "max_ticks_reached"
        self._write_state()
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
                "objective": task["objective"],
                "read_scope": task["read_scope"],
                "write_scope": task["write_scope"],
            },
        }
        inbox_path = step_dir / agent["inbox_path"]
        _append_jsonl(inbox_path, [message])

        metadata = (
            _runtime_adapter_metadata(self.runtime_adapter)
            if self.runtime_adapter
            else {
                "runtime_adapter": "FileMailboxExternalRuntimeAdapter",
                "runtime_model": None,
                "runtime_sandbox": None,
                "runtime_timeout_seconds": None,
            }
        )
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
        outcome = classify_attempt_outcome(runtime_result, task, diff_audit=None)
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
            "worktree_id": inflight["worktree_id"],
            "worktree_path": inflight["worktree_path"],
            "branch": inflight["branch"],
            "validation_status": outcome["validation_status"],
            "failure_category": outcome["failure_category"],
            "retryable": outcome["retryable"],
            "diff_audit": None,
            "patch_path": None,
        }
        self._append_events(
            inflight["step_id"],
            [
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
                        "diff_audit": None,
                        "patch_path": None,
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
                        "diff_audit": None,
                        "patch_path": None,
                    },
                ),
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
            ],
        )
        self._update_task_from_outcome(
            inflight["task_id"],
            outcome["validation_status"],
            outcome["failure_category"],
            retry_allowed,
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

    def _status_without_dispatch(self):
        if self.state["inflight_attempts"]:
            return "waiting"
        if self._ready_tasks():
            return "running"
        return "idle"

    def _task_by_id(self, task_id):
        for task in self.state["backlog"]["items"]:
            if task["task_id"] == task_id:
                return task
        raise ValueError(f"task not found in two-phase scheduler state: {task_id}")

    def _update_task_from_outcome(
        self,
        task_id,
        validation_status,
        failure_category,
        retry_allowed,
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
            task["blockers"] = [failure_category or "validation_rejected"]

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
            return state
        return {
            "scheduler_status": "initialized",
            "max_attempts": self.max_attempts,
            "lease_timeout_seconds": self.lease_timeout_seconds,
            "backlog": _read_json(self.backlog_path),
            "steps": [],
            "inflight_attempts": [],
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

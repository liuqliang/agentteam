import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


class SystemClock:
    def now(self):
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeRuntimeAdapter:
    def run(self, message, worktree_path=None):
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
        return {
            "result_status": "completed",
            "changed_files": changed_files,
            "output": {"adapter": "fake"},
        }


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
    ):
        self.command = list(command or ["codex", "exec"])
        self.model = model
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])

    def run(self, message, worktree_path=None):
        if not worktree_path:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "codex", "error": "missing_worktree_path"},
            }

        result_path = (
            Path(worktree_path)
            / ".agentteam"
            / f"codex_result_{message['payload']['attempt_id']}.json"
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)

        command = self._build_command(worktree_path, result_path)
        prompt = self._build_prompt(message)
        try:
            completed = subprocess.run(
                command,
                cwd=worktree_path,
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

        if completed.returncode != 0:
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

        return _normalize_runtime_result(result, adapter="codex", stderr=completed.stderr)

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
        return "\n".join(
            [
                "You are an AgentTeam runtime worker.",
                "Execute only the bounded task described by this mailbox message.",
                "Return exactly one JSON object as the final response.",
                "The JSON object must have this shape:",
                '{"result_status":"completed|blocked|failed|cancelled","changed_files":["path"],"output":{}}',
                "All changed_files entries must be relative paths inside the declared write_scope.",
                "Mailbox message:",
                json.dumps(message, sort_keys=True),
            ]
        )


def run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    clock=None,
    project_root=None,
    runtime_adapter=None,
    max_attempts=1,
    cleanup_accepted_worktrees=False,
):
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    clock = clock or SystemClock()
    runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    agent_pool = _read_json(agent_pool_path)
    backlog = _read_json(backlog_path)

    task = _select_ready_task(backlog)
    agent = _find_idle_agent(agent_pool, task["required_role"])

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
        attempt_id = f"ATTEMPT-{attempt_number:03d}"
        lease_id = f"LEASE-{attempt_number:03d}"
        message_id = f"MSG-{attempt_number:04d}"
        worktree_id = f"WT-{attempt_id}" if task.get("write_scope") else None
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
                "read_scope": task["read_scope"],
                "write_scope": task["write_scope"],
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
            },
        )
        runtime_result = runtime_adapter.run(message, worktree_path=worktree_path)
        diff_audit = (
            audit_worktree_diff(worktree_path, runtime_result["changed_files"])
            if worktree_path
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
            },
        )

        final_attempt = {
            "task_id": task["task_id"],
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "message_id": message_id,
            "worktree_id": worktree_id,
            "worktree_path": str(worktree_path) if worktree_path else None,
            "branch": branch,
            "validation_status": outcome["validation_status"],
            "failure_category": outcome["failure_category"],
            "retryable": outcome["retryable"],
            "diff_audit": diff_audit,
            "worktree_removed": False,
        }
        attempts.append(final_attempt)

        if outcome["validation_status"] == "accepted":
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
                    "next_attempt_id": f"ATTEMPT-{attempt_number + 1:03d}",
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
        "worktree_id": final_attempt["worktree_id"],
        "worktree_path": final_attempt["worktree_path"],
        "branch": final_attempt["branch"],
        "validation_status": final_attempt["validation_status"],
        "failure_category": final_attempt["failure_category"],
        "retryable": final_attempt["retryable"],
        "diff_audit": final_attempt["diff_audit"],
        "attempt_count": len(attempts),
        "attempts": attempts,
        "worktree_removed": final_attempt["worktree_removed"],
        "events_path": str(events_path),
        "mailbox_path": str(inbox_path),
    }


def replay_events(events_path):
    snapshot = {"tasks": {}, "attempts": {}, "leases": {}}
    for event in _read_jsonl(events_path):
        payload = event["payload"]
        task_id = payload.get("task_id")
        attempt_id = payload.get("attempt_id")
        lease_id = payload.get("lease_id")

        if event["event_type"] == "task_selected":
            snapshot["tasks"][task_id] = {"task_status": "running"}
            snapshot["attempts"][attempt_id] = {"attempt_status": "created"}
        elif event["event_type"] == "lease_acquired":
            snapshot["leases"][lease_id] = {
                "lease_status": "active",
                "attempt_id": attempt_id,
                "task_id": task_id,
            }
            snapshot["attempts"].setdefault(attempt_id, {})["attempt_status"] = "dispatched"
        elif event["event_type"] == "worktree_created":
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_id"] = payload["worktree_id"]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_path"] = payload[
                "worktree_path"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["branch"] = payload["branch"]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_status"] = "created"
        elif event["event_type"] == "worktree_removed":
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_id"] = payload["worktree_id"]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_path"] = payload[
                "worktree_path"
            ]
            snapshot["attempts"].setdefault(attempt_id, {})["worktree_status"] = "removed"
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
            if lease_id in snapshot["leases"]:
                snapshot["leases"][lease_id]["lease_status"] = "released"
        elif event["event_type"] == "backlog_updated":
            snapshot["tasks"].setdefault(task_id, {})["task_status"] = payload["task_status"]

    return snapshot


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


def _fake_changed_files(write_scope):
    if not write_scope:
        return []
    return [f"{write_scope[0].rstrip('/')}/m0_generated_repo_index.json"]


def _changed_files_in_scope(changed_files, task):
    write_scope = [scope.rstrip("/") + "/" for scope in task.get("write_scope", [])]
    return all(any(path.startswith(scope) for scope in write_scope) for path in changed_files)


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
        return {
            "validation_status": "accepted",
            "failure_category": None,
            "retryable": False,
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


def _git_changed_files(worktree_path):
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    paths = []
    for line in completed.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith(".agentteam/"):
            continue
        paths.append(path)
    return paths


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
    return {
        "result_status": result.get("result_status", "failed"),
        "changed_files": changed_files,
        "output": result.get("output", {}),
    }


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


def _create_git_worktree(project_root, output_dir, attempt_id, worktree_id):
    project_root = Path(project_root)
    worktree_path = Path(output_dir) / "worktrees" / worktree_id
    branch = f"agentteam/{attempt_id}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(project_root), "worktree", "add", "-b", branch, str(worktree_path), "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return worktree_path, branch


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

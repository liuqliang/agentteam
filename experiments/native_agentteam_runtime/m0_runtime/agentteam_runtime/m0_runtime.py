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
    integrate_accepted_patch=False,
    integration_verification_command=None,
    commit_verified_integration=False,
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
            "worktree_removed": False,
        }
        attempts.append(final_attempt)

        if outcome["validation_status"] == "accepted":
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
            snapshot["attempts"].setdefault(attempt_id, {})["patch_path"] = payload.get(
                "patch_path"
            )
            if lease_id in snapshot["leases"]:
                snapshot["leases"][lease_id]["lease_status"] = "released"
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


def apply_patch_to_integration_worktree(project_root, output_dir, task_id, patch_path):
    integration_branch = f"agentteam/integration/{task_id}"
    integration_worktree = Path(output_dir) / "integration" / task_id
    integration_worktree.parent.mkdir(parents=True, exist_ok=True)
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
    }


def run_integration_verification(command, integration_worktree_path):
    completed = subprocess.run(
        list(command),
        cwd=integration_worktree_path,
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

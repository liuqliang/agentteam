"""Implementation-first worker experiment with controller-owned verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from collections import Counter
from pathlib import Path

from .m0_runtime import validate_verification_additions
from .worker_turn_checkpoint import (
    WorkerTurnCheckpointError,
    aggregate_worker_turn_usage,
    build_worker_turn_checkpoint,
    publish_worker_turn_checkpoint,
    worktree_state,
)


STATE_SCHEMA_VERSION = "adaptive_worker_turn_state.v1"
OBSERVATION_SCHEMA_VERSION = "worker_execution_observation.v1"
CHECKPOINT_FIELDS = (
    "completed_actions",
    "key_findings",
    "decisions",
    "verification",
    "remaining_objective",
    "source_paths",
)
TERMINAL_STATUSES = {"completed", "stopped", "failed"}


class AdaptiveWorkerTurnExperimentRunner:
    """Run implementation, deterministic verification, and optional repair."""

    def __init__(
        self,
        *,
        worktree_path,
        output_dir,
        run_id,
        task,
        invoke,
        run_verification,
        maximum_total_tokens,
        allow_repair=True,
    ):
        self.worktree = Path(worktree_path).resolve(strict=True)
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = str(run_id)
        self.task = _validate_task(task)
        self.invoke = invoke
        self.run_verification = run_verification
        self.maximum_total_tokens = int(maximum_total_tokens)
        self.allow_repair = bool(allow_repair)
        if self.maximum_total_tokens < 1:
            raise WorkerTurnCheckpointError("maximum_total_tokens must be positive")
        self.state_path = self.output_dir / "adaptive-worker-state.json"

    def run(self):
        state = self._load_or_initialize_state()
        if state["status"] in TERMINAL_STATUSES:
            return state
        while state["status"] == "running":
            phase = state["phase"]
            if phase == "implementation":
                self._run_model_turn(state, "implement")
            elif phase == "controller_verification":
                self._run_controller_verification(state, after_repair=False)
            elif phase == "repair":
                self._run_model_turn(state, "repair")
            elif phase == "post_repair_verification":
                self._run_controller_verification(state, after_repair=True)
            else:
                raise WorkerTurnCheckpointError(
                    f"adaptive worker phase is invalid: {phase}"
                )
        return state

    def resume_deferred_verification(self, settled_result):
        """Recover an older stopped state that missed a valid verification handoff."""

        state = self._load_or_initialize_state()
        if not (
            state["status"] == "stopped"
            and state["stop_reason"] == "worker_turn_not_completed"
            and state["phase"] == "implementation"
            and len(state["model_turns"]) == 1
        ):
            return state
        self._require_expected_worktree(state)
        if not isinstance(settled_result, dict):
            raise WorkerTurnCheckpointError("deferred worker result must be an object")
        if _usage_binding(settled_result.get("token_usage")) != _usage_binding(
            state["model_turns"][0]["token_usage"]
        ):
            raise WorkerTurnCheckpointError("deferred worker usage binding differs")
        if not state["model_turns"][0].get("checkpoint_path"):
            raise WorkerTurnCheckpointError("deferred worker checkpoint is missing")
        output = settled_result.get("output")
        if not _is_controller_verification_handoff(settled_result, output):
            raise WorkerTurnCheckpointError("deferred worker handoff is invalid")
        try:
            additions = validate_verification_additions(
                output.get("verification_additions")
            )
        except ValueError as exc:
            raise WorkerTurnCheckpointError(
                "deferred worker verification additions are invalid"
            ) from exc
        state["verification_additions"] = additions
        state["status"] = "running"
        state["stop_reason"] = None
        state["phase"] = "controller_verification"
        self._write_state(state)
        return state

    def _run_model_turn(self, state, stage):
        self._require_expected_worktree(state)
        if stage == "repair" and state["repair_invocations"] >= 1:
            raise WorkerTurnCheckpointError("adaptive worker repair limit exceeded")
        if state["usage"]["totals"]["total_tokens"] >= self.maximum_total_tokens:
            state["status"] = "stopped"
            state["stop_reason"] = "settled_token_ceiling_reached"
            self._write_state(state)
            return

        message = self._message(stage, state)
        result = self.invoke(stage, message, self.worktree)
        if not isinstance(result, dict):
            raise WorkerTurnCheckpointError("worker turn result must be an object")
        result = dict(result)
        result["turn_stage"] = stage
        state["usage"] = aggregate_worker_turn_usage(
            [*self._usage_results(state), result]
        )
        current = worktree_state(self.worktree)
        worker_changed_source_head = current["head"] != state["source_head"]
        output = result.get("output")
        semantic = output.get("turn_checkpoint") if isinstance(output, dict) else None
        checkpoint = None
        checkpoint_path = None
        if isinstance(semantic, dict) and not worker_changed_source_head:
            checkpoint_stage = "implement" if stage == "implement" else "verify"
            checkpoint = build_worker_turn_checkpoint(
                task_id=self.task["task_id"],
                attempt_id=state["attempt_id"],
                turn_index=len(state["model_turns"]) + 1,
                stage=checkpoint_stage,
                worktree_path=self.worktree,
                semantic_state=semantic,
            )
            checkpoint_path = publish_worker_turn_checkpoint(
                self.worktree,
                self.run_id,
                checkpoint,
            )

        if worker_changed_source_head:
            state["status"] = "failed"
            state["stop_reason"] = "worker_changed_source_head"
        state["model_turns"].append(
            {
                "stage": stage,
                "result_status": result.get("result_status"),
                "token_usage": result.get("token_usage"),
                "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
                "checkpoint_sha256": (
                    checkpoint.get("checkpoint_sha256") if checkpoint else None
                ),
                "checkpoint_bytes": (
                    checkpoint_path.stat().st_size if checkpoint_path else 0
                ),
                "worktree_state_sha256": current["state_sha256"],
                "changed_files": current["changed_files"],
            }
        )
        state["expected_worktree_state_sha256"] = current["state_sha256"]
        if stage == "repair":
            state["repair_invocations"] += 1

        if state["status"] == "failed":
            pass
        elif checkpoint is None:
            state["status"] = "failed"
            state["stop_reason"] = "missing_turn_checkpoint"
        elif result.get("result_status") != "completed" and not (
            stage == "implement"
            and _is_controller_verification_handoff(result, output)
        ):
            state["status"] = "stopped"
            state["stop_reason"] = "worker_turn_not_completed"
        else:
            try:
                additions = validate_verification_additions(
                    output.get("verification_additions")
                )
            except ValueError:
                if stage == "repair":
                    additions = state.get("verification_additions", [])
                else:
                    state["status"] = "failed"
                    state["stop_reason"] = "invalid_verification_additions"
                    additions = []
            if additions:
                state["verification_additions"] = additions
                state["phase"] = (
                    "controller_verification"
                    if stage == "implement"
                    else "post_repair_verification"
                )
        self._write_state(state)

    def _run_controller_verification(self, state, *, after_repair):
        self._require_expected_worktree(state)
        result = self.run_verification(
            state["verification_additions"], self.worktree
        )
        if not isinstance(result, dict):
            raise WorkerTurnCheckpointError("controller verification must return an object")
        route = classify_controller_verification(result, self.worktree)
        state["controller_verifications"].append(
            {
                "after_repair": after_repair,
                "route": route,
                "result": _bounded_verification_result(result),
            }
        )
        state["expected_worktree_state_sha256"] = worktree_state(self.worktree)[
            "state_sha256"
        ]

        if route == "passed":
            state["status"] = "completed"
            state["stop_reason"] = None
            state["phase"] = "done"
        elif after_repair:
            state["status"] = "stopped"
            state["stop_reason"] = f"repair_exhausted_{route}"
        elif route == "code_semantic_failure" and self.allow_repair:
            if state["usage"]["totals"]["total_tokens"] >= self.maximum_total_tokens:
                state["status"] = "stopped"
                state["stop_reason"] = "settled_token_ceiling_reached"
            else:
                state["phase"] = "repair"
        else:
            state["status"] = "stopped"
            state["stop_reason"] = route
        self._write_state(state)

    def reclassify_last_verification(self):
        """Apply the current deterministic classifier to retained evidence."""

        state = self._load_or_initialize_state()
        self._require_expected_worktree(state)
        if not state["controller_verifications"] or state["repair_invocations"]:
            return state
        retained = state["controller_verifications"][-1]
        route = classify_controller_verification(retained["result"], self.worktree)
        retained["route"] = route
        if route == "passed":
            state["status"] = "completed"
            state["stop_reason"] = None
            state["phase"] = "done"
        elif route == "code_semantic_failure" and self.allow_repair:
            if state["usage"]["totals"]["total_tokens"] >= self.maximum_total_tokens:
                state["status"] = "stopped"
                state["stop_reason"] = "settled_token_ceiling_reached"
            else:
                state["status"] = "running"
                state["stop_reason"] = None
                state["phase"] = "repair"
        else:
            state["status"] = "stopped"
            state["stop_reason"] = route
        self._write_state(state)
        return state

    def _load_or_initialize_state(self):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._validate_state_binding(state)
            return state
        initial = worktree_state(self.worktree)
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task_id": self.task["task_id"],
            "task_sha256": _digest(self.task),
            "attempt_id": f"{self.task['task_id']}-ADAPTIVE-ATTEMPT-001",
            "source_head": initial["head"],
            "status": "running",
            "stop_reason": None,
            "phase": "implementation",
            "maximum_total_tokens": self.maximum_total_tokens,
            "allow_repair": self.allow_repair,
            "model_turns": [],
            "controller_verifications": [],
            "verification_additions": [],
            "repair_invocations": 0,
            "expected_worktree_state_sha256": initial["state_sha256"],
            "usage": aggregate_worker_turn_usage([]),
        }
        self._write_state(state)
        return state

    def _validate_state_binding(self, state):
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise WorkerTurnCheckpointError("adaptive worker state schema differs")
        if state.get("run_id") != self.run_id or state.get("task_id") != self.task["task_id"]:
            raise WorkerTurnCheckpointError("adaptive worker state lineage differs")
        if state.get("task_sha256") != _digest(self.task):
            raise WorkerTurnCheckpointError("adaptive worker task binding differs")
        if state.get("maximum_total_tokens") != self.maximum_total_tokens:
            raise WorkerTurnCheckpointError("adaptive worker budget binding differs")
        if state.get("allow_repair") != self.allow_repair:
            raise WorkerTurnCheckpointError("adaptive worker repair policy differs")
        if state.get("source_head") != worktree_state(self.worktree)["head"]:
            raise WorkerTurnCheckpointError("adaptive worker source head differs")

    def _require_expected_worktree(self, state):
        current = worktree_state(self.worktree)
        if current["state_sha256"] != state["expected_worktree_state_sha256"]:
            raise WorkerTurnCheckpointError("adaptive worker worktree state is stale")

    def _message(self, stage, state):
        if stage == "implement":
            instruction = (
                "Start from the exact task-bound repository handoff. Localize only as needed "
                "inside the implementation turn, implement the complete bounded objective, "
                "and publish controller-runnable verification_additions."
            )
            input_artifacts = self.task.get("input_artifacts", [])
        else:
            failure = state["controller_verifications"][-1]
            instruction = (
                "Repair only the code-semantic verification failure in the bounded failure "
                "packet. Read the current diff before expanding source reads. Do not repair "
                "environment or policy failures."
            )
            input_artifacts = self.task.get("input_artifacts", [])
            input_artifacts = [*input_artifacts, str(self.state_path)]
        payload = {
            **self.task,
            "objective": f"{instruction}\nOriginal objective: {self.task['objective']}",
            "project": "adaptive-worker-turn-experiment",
            "run_id": self.run_id,
            "taskpack_id": self.task.get("taskpack_id", self.run_id),
            "attempt_id": f"{state['attempt_id']}-{stage.upper()}",
            "runtime_execution_session_id": f"SESSION-{state['attempt_id']}-{stage.upper()}",
            "lifecycle_owner_token": f"OWNER-{state['attempt_id']}-{stage.upper()}",
            "agent_id": f"agent-adaptive-worker-{stage}",
            "agent_role": "implementation_worker",
            "required_role": "implementation_worker",
            "usage_stage": "implementation_worker",
            "turn_stage": stage,
            "input_artifacts": input_artifacts,
            "role_prompt_contract": {
                "worker_turn_stage": stage,
                "stage_instruction": instruction,
                "checkpoint_authority": "semantic_only; git_worktree_is_code_authority",
                "checkpoint_output_contract": {
                    "output_field": "output.turn_checkpoint",
                    "required_fields": list(CHECKPOINT_FIELDS),
                    "maximum_items_per_collection": 32,
                    "source_paths": "repository-relative paths only",
                },
                "verification_output_contract": {
                    "output_field": "output.verification_additions",
                    "required": stage == "implement",
                    "command_format": "JSON list; controller executes from repository root",
                },
            },
        }
        if stage == "repair":
            payload["verification_failure_packet"] = failure
        return {
            "message_id": f"MSG-{state['attempt_id']}-{stage.upper()}",
            "from_agent": "agent-adaptive-worker-controller",
            "to_agent": f"agent-adaptive-worker-{stage}",
            "payload": payload,
        }

    def _usage_results(self, state):
        return [
            {"turn_stage": item["stage"], "token_usage": item["token_usage"]}
            for item in state["model_turns"]
        ]

    def _write_state(self, state):
        payload = _canonical_json(state) + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".", dir=self.state_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def classify_controller_verification(result, worktree_path=None):
    """Choose the next route from deterministic verification evidence."""

    status = result.get("integration_verification_additions_status")
    additions = result.get("integration_verification_additions")
    if status == "passed" and isinstance(additions, list) and additions:
        return "passed"
    if status == "rejected":
        return "verification_policy_failure"
    if status != "failed" or not isinstance(additions, list) or not additions:
        return "verification_result_ambiguous"
    if any(_clear_environment_failure(item, worktree_path) for item in additions):
        return "environment_failure"
    return "code_semantic_failure"


def _is_controller_verification_handoff(result, output):
    return (
        result.get("result_status") == "failed"
        and isinstance(output, dict)
        and output.get("verification_deferred") is True
        and output.get("verification_deferred_reason") == "tool_budget_exhausted"
    )


def _usage_binding(usage):
    if not isinstance(usage, dict):
        return None
    return tuple(
        usage.get(field)
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "usage_source",
        )
    )


def profile_execution_observations(transcript_paths):
    """Record repeated provider commands without changing provider execution."""

    commands = []
    for transcript_path in transcript_paths:
        with Path(transcript_path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = event.get("item") if isinstance(event, dict) else None
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "command_execution"
                ):
                    commands.append(str(item.get("command") or ""))
    counts = Counter(commands)
    repeated = []
    for command, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        if count < 2:
            continue
        repeated.append(
            {
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                "command_preview": command[:240],
                "execution_count": count,
                "extra_execution_count": count - 1,
                "simple_read_only_candidate": _simple_read_only_command(command),
            }
        )
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "mode": "record_only",
        "command_count": len(commands),
        "unique_command_count": len(counts),
        "repeated_command_count": len(repeated),
        "extra_execution_count": sum(item["extra_execution_count"] for item in repeated),
        "simple_read_only_extra_execution_count": sum(
            item["extra_execution_count"]
            for item in repeated
            if item["simple_read_only_candidate"]
        ),
        "repeated_commands": repeated[:64],
    }


def _clear_environment_failure(item, worktree_path=None):
    if not isinstance(item, dict) or item.get("verification_addition_status") != "failed":
        return False
    exit_code = item.get("verification_addition_exit_code")
    text = "\n".join(
        str(item.get(field) or "")
        for field in (
            "verification_addition_stdout",
            "verification_addition_stderr",
        )
    ).lower()
    if exit_code in {126, 127} or exit_code is None:
        return True
    markers = (
        "no module named pytest",
        "pytest: command not found",
        "permission denied",
        "no space left on device",
        "resource temporarily unavailable",
        "network is unreachable",
        "temporary failure in name resolution",
    )
    if any(marker in text for marker in markers):
        return True
    missing = re.search(r"no module named ['\"]([^'\"]+)", text)
    if missing and worktree_path is not None:
        top_level = missing.group(1).split(".", 1)[0]
        root = Path(worktree_path)
        return not (
            (root / f"{top_level}.py").is_file()
            or (root / top_level / "__init__.py").is_file()
        )
    return False


def _bounded_verification_result(result):
    bounded = json.loads(json.dumps(result, sort_keys=True))
    for item in bounded.get("integration_verification_additions", []):
        for field in (
            "verification_addition_stdout",
            "verification_addition_stderr",
        ):
            value = item.get(field)
            if isinstance(value, str) and len(value) > 4000:
                item[field] = value[:4000] + "\n[truncated]"
    return bounded


def _simple_read_only_command(command):
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    if len(arguments) >= 3 and arguments[0].endswith("bash") and arguments[1] == "-lc":
        command = arguments[2].strip()
    else:
        command = " ".join(arguments).strip()
    if any(marker in command for marker in (";", "|", "&&", ">", "<", "`", "$(")):
        return False
    prefixes = (
        "cat ",
        "sed -n ",
        "rg ",
        "git diff",
        "git status",
        "git show",
        "git log",
        "git rev-parse",
        "ls",
        "find ",
        "python3 -m json.tool ",
    )
    return command == "ls" or command.startswith(prefixes)


def _validate_task(task):
    if not isinstance(task, dict):
        raise WorkerTurnCheckpointError("adaptive worker task must be an object")
    required = ("task_id", "objective", "read_scope", "write_scope")
    if any(field not in task for field in required):
        raise WorkerTurnCheckpointError("adaptive worker task is incomplete")
    return json.loads(json.dumps(task, sort_keys=True))


def _digest(value):
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")

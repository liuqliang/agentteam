"""Provider-neutral runner for the bounded worker-turn experiment."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .worker_turn_checkpoint import (
    STAGES,
    WorkerTurnCheckpointError,
    aggregate_worker_turn_usage,
    build_worker_turn_checkpoint,
    load_worker_turn_checkpoint,
    publish_worker_turn_checkpoint,
    worktree_state,
)


STATE_SCHEMA_VERSION = "worker_turn_experiment_state.v1"
CHECKPOINT_FIELDS = (
    "completed_actions",
    "key_findings",
    "decisions",
    "verification",
    "remaining_objective",
    "source_paths",
)


class WorkerTurnExperimentRunner:
    """Run locate, implement, and verify turns in one persistent worktree."""

    def __init__(
        self,
        *,
        worktree_path,
        output_dir,
        run_id,
        task,
        invoke,
        maximum_total_tokens,
    ):
        self.worktree = Path(worktree_path).resolve(strict=True)
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = str(run_id)
        self.task = _validate_task(task)
        self.invoke = invoke
        self.maximum_total_tokens = int(maximum_total_tokens)
        if self.maximum_total_tokens < 1:
            raise WorkerTurnCheckpointError("maximum_total_tokens must be positive")
        self.state_path = self.output_dir / "worker-turn-state.json"

    def run(self):
        state = self._load_or_initialize_state()
        if state["status"] in {"completed", "stopped", "failed"}:
            return state
        while state["next_stage"] is not None:
            if state["usage"]["totals"]["total_tokens"] >= self.maximum_total_tokens:
                state["status"] = "stopped"
                state["stop_reason"] = "settled_token_ceiling_reached"
                self._write_state(state)
                return state
            stage = state["next_stage"]
            checkpoint_path = self._validated_predecessor(state, stage)
            state_before = worktree_state(self.worktree)
            message = self._message(stage, state, checkpoint_path)
            result = self.invoke(stage, message, self.worktree)
            if not isinstance(result, dict):
                raise WorkerTurnCheckpointError("worker turn result must be an object")
            result = dict(result)
            result["turn_stage"] = stage
            aggregate = aggregate_worker_turn_usage(
                [*self._usage_results(state), result]
            )
            output = result.get("output")
            semantic = output.get("turn_checkpoint") if isinstance(output, dict) else None
            checkpoint = None
            published_path = None
            if isinstance(semantic, dict):
                checkpoint = build_worker_turn_checkpoint(
                    task_id=self.task["task_id"],
                    attempt_id=state["attempt_id"],
                    turn_index=len(state["turns"]) + 1,
                    stage=stage,
                    worktree_path=self.worktree,
                    semantic_state=semantic,
                )
                published_path = publish_worker_turn_checkpoint(
                    self.worktree,
                    self.run_id,
                    checkpoint,
                )
            if checkpoint is None:
                state["status"] = "failed"
                state["stop_reason"] = "missing_turn_checkpoint"
            elif stage == "locate" and worktree_state(self.worktree) != state_before:
                state["status"] = "failed"
                state["stop_reason"] = "locate_turn_modified_worktree"
            elif result.get("result_status") != "completed":
                state["status"] = "stopped"
                state["stop_reason"] = "worker_turn_not_completed"
            elif stage == "verify":
                state["status"] = "completed"
                state["stop_reason"] = None
            else:
                state["next_stage"] = STAGES[STAGES.index(stage) + 1]
            state["usage"] = aggregate
            state["turns"].append(
                {
                    "stage": stage,
                    "result_status": result.get("result_status"),
                    "token_usage": result.get("token_usage"),
                    "checkpoint_path": str(published_path) if published_path else None,
                    "checkpoint_sha256": (
                        checkpoint.get("checkpoint_sha256") if checkpoint else None
                    ),
                    "checkpoint_bytes": (
                        published_path.stat().st_size if published_path else 0
                    ),
                    "worktree_state_sha256": worktree_state(self.worktree)[
                        "state_sha256"
                    ],
                    "changed_files": worktree_state(self.worktree)["changed_files"],
                }
            )
            if state["status"] == "running" and (
                state["usage"]["totals"]["total_tokens"]
                >= self.maximum_total_tokens
            ):
                state["status"] = "stopped"
                state["stop_reason"] = "settled_token_ceiling_reached"
            self._write_state(state)
            if state["status"] != "running":
                return state
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
            "attempt_id": f"{self.task['task_id']}-BOUNDED-ATTEMPT-001",
            "source_head": initial["head"],
            "status": "running",
            "stop_reason": None,
            "next_stage": "locate",
            "maximum_total_tokens": self.maximum_total_tokens,
            "turns": [],
            "usage": aggregate_worker_turn_usage([]),
        }
        self._write_state(state)
        return state

    def _validate_state_binding(self, state):
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise WorkerTurnCheckpointError("worker turn state schema differs")
        if state.get("run_id") != self.run_id or state.get("task_id") != self.task["task_id"]:
            raise WorkerTurnCheckpointError("worker turn state lineage differs")
        if state.get("task_sha256") != _digest(self.task):
            raise WorkerTurnCheckpointError("worker turn task binding differs")
        if state.get("maximum_total_tokens") != self.maximum_total_tokens:
            raise WorkerTurnCheckpointError("worker turn budget binding differs")

    def _validated_predecessor(self, state, stage):
        if not state["turns"]:
            if stage != "locate":
                raise WorkerTurnCheckpointError("first worker turn must be locate")
            if worktree_state(self.worktree)["head"] != state["source_head"]:
                raise WorkerTurnCheckpointError("worker source head differs")
            return None
        predecessor = state["turns"][-1]
        path = predecessor.get("checkpoint_path")
        if not path:
            raise WorkerTurnCheckpointError("worker predecessor checkpoint is missing")
        load_worker_turn_checkpoint(
            path,
            task_id=self.task["task_id"],
            attempt_id=state["attempt_id"],
            expected_stage=stage,
            worktree_path=self.worktree,
        )
        return path

    def _message(self, stage, state, checkpoint_path):
        stage_contracts = {
            "locate": (
                "Read the taskpack and only the source needed to establish the complete "
                "call chain. Do not modify files or run broad tests. Finish after publishing "
                "a compact turn_checkpoint."
            ),
            "implement": (
                "Read the predecessor checkpoint first. Inspect only named locations unless "
                "new evidence requires expansion. Implement within write_scope, run only a "
                "syntax or narrow smoke check, and publish a compact turn_checkpoint."
            ),
            "verify": (
                "Read the predecessor checkpoint and current git diff first. Run focused "
                "verification, make bounded repairs within write_scope, then publish the final "
                "operator_summary and a terminal turn_checkpoint."
            ),
        }
        objective = (
            f"Worker stage {stage}: {stage_contracts[stage]}\n"
            f"Original objective: {self.task['objective']}"
        )
        payload = {
            **self.task,
            "objective": objective,
            "project": "worker-turn-experiment",
            "run_id": self.run_id,
            "taskpack_id": self.task.get("taskpack_id", self.run_id),
            "attempt_id": f"{state['attempt_id']}-{stage.upper()}",
            "runtime_execution_session_id": f"SESSION-{state['attempt_id']}-{stage.upper()}",
            "lifecycle_owner_token": f"OWNER-{state['attempt_id']}-{stage.upper()}",
            "agent_id": f"agent-worker-turn-{stage}",
            "agent_role": "implementation_worker",
            "required_role": "implementation_worker",
            "usage_stage": "implementation_worker",
            "turn_stage": stage,
            "turn_checkpoint_path": checkpoint_path,
            "input_artifacts": (
                [checkpoint_path]
                if checkpoint_path
                else self.task.get("input_artifacts", [])
            ),
            "role_prompt_contract": {
                "worker_turn_stage": stage,
                "stage_instruction": stage_contracts[stage],
                "checkpoint_authority": "semantic_only; git_worktree_is_code_authority",
                "checkpoint_output_contract": {
                    "output_field": "output.turn_checkpoint",
                    "required_fields": list(CHECKPOINT_FIELDS),
                    "maximum_items_per_collection": 32,
                    "remaining_objective": "one string; use an empty string only for verify",
                    "source_paths": "repository-relative paths only",
                },
            },
        }
        return {
            "message_id": f"MSG-{state['attempt_id']}-{stage.upper()}",
            "from_agent": "agent-worker-turn-controller",
            "to_agent": f"agent-worker-turn-{stage}",
            "payload": payload,
        }

    def _usage_results(self, state):
        return [
            {"turn_stage": item["stage"], "token_usage": item["token_usage"]}
            for item in state["turns"]
        ]

    def _write_state(self, state):
        payload = json.dumps(
            state,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii") + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".",
            dir=self.state_path.parent,
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


def _validate_task(task):
    if not isinstance(task, dict):
        raise WorkerTurnCheckpointError("worker turn task must be an object")
    required = ("task_id", "objective", "read_scope", "write_scope")
    if any(field not in task for field in required):
        raise WorkerTurnCheckpointError("worker turn task is incomplete")
    return json.loads(json.dumps(task, sort_keys=True))


def _digest(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()

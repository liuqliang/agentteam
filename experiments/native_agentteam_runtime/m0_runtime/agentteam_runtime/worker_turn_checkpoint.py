"""Experimental bounded worker-turn checkpoints.

Git and the worker worktree remain authoritative for code.  These checkpoints
carry only the semantic state needed to start a fresh provider turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = "worker_turn_checkpoint.v1"
STAGES = ("locate", "implement", "verify")
NEXT_STAGE = {"locate": "implement", "implement": "verify", "verify": None}
MAX_COLLECTION_ITEMS = 32
MAX_TEXT_LENGTH = 2000


class WorkerTurnCheckpointError(RuntimeError):
    """Raised when a worker-turn checkpoint is invalid or stale."""


def worktree_state(worktree_path):
    worktree = Path(worktree_path).resolve(strict=True)
    head = _git(worktree, "rev-parse", "HEAD").strip()
    status = _git_bytes(worktree, "status", "--porcelain=v1", "-z")
    diff = _git_bytes(worktree, "diff", "--binary", "HEAD", "--")
    untracked = _git_bytes(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    digest = hashlib.sha256()
    digest.update(b"head\0" + head.encode("ascii") + b"\0")
    digest.update(b"status\0" + status + b"\0diff\0" + diff)
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        relative = raw_path.decode("utf-8")
        path = worktree / relative
        digest.update(b"\0untracked\0" + raw_path + b"\0")
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(path.read_bytes())
    return {
        "head": head,
        "changed_files": _changed_files(worktree),
        "state_sha256": digest.hexdigest(),
    }


def build_worker_turn_checkpoint(
    *,
    task_id,
    attempt_id,
    turn_index,
    stage,
    worktree_path,
    semantic_state,
):
    if stage not in STAGES:
        raise WorkerTurnCheckpointError(f"unsupported worker stage: {stage}")
    if not isinstance(turn_index, int) or turn_index < 1:
        raise WorkerTurnCheckpointError("turn_index must be a positive integer")
    semantics = _validate_semantic_state(semantic_state, stage=stage)
    state = worktree_state(worktree_path)
    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "task_id": _required_text(task_id, "task_id"),
        "attempt_id": _required_text(attempt_id, "attempt_id"),
        "turn_index": turn_index,
        "stage": stage,
        "next_stage": NEXT_STAGE[stage],
        "worktree_head": state["head"],
        "worktree_state_sha256": state["state_sha256"],
        "changed_files": state["changed_files"],
        **semantics,
    }
    checkpoint["checkpoint_sha256"] = _checkpoint_digest(checkpoint)
    return checkpoint


def validate_worker_turn_checkpoint(
    checkpoint,
    *,
    task_id=None,
    attempt_id=None,
    expected_stage=None,
    worktree_path=None,
):
    if not isinstance(checkpoint, dict):
        raise WorkerTurnCheckpointError("checkpoint must be an object")
    if checkpoint.get("schema_version") != SCHEMA_VERSION:
        raise WorkerTurnCheckpointError("checkpoint schema_version is invalid")
    stage = checkpoint.get("stage")
    if stage not in STAGES or checkpoint.get("next_stage") != NEXT_STAGE[stage]:
        raise WorkerTurnCheckpointError("checkpoint stage transition is invalid")
    if expected_stage is not None and checkpoint.get("next_stage") != expected_stage:
        raise WorkerTurnCheckpointError("checkpoint does not lead to expected stage")
    if task_id is not None and checkpoint.get("task_id") != task_id:
        raise WorkerTurnCheckpointError("checkpoint task lineage differs")
    if attempt_id is not None and checkpoint.get("attempt_id") != attempt_id:
        raise WorkerTurnCheckpointError("checkpoint attempt lineage differs")
    if checkpoint.get("checkpoint_sha256") != _checkpoint_digest(checkpoint):
        raise WorkerTurnCheckpointError("checkpoint digest differs")
    _required_text(checkpoint.get("task_id"), "task_id")
    _required_text(checkpoint.get("attempt_id"), "attempt_id")
    if not isinstance(checkpoint.get("turn_index"), int) or checkpoint["turn_index"] < 1:
        raise WorkerTurnCheckpointError("checkpoint turn_index is invalid")
    _validate_semantic_state(checkpoint, stage=stage)
    _validate_paths(checkpoint.get("changed_files"), "changed_files")
    if worktree_path is not None:
        current = worktree_state(worktree_path)
        if checkpoint.get("worktree_head") != current["head"]:
            raise WorkerTurnCheckpointError("checkpoint worktree head is stale")
        if checkpoint.get("worktree_state_sha256") != current["state_sha256"]:
            raise WorkerTurnCheckpointError("checkpoint worktree state is stale")
    return dict(checkpoint)


def publish_worker_turn_checkpoint(worktree_path, run_id, checkpoint):
    validated = validate_worker_turn_checkpoint(
        checkpoint,
        task_id=checkpoint.get("task_id"),
        attempt_id=checkpoint.get("attempt_id"),
        worktree_path=worktree_path,
    )
    git_dir = Path(_git(Path(worktree_path), "rev-parse", "--absolute-git-dir").strip())
    destination = (
        git_dir
        / "agentteam-worker-turns"
        / _path_component(run_id)
        / f"turn-{validated['turn_index']:02d}-{validated['stage']}.json"
    )
    payload = _canonical_json(validated) + b"\n"
    if destination.exists():
        if destination.read_bytes() != payload:
            raise WorkerTurnCheckpointError(
                f"checkpoint path already contains different content: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        dir=destination.parent,
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return destination


def load_worker_turn_checkpoint(
    path,
    *,
    task_id=None,
    attempt_id=None,
    expected_stage=None,
    worktree_path=None,
):
    try:
        checkpoint = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerTurnCheckpointError(f"checkpoint cannot be read: {path}") from exc
    return validate_worker_turn_checkpoint(
        checkpoint,
        task_id=task_id,
        attempt_id=attempt_id,
        expected_stage=expected_stage,
        worktree_path=worktree_path,
    )


def aggregate_worker_turn_usage(turn_results):
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    stages = []
    for result in turn_results:
        usage = result.get("token_usage") if isinstance(result, dict) else None
        if (
            not isinstance(usage, dict)
            or usage.get("usage_status") not in {None, "reported"}
            or not all(
                isinstance(usage.get(field), int)
                for field in ("input_tokens", "output_tokens", "total_tokens")
            )
        ):
            raise WorkerTurnCheckpointError("worker turn usage is unavailable")
        stage = result.get("turn_stage")
        stage_usage = {"stage": stage}
        for field in totals:
            value = usage.get(field)
            if field == "uncached_input_tokens" and value is None:
                value = max(
                    int(usage.get("input_tokens") or 0)
                    - int(usage.get("cached_input_tokens") or 0),
                    0,
                )
            if not isinstance(value, int) or value < 0:
                raise WorkerTurnCheckpointError(
                    f"worker turn usage field is invalid: {field}"
                )
            totals[field] += value
            stage_usage[field] = value
        stages.append(stage_usage)
    return {
        "usage_status": "reported",
        "invocation_count": len(stages),
        "totals": totals,
        "stages": stages,
    }


def profile_worker_turn_transcripts(transcript_paths, source_paths):
    normalized_paths = [_relative_path(item, "source_paths") for item in source_paths]
    per_turn = []
    path_turn_counts = {path: 0 for path in normalized_paths}
    total_commands = 0
    for path in transcript_paths:
        seen = set()
        command_count = 0
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = event.get("item") if isinstance(event, dict) else None
                if (
                    event.get("type") != "item.completed"
                    or not isinstance(item, dict)
                    or item.get("type") != "command_execution"
                ):
                    continue
                command_count += 1
                command = str(item.get("command") or "")
                for source_path in normalized_paths:
                    if source_path in command:
                        seen.add(source_path)
        total_commands += command_count
        for source_path in seen:
            path_turn_counts[source_path] += 1
        per_turn.append(
            {
                "transcript_path": str(path),
                "command_count": command_count,
                "source_paths_read": sorted(seen),
            }
        )
    return {
        "command_count": total_commands,
        "per_turn": per_turn,
        "source_path_turn_counts": path_turn_counts,
        "cross_turn_repeated_reads": sum(
            max(count - 1, 0) for count in path_turn_counts.values()
        ),
    }


def _validate_semantic_state(value, *, stage):
    if not isinstance(value, dict):
        raise WorkerTurnCheckpointError("semantic_state must be an object")
    required = (
        "completed_actions",
        "key_findings",
        "decisions",
        "verification",
        "remaining_objective",
    )
    result = {}
    for field in required[:-1]:
        items = value.get(field)
        if not isinstance(items, list) or len(items) > MAX_COLLECTION_ITEMS:
            raise WorkerTurnCheckpointError(f"checkpoint {field} is invalid")
        result[field] = [_bounded_json_item(item, field) for item in items]
    remaining = value.get("remaining_objective")
    if isinstance(remaining, list) and all(
        isinstance(item, str) and item.strip() for item in remaining
    ):
        remaining = "\n".join(f"- {item}" for item in remaining)
    result["remaining_objective"] = _bounded_text(
        remaining,
        "remaining_objective",
        allow_empty=stage == "verify",
    )
    source_paths = value.get("source_paths", [])
    _validate_paths(source_paths, "source_paths")
    result["source_paths"] = list(source_paths)
    return result


def _bounded_json_item(value, field):
    if not isinstance(value, (str, dict)):
        raise WorkerTurnCheckpointError(f"checkpoint {field} entries are invalid")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True)
    if len(encoded) > MAX_TEXT_LENGTH:
        raise WorkerTurnCheckpointError(f"checkpoint {field} entry is too large")
    return value


def _bounded_text(value, field, *, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WorkerTurnCheckpointError(f"checkpoint {field} is invalid")
    if len(value) > MAX_TEXT_LENGTH:
        raise WorkerTurnCheckpointError(f"checkpoint {field} is too large")
    return value


def _required_text(value, field):
    return _bounded_text(value, field)


def _validate_paths(paths, field):
    if not isinstance(paths, list) or len(paths) > MAX_COLLECTION_ITEMS:
        raise WorkerTurnCheckpointError(f"checkpoint {field} is invalid")
    for value in paths:
        _relative_path(value, field)


def _relative_path(value, field):
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkerTurnCheckpointError(f"checkpoint {field} path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkerTurnCheckpointError(f"checkpoint {field} path escapes repository")
    return value


def _checkpoint_digest(checkpoint):
    content = dict(checkpoint)
    content.pop("checkpoint_sha256", None)
    return hashlib.sha256(_canonical_json(content)).hexdigest()


def _canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _changed_files(worktree):
    changed = set(
        item
        for item in _git(worktree, "diff", "--name-only", "HEAD", "--").splitlines()
        if item
    )
    changed.update(
        item
        for item in _git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
        ).splitlines()
        if item
    )
    return sorted(changed)


def _path_component(value):
    text = _required_text(value, "run_id")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(character not in allowed for character in text):
        raise WorkerTurnCheckpointError("run_id contains unsupported characters")
    return text


def _git(worktree, *arguments):
    return subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _git_bytes(worktree, *arguments):
    return subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout

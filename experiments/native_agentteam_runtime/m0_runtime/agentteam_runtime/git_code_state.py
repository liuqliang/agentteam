"""Git-backed code-state authority for decision-bound attempts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .decision_ledger import DecisionLedger
from .decision_runtime import require_active_inherited_decision
from .experiment_contract import canonical_json_bytes


class GitCodeStateError(RuntimeError):
    """Raised when a protected code-state ref or object is invalid."""


class GitCodeStateUnavailable(GitCodeStateError):
    """Raised when no recoverable code state exists for an attempt."""


def attempt_code_state_ref(run_id, task_id, attempt_id):
    return "refs/agentteam/runs/{}/{}/{}".format(
        _ref_component(run_id),
        _ref_component(task_id),
        _ref_component(attempt_id),
    )


def checkpoint_code_state_ref(decision_id, task_id):
    return "refs/agentteam/checkpoints/{}/{}".format(
        _ref_component(decision_id),
        _ref_component(task_id),
    )


def attempt_code_state_artifact_id(run_id, task_id, attempt_id):
    digest = hashlib.sha256(
        canonical_json_bytes(
            {"run_id": run_id, "task_id": task_id, "attempt_id": attempt_id}
        )
    ).hexdigest()[:24]
    return "ART-code-attempt-" + digest


def publish_attempt_code_state(
    binding,
    *,
    project_root,
    worktree_path,
    run_id,
    task_id,
    attempt_id,
    changed_files,
    created_at,
    validation_status,
):
    if binding is None or not worktree_path:
        return None
    decision_id = require_active_inherited_decision(binding, task_id)
    project_root = Path(project_root).resolve(strict=True)
    worktree_path = Path(worktree_path).resolve(strict=True)
    retained_ref = attempt_code_state_ref(run_id, task_id, attempt_id)
    checkpoint_ref = checkpoint_code_state_ref(decision_id, task_id)
    existing = _optional_ref(project_root, retained_ref)

    code_state_tree = _code_state_tree(worktree_path, changed_files)
    if existing is not None:
        existing_tree = _git_output(project_root, "rev-parse", f"{existing}^{{tree}}")
        if existing_tree != code_state_tree:
            raise GitCodeStateError(
                f"protected attempt ref conflicts with current tree: {retained_ref}"
            )
        commit_sha = existing
        publication_status = "reused_existing"
    else:
        commit_sha, commit_status = _create_code_state_commit(
            worktree_path,
            code_state_tree=code_state_tree,
            task_id=task_id,
            attempt_id=attempt_id,
            decision_id=decision_id,
            created_at=created_at,
            validation_status=validation_status,
        )
        _ensure_commit_object(project_root, worktree_path, commit_sha)
        _create_immutable_ref(project_root, retained_ref, commit_sha)
        publication_status = commit_status

    _ensure_commit_object(project_root, worktree_path, commit_sha)
    _advance_checkpoint_ref(project_root, checkpoint_ref, commit_sha)
    artifact_id = attempt_code_state_artifact_id(run_id, task_id, attempt_id)
    algorithm = _git_digest_algorithm(project_root)
    DecisionLedger(binding["work_root"]).append_artifact_link(
        _attempt_artifact_link(
            artifact_id=artifact_id,
            decision_id=decision_id,
            commit_sha=commit_sha,
            digest_algorithm=algorithm,
            created_at=_commit_created_at(project_root, commit_sha),
        )
    )
    return {
        "code_state_status": publication_status,
        "code_state_artifact_id": artifact_id,
        "code_state_decision_id": decision_id,
        "code_state_commit_sha": commit_sha,
        "code_state_ref": retained_ref,
        "checkpoint_ref": checkpoint_ref,
        "git_object_format": "sha256" if algorithm == "git_sha256" else "sha1",
    }


def publish_integration_code_state(
    binding,
    *,
    project_root,
    decision_id,
    commit_sha,
    run_id,
    task_id,
    attempt_id,
    created_at,
    source_repository=None,
):
    if binding is None or not commit_sha:
        return None
    project_root = Path(project_root).resolve(strict=True)
    _ensure_commit_object(
        project_root,
        Path(source_repository).resolve(strict=True)
        if source_repository
        else project_root,
        commit_sha,
    )
    retained_ref = "refs/agentteam/runs/{}/integration/{}".format(
        _ref_component(run_id),
        _ref_component(attempt_id),
    )
    _create_immutable_ref(project_root, retained_ref, commit_sha)
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "run_id": run_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "decision_id": decision_id,
                "commit_sha": commit_sha,
            }
        )
    ).hexdigest()[:24]
    artifact_id = "ART-code-integration-" + identity
    algorithm = _git_digest_algorithm(project_root)
    DecisionLedger(binding["work_root"]).append_artifact_link(
        {
            "schema_version": "decision_artifact_link.v1",
            "artifact_id": artifact_id,
            "decision_id": decision_id,
            "artifact_kind": "code_state",
            "locator": f"git:commit:{commit_sha}",
            "digest_algorithm": algorithm,
            "digest": commit_sha,
            "producer": "agentteam-integration-controller",
            "created_at": created_at,
        }
    )
    return {
        "integration_code_state_artifact_id": artifact_id,
        "integration_code_state_commit_sha": commit_sha,
        "integration_code_state_decision_id": decision_id,
        "integration_code_state_ref": retained_ref,
    }


def resolve_attempt_code_state(
    binding,
    *,
    project_root,
    run_id,
    task_id,
    attempt_id,
    repair_missing_link=False,
):
    if binding is None:
        raise GitCodeStateUnavailable("legacy attempt has no decision-bound code state")
    decision_id = require_active_inherited_decision(binding, task_id)
    project_root = Path(project_root).resolve(strict=True)
    retained_ref = attempt_code_state_ref(run_id, task_id, attempt_id)
    commit_sha = _optional_ref(project_root, retained_ref)
    if commit_sha is None:
        raise GitCodeStateUnavailable(
            f"protected attempt ref is missing: {retained_ref}"
        )
    artifact_id = attempt_code_state_artifact_id(run_id, task_id, attempt_id)
    links = {
        item["artifact_id"]: item
        for item in DecisionLedger(binding["work_root"]).artifact_links(decision_id)
    }
    link = links.get(artifact_id)
    if link is None:
        if not repair_missing_link:
            raise GitCodeStateError(
                f"protected attempt ref has no decision artifact: {retained_ref}"
            )
        _validate_attempt_commit(
            project_root,
            commit_sha,
            decision_id=decision_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        link = _attempt_artifact_link(
            artifact_id=artifact_id,
            decision_id=decision_id,
            commit_sha=commit_sha,
            digest_algorithm=_git_digest_algorithm(project_root),
            created_at=_commit_created_at(project_root, commit_sha),
        )
        DecisionLedger(binding["work_root"]).append_artifact_link(link)
    expected_locator = f"git:commit:{commit_sha}"
    if link["locator"] != expected_locator or link["digest"] != commit_sha:
        raise GitCodeStateError(
            f"protected attempt ref differs from decision artifact: {retained_ref}"
        )
    _require_commit(project_root, commit_sha)
    return {
        "code_state_artifact_id": artifact_id,
        "code_state_decision_id": decision_id,
        "code_state_commit_sha": commit_sha,
        "code_state_ref": retained_ref,
        "checkpoint_ref": checkpoint_code_state_ref(decision_id, task_id),
    }


def restore_attempt_workspace(
    binding,
    *,
    project_root,
    destination,
    run_id,
    task_id,
    attempt_id,
    independent=False,
    events_path=None,
):
    resolved = resolve_attempt_code_state(
        binding,
        project_root=project_root,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        repair_missing_link=True,
    )
    project_root = Path(project_root).resolve(strict=True)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise GitCodeStateError(f"recovery destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    restore_base = _attempt_restore_base(
        project_root,
        resolved["code_state_commit_sha"],
    )
    if independent:
        _clone_detached(
            project_root,
            destination,
            restore_base,
            retained_ref=resolved["code_state_ref"],
        )
    else:
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "prune"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "worktree",
                "add",
                "--detach",
                str(destination),
                restore_base,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    if restore_base != resolved["code_state_commit_sha"]:
        _apply_commit_delta(
            destination,
            restore_base,
            resolved["code_state_commit_sha"],
        )
    checkpoint_sequence, event_tail = _event_tail_after_code_state(
        events_path,
        resolved["code_state_artifact_id"],
    )
    return {
        **resolved,
        "worktree_path": str(destination.resolve()),
        "recovery_status": "restored_from_code_state",
        "recovery_base_sha": restore_base,
        "checkpoint_event_sequence": checkpoint_sequence,
        "event_tail": event_tail,
        "event_tail_count": len(event_tail),
    }


def _create_code_state_commit(
    worktree,
    *,
    code_state_tree,
    task_id,
    attempt_id,
    decision_id,
    created_at,
    validation_status,
):
    head = _git_output(worktree, "rev-parse", "HEAD")
    head_tree = _git_output(worktree, "rev-parse", "HEAD^{tree}")
    commit_status = "unchanged" if code_state_tree == head_tree else "committed"
    message = (
        f"AgentTeam code state {task_id} {attempt_id}\n\n"
        f"AgentTeam-Decision: {decision_id}\n"
        f"AgentTeam-Task: {task_id}\n"
        f"AgentTeam-Verification: {validation_status}"
    )
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "AgentTeam",
            "GIT_AUTHOR_EMAIL": "agentteam@localhost",
            "GIT_AUTHOR_DATE": created_at,
            "GIT_COMMITTER_NAME": "AgentTeam",
            "GIT_COMMITTER_EMAIL": "agentteam@localhost",
            "GIT_COMMITTER_DATE": created_at,
        }
    )
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=AgentTeam",
            "-c",
            "user.email=agentteam@localhost",
            "commit-tree",
            code_state_tree,
            "-p",
            head,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        input=message + "\n",
    )
    if completed.returncode != 0:
        raise GitCodeStateError(completed.stderr.strip() or "code-state commit failed")
    return completed.stdout.strip(), commit_status


def _attempt_artifact_link(
    *,
    artifact_id,
    decision_id,
    commit_sha,
    digest_algorithm,
    created_at,
):
    return {
        "schema_version": "decision_artifact_link.v1",
        "artifact_id": artifact_id,
        "decision_id": decision_id,
        "artifact_kind": "code_state",
        "locator": f"git:commit:{commit_sha}",
        "digest_algorithm": digest_algorithm,
        "digest": commit_sha,
        "producer": "agentteam-git-code-state",
        "created_at": created_at,
    }


def _validate_attempt_commit(
    repo,
    commit_sha,
    *,
    decision_id,
    task_id,
    attempt_id,
):
    _require_commit(repo, commit_sha)
    message = _git_output(repo, "show", "-s", "--format=%B", commit_sha)
    lines = message.splitlines()
    expected_subject = f"AgentTeam code state {task_id} {attempt_id}"
    if not lines or lines[0] != expected_subject:
        raise GitCodeStateError("unlinked code-state commit subject is invalid")
    trailers = {}
    for line in lines[1:]:
        if ": " in line:
            key, value = line.split(": ", 1)
            trailers[key] = value
    expected = {
        "AgentTeam-Decision": decision_id,
        "AgentTeam-Task": task_id,
    }
    for key, value in expected.items():
        if trailers.get(key) != value:
            raise GitCodeStateError(
                f"unlinked code-state commit {key} trailer is invalid"
            )


def _commit_created_at(repo, commit_sha):
    return _git_output(repo, "show", "-s", "--format=%cI", commit_sha)


def _code_state_tree(worktree, changed_files):
    paths = sorted(
        {
            str(path)
            for path in (changed_files or [])
            if isinstance(path, str) and path
        }
    )
    descriptor, index_path = tempfile.mkstemp(prefix="agentteam-code-state-index-")
    os.close(descriptor)
    Path(index_path).unlink()
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index_path
    try:
        subprocess.run(
            ["git", "-C", str(worktree), "read-tree", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        if paths:
            subprocess.run(
                ["git", "-C", str(worktree), "add", "--all", "--", *paths],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        return subprocess.run(
            ["git", "-C", str(worktree), "write-tree"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        ).stdout.strip()
    finally:
        Path(index_path).unlink(missing_ok=True)


def _ensure_commit_object(project_root, source_repository, commit_sha):
    if _commit_exists(project_root, commit_sha):
        return
    export_ref = "refs/agentteam/export/" + hashlib.sha256(
        commit_sha.encode("ascii")
    ).hexdigest()[:24]
    _create_immutable_ref(source_repository, export_ref, commit_sha)
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "fetch",
                "--no-tags",
                "--quiet",
                str(source_repository),
                export_ref,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        subprocess.run(
            ["git", "-C", str(source_repository), "update-ref", "-d", export_ref],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    _require_commit(project_root, commit_sha)


def _create_immutable_ref(repo, ref, commit_sha):
    completed = subprocess.run(
        ["git", "-C", str(repo), "update-ref", ref, commit_sha, ""],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode == 0:
        return
    existing = _optional_ref(repo, ref)
    if existing == commit_sha:
        return
    raise GitCodeStateError(
        completed.stderr.strip() or f"protected attempt ref conflict: {ref}"
    )


def _advance_checkpoint_ref(repo, ref, commit_sha):
    previous = _optional_ref(repo, ref)
    if previous == commit_sha:
        return
    command = ["git", "-C", str(repo), "update-ref", ref, commit_sha]
    command.append(previous or "")
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise GitCodeStateError(
            completed.stderr.strip() or f"checkpoint ref update conflict: {ref}"
        )


def _clone_detached(project_root, destination, commit_sha, *, retained_ref=None):
    subprocess.run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-hardlinks",
            "--no-checkout",
            str(project_root),
            str(destination),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if retained_ref is not None:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(destination),
                    "fetch",
                    "--no-tags",
                    "--quiet",
                    "origin",
                    retained_ref,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        subprocess.run(
            ["git", "-C", str(destination), "checkout", "--detach", commit_sha],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(destination), "remote", "remove", "origin"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _attempt_restore_base(repo, commit_sha):
    message = _git_output(repo, "show", "-s", "--format=%B", commit_sha)
    if not message.startswith("AgentTeam code state "):
        return commit_sha
    parents = _git_output(repo, "rev-list", "--parents", "-n", "1", commit_sha).split()
    if len(parents) != 2:
        raise GitCodeStateError("attempt code-state commit must have one parent")
    return parents[1]


def _apply_commit_delta(worktree, base_sha, commit_sha):
    delta = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "diff",
            "--binary",
            base_sha,
            commit_sha,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    completed = subprocess.run(
        ["git", "-C", str(worktree), "apply", "--binary", "-"],
        input=delta,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise GitCodeStateError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "cannot restore attempt delta"
        )


def _event_tail_after_code_state(events_path, artifact_id):
    if events_path is None or not Path(events_path).is_file():
        return None, []
    events = []
    for line in Path(events_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GitCodeStateError("event journal is invalid during recovery") from exc
        events.append(event)
    checkpoint = None
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if (
            event.get("event_type") == "code_state_published"
            and payload.get("code_state_artifact_id") == artifact_id
        ):
            checkpoint = event.get("sequence")
    if checkpoint is None:
        return None, events
    return checkpoint, [
        event for event in events if int(event.get("sequence", 0)) > checkpoint
    ]


def _optional_ref(repo, ref):
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _require_commit(repo, commit_sha):
    if not _commit_exists(repo, commit_sha):
        raise GitCodeStateError(f"Git commit object is missing: {commit_sha}")


def _commit_exists(repo, commit_sha):
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).returncode == 0


def _git_digest_algorithm(repo):
    object_format = _git_output(repo, "rev-parse", "--show-object-format")
    if object_format == "sha1":
        return "git_sha1"
    if object_format == "sha256":
        return "git_sha256"
    raise GitCodeStateError(f"unsupported Git object format: {object_format}")


def _git_output(repo, *arguments):
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _ref_component(value):
    raw = str(value)
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-") or "item"
    if len(normalized) <= 64 and normalized == raw:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return normalized[:48].rstrip(".-") + "-" + digest

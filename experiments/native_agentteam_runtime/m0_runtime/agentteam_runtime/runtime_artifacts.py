import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


def persist_runtime_artifacts(
    output_dir,
    worktree_path,
    artifact_digests,
    *,
    task_id,
    attempt_id,
):
    store_root = Path(output_dir) / "runtime_artifacts"
    manifest_path = store_root / "manifest.json"
    with _runtime_artifact_store_lock(store_root, exclusive=True):
        manifest = _read_runtime_artifact_manifest(manifest_path)
        records = manifest.setdefault("artifacts", {})
        persisted = []
        for relative_path in sorted(artifact_digests):
            source = _safe_runtime_artifact_path(
                worktree_path,
                relative_path,
                require_file=True,
            )
            stored = _store_runtime_artifact_object(
                source,
                store_root / "objects",
            )
            if stored["sha256"] != artifact_digests[relative_path]:
                raise RuntimeError(
                    f"runtime artifact changed after audit: {relative_path}"
                )
            record = {
                "artifact_path": relative_path,
                "attempt_id": attempt_id,
                "byte_count": stored["byte_count"],
                "sha256": stored["sha256"],
                "task_id": task_id,
            }
            existing = records.get(relative_path)
            if existing and existing != record:
                raise RuntimeError(
                    f"runtime artifact record is immutable: {relative_path}"
                )
            records[relative_path] = record
            persisted.append(record)
        _write_json_atomically(manifest_path, manifest)
        return persisted


def bootstrap_completed_runtime_artifacts(
    output_dir,
    project_root,
    backlog,
    *,
    source_ref="HEAD",
):
    """Import consumed outputs of completed tasks from the frozen Git baseline."""

    if project_root is None:
        return []
    items = backlog.get("items", []) if isinstance(backlog, dict) else []
    tasks_by_id = {
        item.get("task_id"): item
        for item in items
        if isinstance(item, dict) and item.get("task_id")
    }
    required = {}
    for consumer in items:
        if not isinstance(consumer, dict) or consumer.get("backlog_status") == "done":
            continue
        for artifact_path, producer_id in runtime_input_artifact_producers(
            backlog,
            consumer,
        ).items():
            producer = tasks_by_id.get(producer_id, {})
            if producer.get("backlog_status") == "done":
                required[artifact_path] = producer_id
    if not required:
        return []

    project_root = Path(project_root)
    source_commit = _git_output(
        project_root,
        ["rev-parse", source_ref],
    ).decode("ascii").strip()
    store_root = Path(output_dir) / "runtime_artifacts"
    manifest_path = store_root / "manifest.json"
    with _runtime_artifact_store_lock(store_root, exclusive=True):
        manifest = _read_runtime_artifact_manifest(manifest_path)
        records = manifest.setdefault("artifacts", {})
        imported = []
        for relative_path, producer_id in sorted(required.items()):
            existing = records.get(relative_path)
            if existing is not None:
                _validate_runtime_input_artifacts_unlocked(
                    store_root,
                    {relative_path: producer_id},
                )
                imported.append(existing)
                continue
            tree_entry = _git_output(
                project_root,
                ["ls-tree", source_commit, "--", relative_path],
                artifact_path=relative_path,
            ).decode("utf-8", errors="replace")
            if not tree_entry or tree_entry.split(None, 1)[0] not in {
                "100644",
                "100755",
            }:
                raise RuntimeError(
                    "completed prerequisite artifact is not a regular Git "
                    f"blob: {relative_path}"
                )
            baseline_bytes = _git_output(
                project_root,
                ["show", f"{source_commit}:{relative_path}"],
                artifact_path=relative_path,
            )
            stored = _store_runtime_artifact_bytes(
                baseline_bytes,
                store_root / "objects",
            )
            record = {
                "artifact_path": relative_path,
                "attempt_id": f"BASELINE-{source_commit[:12]}",
                "byte_count": stored["byte_count"],
                "sha256": stored["sha256"],
                "task_id": producer_id,
                "artifact_origin": "completed_prerequisite_baseline",
                "source_commit_sha": source_commit,
            }
            records[relative_path] = record
            imported.append(record)
        _write_json_atomically(manifest_path, manifest)
        return imported


def materialize_runtime_input_artifacts(
    output_dir,
    worktree_path,
    artifact_producers,
):
    if not artifact_producers:
        return []
    store_root = Path(output_dir) / "runtime_artifacts"
    with _runtime_artifact_store_lock(store_root, exclusive=False):
        records = _validate_runtime_input_artifacts_unlocked(
            store_root,
            artifact_producers,
        )
        materialized = []
        for relative_path in sorted(artifact_producers):
            target = _safe_runtime_artifact_path(worktree_path, relative_path)
            record = records[relative_path]
            if target.exists():
                if not target.is_file() or target.is_symlink():
                    raise RuntimeError(
                        f"input artifact target is not a regular file: {relative_path}"
                    )
                if _sha256_file(target) != record["sha256"]:
                    raise RuntimeError(
                        f"input artifact conflicts with repository content: {relative_path}"
                    )
                materialized.append(
                    {
                        "artifact_path": relative_path,
                        "materialization_status": "already_present",
                        "sha256": record["sha256"],
                    }
                )
                continue
            source = store_root / "objects" / record["sha256"]
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_atomically(source, target)
            materialized.append(
                {
                    **record,
                    "materialization_status": "materialized",
                }
            )
        return materialized


def validate_runtime_input_artifacts(output_dir, artifact_producers):
    if not artifact_producers:
        return {}
    store_root = Path(output_dir) / "runtime_artifacts"
    with _runtime_artifact_store_lock(store_root, exclusive=False):
        return _validate_runtime_input_artifacts_unlocked(
            store_root,
            artifact_producers,
        )


def runtime_input_artifact_producers(backlog, task):
    items = backlog.get("items", []) if isinstance(backlog, dict) else []
    tasks_by_id = {
        item.get("task_id"): item
        for item in items
        if isinstance(item, dict) and item.get("task_id")
    }
    dependency_ids = set()
    pending = list(task.get("depends_on", []))
    while pending:
        dependency_id = pending.pop()
        if dependency_id in dependency_ids:
            continue
        dependency = tasks_by_id.get(dependency_id)
        if not dependency:
            raise RuntimeError(
                f"runtime artifact dependency is unavailable: {dependency_id}"
            )
        dependency_ids.add(dependency_id)
        pending.extend(dependency.get("depends_on", []))

    requested_paths = set(task.get("input_artifacts", []))
    producers = {}
    for dependency_id in sorted(dependency_ids):
        dependency = tasks_by_id[dependency_id]
        for artifact_path in dependency.get("expected_output_artifacts", []):
            if artifact_path not in requested_paths:
                continue
            existing = producers.get(artifact_path)
            if existing and existing != dependency_id:
                raise RuntimeError(
                    f"runtime artifact has multiple dependency producers: {artifact_path}"
                )
            producers[artifact_path] = dependency_id
    return producers


def runtime_output_artifact_paths(backlog, task):
    task_id = task.get("task_id")
    if not task_id:
        return []
    items = backlog.get("items", []) if isinstance(backlog, dict) else []
    tasks_by_id = {
        item.get("task_id"): item
        for item in items
        if isinstance(item, dict) and item.get("task_id")
    }
    produced_paths = set(task.get("expected_output_artifacts", []))
    paths = set()
    for consumer in items:
        if not isinstance(consumer, dict) or consumer.get("task_id") == task_id:
            continue
        requested_paths = produced_paths & set(consumer.get("input_artifacts", []))
        if not requested_paths:
            continue
        dependency_ids = set()
        pending = list(consumer.get("depends_on", []))
        while pending:
            dependency_id = pending.pop()
            if dependency_id in dependency_ids:
                continue
            dependency_ids.add(dependency_id)
            dependency = tasks_by_id.get(dependency_id)
            if dependency:
                pending.extend(dependency.get("depends_on", []))
        if task_id in dependency_ids:
            paths.update(requested_paths)
    return sorted(paths)


def _validate_runtime_input_artifacts_unlocked(store_root, artifact_producers):
    manifest = _read_runtime_artifact_manifest(store_root / "manifest.json")
    records = manifest.get("artifacts", {})
    for relative_path in sorted(artifact_producers):
        record = records.get(relative_path)
        if not isinstance(record, dict):
            raise RuntimeError(f"input artifact is unavailable: {relative_path}")
        if record.get("artifact_path") != relative_path:
            raise RuntimeError(
                f"input artifact manifest path mismatch: {relative_path}"
            )
        if record.get("task_id") != artifact_producers[relative_path]:
            raise RuntimeError(
                f"input artifact producer mismatch: {relative_path}"
            )
        digest_record = record.get("sha256")
        if (
            not isinstance(digest_record, str)
            or len(digest_record) != 64
            or any(
                character not in "0123456789abcdef"
                for character in digest_record
            )
        ):
            raise RuntimeError(
                f"input artifact manifest digest is invalid: {relative_path}"
            )
        source = store_root / "objects" / digest_record
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(
                f"input artifact object is unavailable: {relative_path}"
            )
        if _sha256_file(source) != digest_record:
            raise RuntimeError(
                f"input artifact digest mismatch in runtime store: {relative_path}"
            )
    return records


def _read_runtime_artifact_manifest(path):
    path = Path(path)
    if not path.exists():
        return {
            "artifact_manifest_schema_version": "runtime_artifact_manifest.v1",
            "artifacts": {},
        }
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("artifact_manifest_schema_version") != "runtime_artifact_manifest.v1":
        raise RuntimeError("unsupported runtime artifact manifest schema")
    if not isinstance(manifest.get("artifacts"), dict):
        raise RuntimeError("runtime artifact manifest artifacts must be an object")
    return manifest


def _safe_runtime_artifact_path(root, relative_path, require_file=False):
    if not isinstance(relative_path, str) or not relative_path:
        raise RuntimeError("runtime artifact path must be a non-empty string")
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"runtime artifact path escapes its root: {relative_path}")
    root = Path(root).resolve()
    candidate = root / path
    resolved_parent = candidate.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            f"runtime artifact path escapes its root: {relative_path}"
        ) from exc
    if require_file:
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(
                f"runtime artifact file is unavailable: {relative_path}"
            ) from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(
                f"runtime artifact file escapes its root: {relative_path}"
            ) from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(
                f"runtime artifact is not a regular file: {relative_path}"
            )
    return candidate


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_output(project_root, arguments, *, artifact_path=None):
    completed = subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        if artifact_path is not None:
            raise RuntimeError(
                "completed prerequisite artifact is not tracked at the Git "
                f"baseline: {artifact_path}"
            )
        raise RuntimeError(
            "completed prerequisite Git baseline is unavailable: "
            + completed.stderr.decode("utf-8", errors="replace").strip()
        )
    return completed.stdout


@contextmanager
def _runtime_artifact_store_lock(store_root, *, exclusive):
    store_root = Path(store_root)
    store_root.mkdir(parents=True, exist_ok=True)
    with (store_root / ".lock").open("a+b") as lock_file:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_file.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _store_runtime_artifact_object(source, objects_root):
    objects_root = Path(objects_root)
    objects_root.mkdir(parents=True, exist_ok=True)
    temporary = objects_root / f".object.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        with Path(source).open("rb") as source_stream:
            with temporary.open("xb") as destination_stream:
                shutil.copyfileobj(source_stream, destination_stream)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        digest = _sha256_file(temporary)
        byte_count = temporary.stat().st_size
        destination = objects_root / digest
        if destination.exists():
            if (
                not destination.is_file()
                or destination.is_symlink()
                or _sha256_file(destination) != digest
            ):
                raise RuntimeError(
                    f"runtime artifact object is invalid: {digest}"
                )
        else:
            os.replace(temporary, destination)
            _fsync_directory(objects_root)
        return {
            "byte_count": byte_count,
            "sha256": digest,
        }
    finally:
        temporary.unlink(missing_ok=True)


def _store_runtime_artifact_bytes(content, objects_root):
    objects_root = Path(objects_root)
    staging = objects_root.parent / (
        f".baseline-artifact.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with staging.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return _store_runtime_artifact_object(staging, objects_root)
    finally:
        staging.unlink(missing_ok=True)


def _copy_file_atomically(source, destination):
    destination = Path(destination)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with Path(source).open("rb") as source_stream:
            with temporary.open("xb") as destination_stream:
                shutil.copyfileobj(source_stream, destination_stream)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomically(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

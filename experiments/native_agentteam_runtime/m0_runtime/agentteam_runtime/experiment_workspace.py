"""Sanitized Git workspace allocation for Phase 2 experiment runs.

The allocator intentionally does not use ``git worktree`` or clone-local
alternates.  Each run receives a freshly initialized repository and fetches
only the protocol-bound commit at depth one.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import (
    ExperimentContractError,
    canonical_json_bytes,
    publish_immutable_json,
)


CLEAN_SNAPSHOT_SCHEMA_VERSION = "experiment_clean_snapshot.v1"
DEFAULT_INVENTORY_LIMIT = 100_000
MAX_INVENTORY_LIMIT = 1_000_000
SNAPSHOT_DIRECTORY_NAME = "repository"
ATTESTATION_FILE_NAME = "clean-snapshot.json"

_OBJECT_ID_LENGTHS = {"sha1": 40, "sha256": 64}
_HEX = re.compile(r"^[0-9a-f]+$")
_PRIOR_RUN_ROOTS = (b".agentteam",)
_FORBIDDEN_PSEUDOREFS = (
    "AUTO_MERGE",
    "BISECT_HEAD",
    "CHERRY_PICK_HEAD",
    "FETCH_HEAD",
    "MERGE_HEAD",
    "ORIG_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
)


class ExperimentWorkspaceError(ExperimentContractError):
    """Raised when a sanitized workspace cannot be proven safe."""


def allocate_clean_snapshot(
    run_dir,
    repository,
    *,
    attested_at=None,
    inventory_limit=DEFAULT_INVENTORY_LIMIT,
):
    """Allocate and attest one independent, exact-commit Git snapshot.

    ``repository`` is the immutable protocol repository object containing
    ``source``, ``commit``, ``tree``, and ``git_object_format``.  The target
    path and attestation must both be absent; callers must never reuse a
    snapshot across modes or repetitions.
    """

    run_dir = _safe_run_directory(run_dir)
    repository = _normalize_repository(repository)
    inventory_limit = _normalize_inventory_limit(inventory_limit)
    snapshot_path = run_dir / SNAPSHOT_DIRECTORY_NAME
    attestation_path = run_dir / ATTESTATION_FILE_NAME
    if snapshot_path.exists() or snapshot_path.is_symlink():
        raise ExperimentWorkspaceError(
            f"snapshot path already exists and cannot be reused: {snapshot_path}"
        )
    if attestation_path.exists() or attestation_path.is_symlink():
        raise ExperimentWorkspaceError(
            f"clean snapshot attestation already exists: {attestation_path}"
        )

    source = _inspect_source(repository, inventory_limit=inventory_limit)
    stage_path = Path(
        tempfile.mkdtemp(prefix=".repository-staging-", dir=str(run_dir))
    )
    published_snapshot = False
    try:
        _git(
            None,
            "init",
            "--quiet",
            f"--object-format={repository['git_object_format']}",
            str(stage_path),
        )
        _git(
            stage_path,
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--no-tags",
            "--depth=1",
            "--no-write-fetch-head",
            source["source_path"],
            repository["commit"],
        )
        _git(
            stage_path,
            "checkout",
            "--quiet",
            "--detach",
            repository["commit"],
        )
        _git(stage_path, "reset", "--quiet", "--hard", repository["commit"])
        _git(stage_path, "clean", "--quiet", "-ffdx")
        for pseudoref in _FORBIDDEN_PSEUDOREFS:
            _git(stage_path, "update-ref", "-d", pseudoref)

        verification = _verify_clean_snapshot(
            stage_path,
            repository,
            inventory_limit=inventory_limit,
            source=source,
        )
        _rename_directory_no_replace(stage_path, snapshot_path)
        published_snapshot = True
        verification = _verify_clean_snapshot(
            snapshot_path,
            repository,
            inventory_limit=inventory_limit,
            source=source,
        )

        attestation = {
            "schema_version": CLEAN_SNAPSHOT_SCHEMA_VERSION,
            "experiment_run_id": run_dir.name,
            "attested_at": attested_at or _utc_timestamp(),
            "repository": repository,
            "snapshot_path": str(snapshot_path),
            "snapshot_common_dir": verification["snapshot_common_dir"],
            "source_common_dir": verification["source_common_dir"],
            "path_preexisted": False,
            "head_commit": verification["head_commit"],
            "head_tree": verification["head_tree"],
            "git_object_format": verification["git_object_format"],
            "detached_head": verification["detached_head"],
            "worktree_clean": verification["worktree_clean"],
            "common_dirs_distinct": verification["common_dirs_distinct"],
            "remotes": verification["remotes"],
            "alternates": verification["alternates"],
            "extra_refs": verification["extra_refs"],
            "file_inventory": verification["file_inventory"],
            "symlink_escape_count": verification["symlink_escape_count"],
            "tracked_files_only": verification["tracked_files_only"],
            "prior_run_state_detected": verification[
                "prior_run_state_detected"
            ],
        }
        validate_clean_snapshot_attestation(attestation)
        publish_immutable_json(
            attestation_path,
            attestation,
            label="clean snapshot attestation",
        )
        return {
            "created": True,
            "snapshot_path": str(snapshot_path),
            "attestation_path": str(attestation_path),
            "attestation": attestation,
        }
    except Exception:
        cleanup_target = snapshot_path if published_snapshot else stage_path
        if cleanup_target.exists() and not cleanup_target.is_symlink():
            shutil.rmtree(cleanup_target, ignore_errors=True)
        raise


def verify_clean_snapshot(
    snapshot_path,
    repository,
    *,
    inventory_limit=DEFAULT_INVENTORY_LIMIT,
):
    """Verify all clean-reset properties and return attestation evidence."""

    repository = _normalize_repository(repository)
    inventory_limit = _normalize_inventory_limit(inventory_limit)
    return _verify_clean_snapshot(
        snapshot_path,
        repository,
        inventory_limit=inventory_limit,
        source=_inspect_source(
            repository,
            inventory_limit=inventory_limit,
        ),
    )


def _verify_clean_snapshot(
    snapshot_path,
    repository,
    *,
    inventory_limit,
    source,
):
    snapshot_path = Path(snapshot_path)
    if snapshot_path.is_symlink() or not snapshot_path.is_dir():
        raise ExperimentWorkspaceError(
            f"snapshot is missing or is not a real directory: {snapshot_path}"
        )
    snapshot_path = snapshot_path.resolve()
    in_workspace_git_dir = snapshot_path / ".git"
    if in_workspace_git_dir.is_symlink() or not in_workspace_git_dir.is_dir():
        raise ExperimentWorkspaceError(
            "snapshot must be a standalone repository with an in-workspace "
            ".git directory"
        )
    in_workspace_git_dir = in_workspace_git_dir.resolve()

    head_commit = _git(snapshot_path, "rev-parse", "--verify", "HEAD").stdout.strip()
    head_tree = _git(snapshot_path, "rev-parse", "HEAD^{tree}").stdout.strip()
    object_format = _git(
        snapshot_path,
        "rev-parse",
        "--show-object-format",
    ).stdout.strip()
    git_dir = _resolve_git_path(
        snapshot_path,
        _git(
            snapshot_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
        ).stdout.strip(),
        "snapshot Git directory",
    )
    common_dir = _resolve_git_path(
        snapshot_path,
        _git(
            snapshot_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ).stdout.strip(),
        "snapshot common directory",
    )
    if git_dir != in_workspace_git_dir or common_dir != in_workspace_git_dir:
        raise ExperimentWorkspaceError(
            "snapshot must not use a linked worktree or external Git common "
            "directory"
        )
    objects_dir = common_dir / "objects"
    if common_dir.is_symlink() or objects_dir.is_symlink() or not objects_dir.is_dir():
        raise ExperimentWorkspaceError("snapshot object store is missing or unsafe")

    if head_commit != repository["commit"]:
        raise ExperimentWorkspaceError("snapshot HEAD does not match protocol commit")
    if head_tree != repository["tree"]:
        raise ExperimentWorkspaceError("snapshot tree does not match protocol tree")
    if object_format != repository["git_object_format"]:
        raise ExperimentWorkspaceError(
            "snapshot Git object format does not match protocol"
        )
    if common_dir == source["source_common_dir"]:
        raise ExperimentWorkspaceError(
            "snapshot shares its Git common directory with the source"
        )

    inventory_bytes = _git_bytes(
        snapshot_path,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "HEAD",
    )
    inventory_count = _bounded_inventory_count(
        inventory_bytes,
        inventory_limit=inventory_limit,
        label="snapshot",
    )
    if inventory_bytes != source["inventory_bytes"]:
        raise ExperimentWorkspaceError(
            "snapshot file inventory does not match the protocol source tree"
        )
    prior_run_paths = _prior_run_state_paths(inventory_bytes)
    if prior_run_paths:
        raise ExperimentWorkspaceError(
            "snapshot source tree contains prior AgentTeam run state: "
            + ", ".join(prior_run_paths)
        )

    status = _git(
        snapshot_path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored",
    ).stdout
    symbolic_head = _git(
        snapshot_path,
        "symbolic-ref",
        "--quiet",
        "HEAD",
        check=False,
    )
    remotes = _nonempty_lines(_git(snapshot_path, "remote").stdout)
    remote_config = _git(
        snapshot_path,
        "config",
        "--local",
        "--get-regexp",
        r"^remote\.",
        check=False,
    )
    extra_refs = _nonempty_lines(
        _git(snapshot_path, "for-each-ref", "--format=%(refname)").stdout
    )
    extra_refs.extend(_present_pseudorefs(git_dir))
    alternates = _alternate_paths(common_dir)
    symlink_escapes = _symlink_escapes(snapshot_path)

    if status:
        raise ExperimentWorkspaceError("snapshot worktree is not clean")
    if symbolic_head.returncode == 0:
        raise ExperimentWorkspaceError("snapshot HEAD is attached to a branch")
    if symbolic_head.returncode not in (1,):
        raise ExperimentWorkspaceError("snapshot detached-HEAD state is inconclusive")
    if remotes or remote_config.returncode == 0:
        raise ExperimentWorkspaceError("snapshot contains remote configuration")
    if remote_config.returncode not in (0, 1):
        raise ExperimentWorkspaceError(
            "snapshot remote-configuration check was inconclusive"
        )
    if alternates:
        raise ExperimentWorkspaceError("snapshot contains Git object alternates")
    if extra_refs:
        raise ExperimentWorkspaceError("snapshot contains extra Git refs")
    if symlink_escapes:
        raise ExperimentWorkspaceError(
            "snapshot contains symlinks that escape the worktree: "
            + ", ".join(symlink_escapes)
        )

    return {
        "head_commit": head_commit,
        "head_tree": head_tree,
        "git_object_format": object_format,
        "snapshot_common_dir": str(common_dir),
        "source_common_dir": str(source["source_common_dir"]),
        "detached_head": True,
        "worktree_clean": True,
        "common_dirs_distinct": True,
        "remotes": [],
        "alternates": [],
        "extra_refs": [],
        "file_inventory": {
            "entry_count": inventory_count,
            "sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "inventory_limit": inventory_limit,
            "matches_source": True,
        },
        "symlink_escape_count": 0,
        "tracked_files_only": True,
        "prior_run_state_detected": False,
    }


def load_clean_snapshot_attestation(path):
    """Load and validate a canonically published clean-reset attestation."""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ExperimentWorkspaceError(
            f"clean snapshot attestation is missing or unsafe: {path}"
        )
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentWorkspaceError(
            "clean snapshot attestation is not valid UTF-8 JSON"
        ) from exc
    if payload != canonical_json_bytes(value) + b"\n":
        raise ExperimentWorkspaceError(
            "clean snapshot attestation is not canonically published"
        )
    validate_clean_snapshot_attestation(value)
    return value


def validate_clean_snapshot_attestation(attestation):
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "experiment_clean_snapshot.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentWorkspaceError(
            "clean snapshot attestation schema is unavailable"
        ) from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(attestation),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise ExperimentWorkspaceError(
            "clean snapshot attestation schema validation failed at "
            f"{location}: {first.message}"
        )
    _normalize_repository(attestation["repository"])
    snapshot_path = Path(attestation["snapshot_path"])
    snapshot_common_dir = Path(attestation["snapshot_common_dir"])
    source_common_dir = Path(attestation["source_common_dir"])
    if not all(
        path.is_absolute()
        for path in (snapshot_path, snapshot_common_dir, source_common_dir)
    ):
        raise ExperimentWorkspaceError(
            "clean snapshot attestation paths must be absolute"
        )
    if (
        snapshot_path.name != SNAPSHOT_DIRECTORY_NAME
        or snapshot_path.parent.name != attestation["experiment_run_id"]
    ):
        raise ExperimentWorkspaceError(
            "clean snapshot attestation does not bind the run layout"
        )
    if snapshot_common_dir != snapshot_path / ".git":
        raise ExperimentWorkspaceError(
            "clean snapshot attestation does not bind an in-workspace .git "
            "directory"
        )
    if snapshot_common_dir.resolve(strict=False) == source_common_dir.resolve(
        strict=False
    ):
        raise ExperimentWorkspaceError(
            "clean snapshot attestation common directories are not distinct"
        )
    try:
        snapshot_common_dir.resolve(strict=False).relative_to(
            snapshot_path.resolve(strict=False)
        )
    except ValueError as exc:
        raise ExperimentWorkspaceError(
            "clean snapshot common directory is outside the snapshot"
        ) from exc
    inventory = attestation["file_inventory"]
    if inventory["entry_count"] > inventory["inventory_limit"]:
        raise ExperimentWorkspaceError(
            "clean snapshot inventory exceeds its attested bound"
        )
    if attestation["head_commit"] != attestation["repository"]["commit"]:
        raise ExperimentWorkspaceError(
            "clean snapshot attestation does not bind the repository commit"
        )
    if attestation["head_tree"] != attestation["repository"]["tree"]:
        raise ExperimentWorkspaceError(
            "clean snapshot attestation does not bind the repository tree"
        )
    if (
        attestation["git_object_format"]
        != attestation["repository"]["git_object_format"]
    ):
        raise ExperimentWorkspaceError(
            "clean snapshot attestation does not bind the Git object format"
        )
    return attestation


def cleanup_clean_snapshot(run_dir, *, sealed_result_path):
    """Remove only the disposable snapshot while preserving sealed evidence.

    The result must already exist outside the snapshot but inside the run
    directory.  It is fsynced and hashed before cleanup, then hashed again
    afterwards.  Cleanup failures are returned as evidence rather than masking
    or deleting the terminal result.
    """

    run_dir = _safe_run_directory(run_dir)
    snapshot_path = run_dir / SNAPSHOT_DIRECTORY_NAME
    attestation = load_clean_snapshot_attestation(
        run_dir / ATTESTATION_FILE_NAME
    )
    attested_snapshot = Path(attestation["snapshot_path"])
    if not attested_snapshot.is_absolute():
        attested_snapshot = run_dir / attested_snapshot
    if attested_snapshot.resolve(strict=False) != snapshot_path:
        raise ExperimentWorkspaceError(
            "clean snapshot attestation does not bind the cleanup target"
        )
    result_path = _safe_result_path(run_dir, snapshot_path, sealed_result_path)
    _fsync_result_tree(result_path)
    before_sha256 = _result_digest(result_path)
    record = {
        "cleanup_status": "removed",
        "snapshot_path": str(snapshot_path),
        "sealed_result_path": str(result_path),
        "sealed_result_sha256": before_sha256,
        "result_preserved": True,
    }

    try:
        if snapshot_path.is_symlink():
            raise ExperimentWorkspaceError("snapshot cleanup target is a symlink")
        if snapshot_path.exists():
            if not snapshot_path.is_dir():
                raise ExperimentWorkspaceError(
                    "snapshot cleanup target is not a directory"
                )
            shutil.rmtree(snapshot_path)
        else:
            record["cleanup_status"] = "already_absent"
    except Exception as exc:
        record["cleanup_status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"

    after_sha256 = _result_digest(result_path)
    if after_sha256 != before_sha256:
        raise ExperimentWorkspaceError(
            "sealed result changed while cleaning the disposable snapshot"
        )
    record["sealed_result_sha256_after_cleanup"] = after_sha256
    return record


def _inspect_source(repository, *, inventory_limit):
    source_raw = Path(repository["source"]).expanduser()
    if source_raw.is_symlink() or not source_raw.exists():
        raise ExperimentWorkspaceError(
            f"protocol repository source is missing or unsafe: {source_raw}"
        )
    source_path = source_raw.resolve()
    object_format = _git(
        source_path,
        "rev-parse",
        "--show-object-format",
    ).stdout.strip()
    if object_format != repository["git_object_format"]:
        raise ExperimentWorkspaceError(
            "source Git object format does not match protocol"
        )
    source_commit = _git(
        source_path,
        "rev-parse",
        "--verify",
        f"{repository['commit']}^{{commit}}",
    ).stdout.strip()
    source_tree = _git(
        source_path,
        "rev-parse",
        f"{repository['commit']}^{{tree}}",
    ).stdout.strip()
    if source_commit != repository["commit"]:
        raise ExperimentWorkspaceError("source commit does not match protocol")
    if source_tree != repository["tree"]:
        raise ExperimentWorkspaceError("source tree does not match protocol")
    common_dir = _resolve_git_path(
        source_path,
        _git(
            source_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ).stdout.strip(),
        "source common directory",
    )
    inventory_bytes = _git_bytes(
        source_path,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        repository["commit"],
    )
    _bounded_inventory_count(
        inventory_bytes,
        inventory_limit=inventory_limit,
        label="source",
    )
    return {
        "source_path": str(source_path),
        "source_common_dir": common_dir,
        "inventory_bytes": inventory_bytes,
    }


def _normalize_repository(repository):
    required = ("source", "commit", "tree", "git_object_format")
    if not isinstance(repository, dict) or any(
        field not in repository for field in required
    ):
        raise ExperimentWorkspaceError(
            "repository must contain source, commit, tree, and git_object_format"
        )
    if set(repository) != set(required):
        raise ExperimentWorkspaceError("repository contains undeclared fields")
    normalized = {field: repository[field] for field in required}
    if not isinstance(normalized["source"], str) or not normalized["source"]:
        raise ExperimentWorkspaceError("repository.source must be non-empty")
    object_format = normalized["git_object_format"]
    length = _OBJECT_ID_LENGTHS.get(object_format)
    if length is None:
        raise ExperimentWorkspaceError("unsupported Git object format")
    for field in ("commit", "tree"):
        value = normalized[field]
        if (
            not isinstance(value, str)
            or len(value) != length
            or _HEX.fullmatch(value) is None
        ):
            raise ExperimentWorkspaceError(
                f"repository.{field} does not match {object_format}"
            )
    return normalized


def _normalize_inventory_limit(value):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > MAX_INVENTORY_LIMIT
    ):
        raise ExperimentWorkspaceError(
            f"inventory_limit must be between 1 and {MAX_INVENTORY_LIMIT}"
        )
    return value


def _safe_run_directory(run_dir):
    path = Path(run_dir).expanduser()
    if path.is_symlink() or not path.is_dir():
        raise ExperimentWorkspaceError(
            f"experiment run directory is missing or unsafe: {path}"
        )
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*",
        resolved.name,
    ) is None:
        raise ExperimentWorkspaceError("experiment run directory is not bounded")
    return resolved


def _safe_result_path(run_dir, snapshot_path, sealed_result_path):
    raw = Path(sealed_result_path).expanduser()
    if raw.is_symlink() or not raw.exists():
        raise ExperimentWorkspaceError(
            f"sealed result is missing or unsafe: {raw}"
        )
    result_path = raw.resolve()
    if result_path == run_dir:
        raise ExperimentWorkspaceError(
            "sealed result must not be the experiment run directory itself"
        )
    try:
        result_path.relative_to(run_dir)
    except ValueError as exc:
        raise ExperimentWorkspaceError(
            "sealed result must be inside the experiment run directory"
        ) from exc
    try:
        result_path.relative_to(snapshot_path)
    except ValueError:
        pass
    else:
        raise ExperimentWorkspaceError(
            "sealed result must be outside the disposable snapshot"
        )
    return result_path


def _git(cwd, *arguments, check=True):
    command = ["git"]
    if cwd is not None:
        command.extend(["-C", str(cwd)])
    command.extend(arguments)
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_sanitized_git_environment(),
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ExperimentWorkspaceError(
            f"Git command failed ({' '.join(command)}): {detail}"
        )
    return completed


def _git_bytes(cwd, *arguments):
    command = ["git", "-C", str(cwd), *arguments]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_sanitized_git_environment(),
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ExperimentWorkspaceError(
            f"Git command failed ({' '.join(command)}): {detail}"
        )
    return completed.stdout


def _sanitized_git_environment():
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _resolve_git_path(repository_path, value, label):
    path = Path(value)
    if not path.is_absolute():
        path = repository_path / path
    if path.is_symlink() or not path.is_dir():
        raise ExperimentWorkspaceError(f"{label} is missing or unsafe")
    return path.resolve()


def _bounded_inventory_count(payload, *, inventory_limit, label):
    entries = payload.count(b"\0")
    if payload and not payload.endswith(b"\0"):
        raise ExperimentWorkspaceError(f"{label} Git inventory is malformed")
    if entries > inventory_limit:
        raise ExperimentWorkspaceError(
            f"{label} Git inventory exceeds limit {inventory_limit}"
        )
    return entries


def _nonempty_lines(value):
    return [line for line in value.splitlines() if line]


def _alternate_paths(common_dir):
    paths = []
    for name in ("alternates", "http-alternates"):
        path = common_dir / "objects" / "info" / name
        if path.is_symlink():
            paths.append(f"unsafe:{path}")
        elif path.exists():
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                paths.append(f"unsafe:{path}")
            else:
                paths.extend(_nonempty_lines(content))
    return paths


def _present_pseudorefs(git_dir):
    return [
        name
        for name in _FORBIDDEN_PSEUDOREFS
        if (git_dir / name).exists() or (git_dir / name).is_symlink()
    ]


def _prior_run_state_paths(inventory):
    paths = []
    for entry in inventory.split(b"\0"):
        if not entry:
            continue
        _, separator, path = entry.partition(b"\t")
        if not separator:
            raise ExperimentWorkspaceError("snapshot Git inventory is malformed")
        if any(
            path == root or path.startswith(root + b"/")
            for root in _PRIOR_RUN_ROOTS
        ):
            paths.append(os.fsdecode(path))
    return sorted(paths)


def _symlink_escapes(root):
    escapes = []
    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        if directory_path == root:
            directory_names[:] = [
                name for name in directory_names if name != ".git"
            ]
        for name in [*directory_names, *file_names]:
            candidate = directory_path / name
            if not candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                escapes.append(candidate.relative_to(root).as_posix())
    return sorted(escapes)


def _rename_directory_no_replace(source, target):
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise ExperimentWorkspaceError(
            "Linux renameat2 is required for atomic snapshot publication"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in (errno.EEXIST, errno.ENOTEMPTY):
        raise ExperimentWorkspaceError(
            f"snapshot path appeared during allocation: {target}"
        )
    raise OSError(error, os.strerror(error), str(target))


def _result_digest(path):
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        raise ExperimentWorkspaceError("sealed result is not a file or directory")
    digest.update(b"directory\0")
    for candidate in sorted(path.rglob("*")):
        if candidate.is_symlink():
            raise ExperimentWorkspaceError("sealed result contains a symlink")
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        if candidate.is_dir():
            digest.update(b"d\0" + relative + b"\0")
        elif candidate.is_file():
            digest.update(b"f\0" + relative + b"\0")
            digest.update(hashlib.sha256(candidate.read_bytes()).digest())
        else:
            raise ExperimentWorkspaceError(
                "sealed result contains a non-regular filesystem entry"
            )
    return digest.hexdigest()


def _fsync_result_tree(path):
    files = [path] if path.is_file() else [
        candidate for candidate in path.rglob("*") if candidate.is_file()
    ]
    for candidate in files:
        if candidate.is_symlink():
            raise ExperimentWorkspaceError("sealed result contains a symlink")
        with candidate.open("rb") as handle:
            os.fsync(handle.fileno())
    directories = []
    if path.is_dir():
        directories.extend(
            sorted(
                (candidate for candidate in path.rglob("*") if candidate.is_dir()),
                reverse=True,
            )
        )
        directories.append(path)
    directories.append(path.parent)
    for directory in directories:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _utc_timestamp():
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

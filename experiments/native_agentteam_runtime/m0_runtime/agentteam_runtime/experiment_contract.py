"""Immutable identity and allocation primitives for Phase 2 experiments.

This module deliberately stops before repository snapshotting or provider
launch.  A successful allocation means that all authoritative inputs needed by
those later operations have already been published with create-if-absent
semantics.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


PROTOCOL_SCHEMA_VERSION = "experiment_protocol.v1"
RUN_MANIFEST_SCHEMA_VERSION = "experiment_run_manifest.v2"
RUN_BINDING_SCHEMA_VERSION = "experiment_run_binding.v1"
STATE_SCHEMA_VERSION = "experiment_state.v1"
LEGACY_MANIFEST_SCHEMA_VERSION = "agentteam_experiment_manifest.v1"

EXPERIMENT_MODES = (
    "single_codex",
    "agentteam_direct",
    "agentteam_full",
)
TERMINAL_STATES = {
    "completed",
    "failed",
    "interrupted",
    "budget_stopped",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_FIELDS = (
    "release_id",
    "release_root",
    "runtime_root",
    "release_manifest_sha256",
    "source_commit",
    "git_object_format",
)
_REPOSITORY_FIELDS = (
    "source",
    "commit",
    "tree",
    "git_object_format",
)


class ExperimentContractError(RuntimeError):
    """Raised when immutable experiment authority is absent or inconsistent."""


class ExperimentLeaseError(ExperimentContractError):
    """Raised when another controller already owns a run's writer lease."""


class ControllerLease:
    """An owned non-blocking ``flock`` kept alive by its file descriptor."""

    def __init__(self, path, fd, record):
        self.path = Path(path)
        self.record = dict(record)
        self._fd = fd

    @property
    def held(self):
        return self._fd is not None

    def release(self):
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        if not self.held:
            raise ExperimentLeaseError("controller lease has already been released")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False

    def __del__(self):
        try:
            self.release()
        except OSError:
            pass


def schema_path(filename):
    return Path(__file__).resolve().parents[2] / "schemas" / filename


def canonical_json_bytes(value):
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExperimentContractError("value is not canonical JSON") from exc
    return text.encode("utf-8")


def canonical_json_sha256(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def validate_experiment_protocol(protocol):
    _validate_schema(
        protocol,
        "experiment_protocol.schema.json",
        "experiment protocol",
    )
    return protocol


def load_experiment_protocol(protocol_path):
    protocol = _read_json_object(protocol_path, "experiment protocol")
    validate_experiment_protocol(protocol)
    return protocol


def validate_experiment_run_manifest(manifest, protocol=None):
    version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if version == LEGACY_MANIFEST_SCHEMA_VERSION:
        raise ExperimentContractError(
            "agentteam_experiment_manifest.v1 is validation-only and is not "
            "executable by the Phase 2 harness"
        )
    _validate_schema(
        manifest,
        "experiment_run_manifest.schema.json",
        "experiment run manifest",
    )
    if protocol is not None:
        protocol = _snapshot_json_object(
            _coerce_json_object(protocol, "experiment protocol"),
            "experiment protocol",
        )
        validate_experiment_protocol(protocol)
        protocol_sha256 = canonical_json_sha256(protocol)
        if manifest["protocol_sha256"] != protocol_sha256:
            raise ExperimentContractError(
                "run manifest does not bind the canonical protocol digest"
            )
        if manifest["mode"] not in protocol["modes"]:
            raise ExperimentContractError("run manifest mode is absent from protocol")
        if manifest["repetition_index"] >= protocol["repetition_policy"]["count"]:
            raise ExperimentContractError(
                "run manifest repetition index is outside protocol policy"
            )
    expected_run_id = derive_experiment_run_id(
        manifest["protocol_sha256"],
        manifest["mode"],
        manifest["repetition_index"],
        manifest["stable_request_key"],
    )
    if manifest["experiment_run_id"] != expected_run_id:
        raise ExperimentContractError(
            "run manifest experiment_run_id does not match its stable identity"
        )
    return manifest


def validate_experiment_run_binding(binding):
    _validate_schema(
        binding,
        "experiment_run_binding.schema.json",
        "experiment run binding",
    )
    return binding


def validate_experiment_state(state):
    _validate_schema(
        state,
        "experiment_state.schema.json",
        "experiment state",
    )
    return state


def ensure_executable_manifest(manifest):
    """Accept only the Phase 2 run manifest, never the legacy P0-A manifest."""
    return validate_experiment_run_manifest(manifest)


def derive_experiment_run_id(
    protocol_sha256,
    mode,
    repetition_index,
    stable_request_key,
):
    if not isinstance(protocol_sha256, str) or not _SHA256.fullmatch(
        protocol_sha256
    ):
        raise ExperimentContractError("protocol_sha256 must be a lowercase SHA-256")
    if mode not in EXPERIMENT_MODES:
        raise ExperimentContractError(f"unsupported experiment mode: {mode!r}")
    if (
        not isinstance(repetition_index, int)
        or isinstance(repetition_index, bool)
        or repetition_index < 0
    ):
        raise ExperimentContractError("repetition_index must be a non-negative integer")
    _require_safe_id(stable_request_key, "stable_request_key")
    digest = canonical_json_sha256(
        {
            "mode": mode,
            "protocol_sha256": protocol_sha256,
            "repetition_index": repetition_index,
            "stable_request_key": stable_request_key,
        }
    )
    return f"experiment-run-{digest}"


def build_experiment_run_manifest(
    protocol,
    *,
    mode,
    repetition_index,
    stable_request_key,
):
    protocol = _snapshot_json_object(
        _coerce_json_object(protocol, "experiment protocol"),
        "experiment protocol",
    )
    validate_experiment_protocol(protocol)
    protocol_sha256 = canonical_json_sha256(protocol)
    manifest = {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "experiment_run_id": derive_experiment_run_id(
            protocol_sha256,
            mode,
            repetition_index,
            stable_request_key,
        ),
        "protocol_sha256": protocol_sha256,
        "mode": mode,
        "repetition_index": repetition_index,
        "stable_request_key": stable_request_key,
    }
    validate_experiment_run_manifest(manifest, protocol)
    return manifest


def publish_immutable_json(path, value, *, label="immutable JSON artifact"):
    """Create one canonical JSON file or accept an identical existing file."""
    path = Path(path)
    parent = path.parent
    _require_safe_directory(parent, f"{label} parent", create=True)
    canonical_payload = canonical_json_bytes(value)
    payload = canonical_payload + b"\n"
    payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(path, flags, 0o600)
        created = True
    except FileExistsError:
        existing = _read_file_bytes(path, label)
        if existing != payload:
            raise ExperimentContractError(
                f"{label} already exists with different or non-canonical content: {path}"
            )
        return {
            "path": str(path),
            "sha256": payload_sha256,
            "created": False,
        }
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ExperimentContractError(f"{label} path is a symlink: {path}") from exc
        raise
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        if created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    else:
        os.close(fd)
    _fsync_directory(parent)
    return {
        "path": str(path),
        "sha256": payload_sha256,
        "created": True,
    }


def publish_experiment_protocol(experiment_root, protocol):
    protocol = _snapshot_json_object(
        _coerce_json_object(protocol, "experiment protocol"),
        "experiment protocol",
    )
    validate_experiment_protocol(protocol)
    digest = canonical_json_sha256(protocol)
    root = _normalize_experiment_root(experiment_root)
    publication = publish_immutable_json(
        root / "protocols" / f"{digest}.json",
        protocol,
        label="experiment protocol",
    )
    return {**publication, "protocol_sha256": digest}


def publish_experiment_run_manifest(experiment_root, manifest, protocol=None):
    if protocol is None:
        raise ExperimentContractError(
            "experiment protocol is required to publish a run manifest"
        )
    manifest = _snapshot_json_object(manifest, "experiment run manifest")
    validate_experiment_run_manifest(manifest, protocol)
    digest = canonical_json_sha256(manifest)
    root = _normalize_experiment_root(experiment_root)
    publication = publish_immutable_json(
        root / "run-manifests" / f"{digest}.json",
        manifest,
        label="experiment run manifest",
    )
    return {**publication, "run_manifest_sha256": digest}


def allocate_experiment_run(
    experiment_root,
    protocol,
    *,
    mode,
    repetition_index,
    stable_request_key,
    runtime_release,
    repository=None,
    bound_at=None,
):
    """Allocate or return one stable request without launching a provider."""
    root = _normalize_experiment_root(experiment_root)
    protocol = _snapshot_json_object(
        _coerce_json_object(protocol, "experiment protocol"),
        "experiment protocol",
    )
    validate_experiment_protocol(protocol)
    stable_request_key = _require_safe_id(
        stable_request_key,
        "stable_request_key",
    )
    release_identity = _normalize_runtime_release(runtime_release)
    protocol_repository = _normalize_repository(protocol["repository"])
    repository_identity = _normalize_repository(repository or protocol_repository)
    if repository_identity != protocol_repository:
        raise ExperimentContractError(
            "repository identity does not match the immutable protocol"
        )
    manifest = build_experiment_run_manifest(
        protocol,
        mode=mode,
        repetition_index=repetition_index,
        stable_request_key=stable_request_key,
    )
    protocol_sha256 = manifest["protocol_sha256"]
    run_manifest_sha256 = canonical_json_sha256(manifest)
    experiment_run_id = manifest["experiment_run_id"]
    request_path = root / "requests" / f"{stable_request_key}.json"
    run_dir = root / "runs" / experiment_run_id

    with _allocation_lock(root):
        if request_path.exists() or request_path.is_symlink():
            request_binding = _read_immutable_json(
                request_path,
                "stable request binding",
            )
            validate_experiment_run_binding(request_binding)
            _assert_binding_expectations(
                request_binding,
                protocol=protocol,
                run_manifest=manifest,
                runtime_release=release_identity,
                repository=repository_identity,
                stable_request_key=stable_request_key,
                experiment_run_id=experiment_run_id,
            )
            bound = _load_bound_run(
                run_dir,
                expected_protocol=protocol,
                expected_run_manifest=manifest,
                expected_runtime_release=release_identity,
                expected_repository=repository_identity,
                expected_stable_request_key=stable_request_key,
                require_request=True,
                allow_terminal=True,
            )
            if request_binding != bound["binding"]:
                raise ExperimentContractError(
                    "stable request binding differs from run binding"
                )
            return _allocation_result(
                root,
                bound,
                request_path,
                allocation_status="existing",
            )

        protocol_publication = publish_experiment_protocol(root, protocol)
        manifest_publication = publish_experiment_run_manifest(
            root,
            manifest,
            protocol,
        )
        binding = {
            "schema_version": RUN_BINDING_SCHEMA_VERSION,
            "experiment_run_id": experiment_run_id,
            "protocol_sha256": protocol_sha256,
            "run_manifest_sha256": run_manifest_sha256,
            "mode": mode,
            "repetition_index": repetition_index,
            "stable_request_key": stable_request_key,
            "runtime_release": release_identity,
            "repository": repository_identity,
            "bound_at": bound_at or _utc_now(),
        }
        validate_experiment_run_binding(binding)
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "experiment_run_id": experiment_run_id,
            "protocol_sha256": protocol_sha256,
            "run_manifest_sha256": run_manifest_sha256,
            "status": "prepared",
            "state_version": 1,
            "recoverable": False,
            "updated_at": binding["bound_at"],
        }
        validate_experiment_state(state)

        if run_dir.exists() or run_dir.is_symlink():
            bound = _load_bound_run(
                run_dir,
                expected_protocol=protocol,
                expected_run_manifest=manifest,
                expected_runtime_release=release_identity,
                expected_repository=repository_identity,
                expected_stable_request_key=stable_request_key,
                require_request=False,
                allow_terminal=True,
            )
            binding = bound["binding"]
        else:
            _publish_run_directory(root, run_dir, binding, state)
            bound = {
                "run_dir": str(run_dir),
                "binding": binding,
                "protocol": protocol,
                "run_manifest": manifest,
                "state": state,
            }
        publish_immutable_json(
            request_path,
            binding,
            label="stable request binding",
        )
        bound = _load_bound_run(
            run_dir,
            expected_protocol=protocol,
            expected_run_manifest=manifest,
            expected_runtime_release=release_identity,
            expected_repository=repository_identity,
            expected_stable_request_key=stable_request_key,
            require_request=True,
            allow_terminal=True,
        )
        result = _allocation_result(
            root,
            bound,
            request_path,
            allocation_status="created",
        )
        result["publication"] = {
            "protocol": protocol_publication,
            "run_manifest": manifest_publication,
        }
        return result


def validate_resume_binding(
    run_dir,
    *,
    protocol=None,
    run_manifest=None,
    runtime_release=None,
    repository=None,
    stable_request_key=None,
    experiment_run_id=None,
    protocol_sha256=None,
    run_manifest_sha256=None,
):
    """Revalidate every supplied identity before an interrupted run resumes."""
    bound = _load_bound_run(
        run_dir,
        expected_protocol=_coerce_json_object(protocol, "expected protocol"),
        expected_run_manifest=_coerce_json_object(
            run_manifest,
            "expected run manifest",
        ),
        expected_runtime_release=(
            _normalize_runtime_release(runtime_release)
            if runtime_release is not None
            else None
        ),
        expected_repository=(
            _normalize_repository(repository) if repository is not None else None
        ),
        expected_stable_request_key=stable_request_key,
        expected_experiment_run_id=experiment_run_id,
        expected_protocol_sha256=protocol_sha256,
        expected_run_manifest_sha256=run_manifest_sha256,
        require_request=True,
        allow_terminal=False,
    )
    return {
        "resume_status": "accepted",
        "provider_calls": 0,
        "target_mutations": 0,
        **bound,
    }


def acquire_controller_lease(
    run_dir,
    *,
    controller_id,
    lease_id=None,
    acquired_at=None,
):
    """Acquire the run-local single-writer lease or fail without waiting."""
    run_dir = _normalize_run_dir(run_dir)
    binding = _read_immutable_json(
        run_dir / "binding.json",
        "experiment run binding",
    )
    validate_experiment_run_binding(binding)
    if binding["experiment_run_id"] != run_dir.name:
        raise ExperimentContractError("run binding does not match run directory")
    controller_id = _require_safe_id(controller_id, "controller_id")
    lease_id = _require_safe_id(
        lease_id or f"lease-{os.getpid()}-{os.urandom(8).hex()}",
        "lease_id",
    )
    lease_path = run_dir / "controller-lease.json"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lease_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ExperimentLeaseError("controller lease path is a symlink") from exc
        raise
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExperimentLeaseError(
                f"experiment run already has a controller writer: {run_dir.name}"
            ) from exc
        record = {
            "schema_version": "experiment_controller_lease.v1",
            "experiment_run_id": run_dir.name,
            "controller_id": controller_id,
            "lease_id": lease_id,
            "pid": os.getpid(),
            "acquired_at": acquired_at or _utc_now(),
        }
        payload = canonical_json_bytes(record) + b"\n"
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        _write_all(fd, payload)
        os.fsync(fd)
        _fsync_directory(run_dir)
    except Exception:
        os.close(fd)
        raise
    return ControllerLease(lease_path, fd, record)


def _allocation_result(root, bound, request_path, *, allocation_status):
    binding = bound["binding"]
    return {
        "allocation_status": allocation_status,
        "created": allocation_status == "created",
        "experiment_run_id": binding["experiment_run_id"],
        "protocol_sha256": binding["protocol_sha256"],
        "run_manifest_sha256": binding["run_manifest_sha256"],
        "protocol_path": str(
            root / "protocols" / f"{binding['protocol_sha256']}.json"
        ),
        "run_manifest_path": str(
            root
            / "run-manifests"
            / f"{binding['run_manifest_sha256']}.json"
        ),
        "request_path": str(request_path),
        "run_dir": bound["run_dir"],
        "binding": binding,
        "run_manifest": bound["run_manifest"],
        "state": bound["state"],
        "provider_calls": 0,
        "target_mutations": 0,
    }


def _publish_run_directory(root, run_dir, binding, state):
    staging_root = root / ".staging"
    _require_safe_directory(staging_root, "experiment staging root", create=True)
    stage = staging_root / (
        f"{binding['experiment_run_id']}.{os.getpid()}.{os.urandom(8).hex()}"
    )
    stage.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        publish_immutable_json(
            stage / "binding.json",
            binding,
            label="experiment run binding",
        )
        publish_immutable_json(
            stage / "state.json",
            state,
            label="initial experiment state",
        )
        _fsync_directory(stage)
        _require_safe_directory(run_dir.parent, "experiment runs root", create=True)
        if run_dir.exists() or run_dir.is_symlink():
            raise ExperimentContractError(
                f"experiment run allocation collision: {run_dir.name}"
            )
        _rename_no_replace(stage, run_dir)
        _fsync_directory(run_dir.parent)
        _fsync_directory(staging_root)
    except Exception:
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage)
        raise


def _load_bound_run(
    run_dir,
    *,
    expected_protocol=None,
    expected_run_manifest=None,
    expected_runtime_release=None,
    expected_repository=None,
    expected_stable_request_key=None,
    expected_experiment_run_id=None,
    expected_protocol_sha256=None,
    expected_run_manifest_sha256=None,
    require_request,
    allow_terminal,
):
    run_dir = _normalize_run_dir(run_dir)
    root = run_dir.parent.parent
    binding = _read_immutable_json(
        run_dir / "binding.json",
        "experiment run binding",
    )
    validate_experiment_run_binding(binding)
    if binding["experiment_run_id"] != run_dir.name:
        raise ExperimentContractError("run binding does not match run directory")
    protocol_path = root / "protocols" / f"{binding['protocol_sha256']}.json"
    manifest_path = (
        root / "run-manifests" / f"{binding['run_manifest_sha256']}.json"
    )
    protocol = _read_immutable_json(protocol_path, "published experiment protocol")
    manifest = _read_immutable_json(
        manifest_path,
        "published experiment run manifest",
    )
    validate_experiment_protocol(protocol)
    validate_experiment_run_manifest(manifest, protocol)
    if canonical_json_sha256(protocol) != binding["protocol_sha256"]:
        raise ExperimentContractError("published protocol digest does not match binding")
    if canonical_json_sha256(manifest) != binding["run_manifest_sha256"]:
        raise ExperimentContractError(
            "published run manifest digest does not match binding"
        )
    if binding["repository"] != _normalize_repository(protocol["repository"]):
        raise ExperimentContractError(
            "run binding repository identity differs from published protocol"
        )
    _assert_binding_expectations(
        binding,
        protocol=protocol,
        run_manifest=manifest,
        runtime_release=expected_runtime_release,
        repository=expected_repository,
        stable_request_key=expected_stable_request_key,
        experiment_run_id=expected_experiment_run_id,
        protocol_sha256=expected_protocol_sha256,
        run_manifest_sha256=expected_run_manifest_sha256,
    )
    if expected_protocol is not None:
        validate_experiment_protocol(expected_protocol)
        if canonical_json_sha256(expected_protocol) != binding["protocol_sha256"]:
            raise ExperimentContractError("resume rejected protocol drift")
    if expected_run_manifest is not None:
        validate_experiment_run_manifest(expected_run_manifest, protocol)
        if (
            canonical_json_sha256(expected_run_manifest)
            != binding["run_manifest_sha256"]
        ):
            raise ExperimentContractError("resume rejected run manifest drift")

    request_path = root / "requests" / f"{binding['stable_request_key']}.json"
    if require_request:
        request_binding = _read_immutable_json(
            request_path,
            "stable request binding",
        )
        validate_experiment_run_binding(request_binding)
        if request_binding != binding:
            raise ExperimentContractError(
                "stable request binding differs from run binding"
            )

    state = _read_json_object(run_dir / "state.json", "experiment state")
    validate_experiment_state(state)
    for field in (
        "experiment_run_id",
        "protocol_sha256",
        "run_manifest_sha256",
    ):
        if state[field] != binding[field]:
            raise ExperimentContractError(
                f"experiment state {field} does not match immutable binding"
            )
    if not allow_terminal and state["status"] in TERMINAL_STATES:
        raise ExperimentContractError(
            f"terminal experiment state cannot resume: {state['status']}"
        )
    return {
        "run_dir": str(run_dir),
        "binding": binding,
        "protocol": protocol,
        "run_manifest": manifest,
        "state": state,
    }


def _assert_binding_expectations(
    binding,
    *,
    protocol=None,
    run_manifest=None,
    runtime_release=None,
    repository=None,
    stable_request_key=None,
    experiment_run_id=None,
    protocol_sha256=None,
    run_manifest_sha256=None,
):
    if protocol is not None:
        protocol_digest = canonical_json_sha256(protocol)
        if binding["protocol_sha256"] != protocol_digest:
            raise ExperimentContractError("stable request is bound to another protocol")
    if run_manifest is not None:
        manifest_digest = canonical_json_sha256(run_manifest)
        if binding["run_manifest_sha256"] != manifest_digest:
            raise ExperimentContractError(
                "stable request is bound to another run manifest"
            )
        for field in (
            "experiment_run_id",
            "protocol_sha256",
            "mode",
            "repetition_index",
            "stable_request_key",
        ):
            if binding[field] != run_manifest[field]:
                raise ExperimentContractError(
                    f"run binding {field} differs from run manifest"
                )
    if runtime_release is not None and binding["runtime_release"] != runtime_release:
        raise ExperimentContractError("resume rejected runtime release drift")
    if repository is not None and binding["repository"] != repository:
        if (
            binding["repository"].get("git_object_format")
            != repository.get("git_object_format")
        ):
            raise ExperimentContractError(
                "resume rejected repository object-format drift"
            )
        raise ExperimentContractError("resume rejected repository identity drift")
    scalar_expectations = {
        "stable_request_key": stable_request_key,
        "experiment_run_id": experiment_run_id,
        "protocol_sha256": protocol_sha256,
        "run_manifest_sha256": run_manifest_sha256,
    }
    for field, expected in scalar_expectations.items():
        if expected is not None and binding[field] != expected:
            raise ExperimentContractError(f"resume rejected {field} drift")


@contextmanager
def _allocation_lock(root):
    locks_root = root / ".locks"
    _require_safe_directory(locks_root, "experiment lock root", create=True)
    lock_path = locks_root / "request-allocation.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ExperimentContractError("experiment allocation lock is unsafe") from exc
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _normalize_experiment_root(experiment_root):
    raw = Path(experiment_root).expanduser()
    if raw.is_symlink():
        raise ExperimentContractError("experiment root must not be a symlink")
    raw.mkdir(parents=True, exist_ok=True)
    if not raw.is_dir():
        raise ExperimentContractError("experiment root must be a directory")
    return raw.resolve()


def _normalize_run_dir(run_dir):
    raw = Path(run_dir).expanduser()
    if raw.is_symlink() or not raw.is_dir():
        raise ExperimentContractError(f"experiment run is missing or unsafe: {raw}")
    resolved = raw.resolve()
    if resolved.parent.name != "runs" or resolved.parent.parent == resolved.parent:
        raise ExperimentContractError("experiment run path is outside the run layout")
    _require_safe_id(resolved.name, "experiment_run_id")
    return resolved


def _normalize_runtime_release(value):
    if not isinstance(value, dict):
        raise ExperimentContractError("runtime_release must be an object")
    missing = [field for field in _RELEASE_FIELDS if value.get(field) is None]
    if missing:
        raise ExperimentContractError(
            "runtime_release is missing fields: " + ", ".join(missing)
        )
    normalized = {field: value[field] for field in _RELEASE_FIELDS}
    _require_safe_id(normalized["release_id"], "runtime_release.release_id")
    for field in ("release_root", "runtime_root"):
        if not isinstance(normalized[field], str) or not normalized[field]:
            raise ExperimentContractError(f"runtime_release.{field} must be non-empty")
    if not _SHA256.fullmatch(str(normalized["release_manifest_sha256"])):
        raise ExperimentContractError(
            "runtime_release.release_manifest_sha256 must be a lowercase SHA-256"
        )
    _validate_object_id(
        normalized["source_commit"],
        normalized["git_object_format"],
        "runtime_release.source_commit",
    )
    return normalized


def _normalize_repository(value):
    if not isinstance(value, dict):
        raise ExperimentContractError("repository must be an object")
    missing = [field for field in _REPOSITORY_FIELDS if value.get(field) is None]
    if missing:
        raise ExperimentContractError(
            "repository is missing fields: " + ", ".join(missing)
        )
    normalized = {field: value[field] for field in _REPOSITORY_FIELDS}
    if not isinstance(normalized["source"], str) or not normalized["source"]:
        raise ExperimentContractError("repository.source must be non-empty")
    _validate_object_id(
        normalized["commit"],
        normalized["git_object_format"],
        "repository.commit",
    )
    _validate_object_id(
        normalized["tree"],
        normalized["git_object_format"],
        "repository.tree",
    )
    return normalized


def _validate_object_id(value, object_format, field):
    lengths = {"sha1": 40, "sha256": 64}
    length = lengths.get(object_format)
    if length is None:
        raise ExperimentContractError(f"{field} has unsupported Git object format")
    if (
        not isinstance(value, str)
        or len(value) != length
        or re.fullmatch(r"[0-9a-f]+", value) is None
    ):
        raise ExperimentContractError(
            f"{field} does not match Git object format {object_format}"
        )


def _coerce_json_object(value, label):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, os.PathLike)):
        return _read_json_object(Path(value), label)
    raise ExperimentContractError(f"{label} must be an object or JSON path")


def _snapshot_json_object(value, label):
    if not isinstance(value, dict):
        raise ExperimentContractError(f"{label} must be a JSON object")
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ExperimentContractError(f"{label} cannot be snapshotted") from exc


def _validate_schema(value, filename, label):
    schema = _read_json_object(schema_path(filename), f"{label} schema")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise ExperimentContractError(
            f"{label} schema validation failed at {location}: {first.message}"
        )


def _read_immutable_json(path, label):
    payload = _read_file_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentContractError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ExperimentContractError(f"{label} must be a JSON object")
    if payload != canonical_json_bytes(value) + b"\n":
        raise ExperimentContractError(f"{label} is not canonically published")
    return value


def _read_json_object(path, label):
    payload = _read_file_bytes(path, label)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentContractError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ExperimentContractError(f"{label} must be a JSON object")
    return value


def _read_file_bytes(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ExperimentContractError(f"{label} is missing or unsafe: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ExperimentContractError(f"{label} is not readable: {path}") from exc


def _require_safe_directory(path, label, *, create):
    path = Path(path)
    if path.is_symlink():
        raise ExperimentContractError(f"{label} is a symlink: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ExperimentContractError(f"{label} is not a directory: {path}")
    return path


def _require_safe_id(value, field):
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ExperimentContractError(f"{field} is not a safe identifier")
    return value


def _write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing experiment authority")
        view = view[written:]


def _fsync_directory(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_no_replace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ExperimentContractError(
            "Linux renameat2 is required for atomic experiment run allocation"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ExperimentContractError(
            f"experiment run allocation collision: {Path(target).name}"
        )
    raise OSError(error, os.strerror(error), str(target))


def _utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# Short compatibility aliases for controller code introduced in later tasks.
build_run_manifest = build_experiment_run_manifest
publish_protocol = publish_experiment_protocol
publish_run_manifest = publish_experiment_run_manifest
allocate_or_get_experiment_run = allocate_experiment_run
validate_run_binding = validate_experiment_run_binding

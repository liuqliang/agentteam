"""Fail-closed provider isolation and trusted experiment evaluation.

Provider processes receive an explicit bubblewrap mount namespace and an
explicit environment.  Evaluators run only after every model invocation has a
terminal record, use an argv vector (never a shell), and retain bounded
evidence including canary leakage scans.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import (
    ExperimentContractError,
    publish_immutable_json,
)
from .model_context_budget import (
    CONTEXT_BUDGET_POLICY_FIELDS,
    LEGACY_CONTEXT_POLICY_FIELDS,
    normalize_context_budget_policy,
    select_tool_budget_route,
)


PROVIDER_SANDBOX_SCHEMA_VERSION = "experiment_provider_sandbox.v1"
NAMESPACE_PROBE_SCHEMA_VERSION = "experiment_namespace_probe.v1"
EVALUATION_SCHEMA_VERSION = "experiment_evaluation.v1"
SANDBOX_REFERENCE_SCHEMA_VERSION = "experiment_provider_sandbox_reference.v1"
SCAN_SCOPE_REFERENCE_SCHEMA_VERSION = "experiment_scan_scope_reference.v1"
EVALUATOR_REFERENCE_SCHEMA_VERSION = "experiment_evaluator_reference.v1"
INVOCATION_SET_REFERENCE_SCHEMA_VERSION = (
    "experiment_model_invocation_set_reference.v1"
)
LIFECYCLE_REGISTRY_SEAL_SCHEMA_VERSION = (
    "experiment_lifecycle_registry_seal.v1"
)
PROTOCOL_REFERENCE_SCHEMA_VERSION = "experiment_protocol_reference.v1"
LAUNCH_REGISTRATION_SCHEMA_VERSION = (
    "experiment_launch_registration.v1"
)
MODE_AUTHORITY_SCHEMA_VERSION = "experiment_mode_authority.v1"
DEFAULT_MAX_CREDENTIAL_BYTES = 1024 * 1024
DEFAULT_MAX_CREDENTIAL_FILES = 32
DEFAULT_MAX_SCAN_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_SCAN_FILES = 10_000
DEPENDENCY_TREE_MAX_BYTES = 512 * 1024 * 1024
DEPENDENCY_TREE_MAX_ENTRIES = 20_000
DEPENDENCY_TREE_IDENTITY_POLICY = "bounded_dependency_tree.v1"
PUBLIC_DEPENDENCY_TREE_MAX_BYTES = 2 * 1024 * 1024 * 1024
PUBLIC_DEPENDENCY_TREE_MAX_ENTRIES = 100_000
PUBLIC_DEPENDENCY_TREE_IDENTITY_POLICY = "bounded_public_dependency_tree.v1"
DEFAULT_MAX_EVALUATOR_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_RUNTIME_FILE_BYTES = 512 * 1024 * 1024
MAX_EVALUATION_TIMEOUT_SECONDS = 3600
PRELAUNCH_SOURCE_REVALIDATION_TIMEOUT_SECONDS = 120
_CREDENTIAL_ROOT = Path("/run/agentteam-credentials")
_TRUSTED_BWRAP_PATH = Path("/usr/bin/bwrap")
_TRUSTED_EVALUATOR_ENVIRONMENT_NAMES = frozenset(
    {"PYTHONDONTWRITEBYTECODE", "SSL_CERT_DIR", "SSL_CERT_FILE"}
)
_NETWORK_POLICIES = {"disabled", "provider_access"}
_TRUSTED_ENV_PATH = Path("/usr/bin/env")
_TRUSTED_GIT_PATH = Path("/usr/bin/git")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOWERCASE_PROXY_ENVIRONMENT_NAMES = {
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}
_PROXY_ENVIRONMENT_NAMES = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    *_LOWERCASE_PROXY_ENVIRONMENT_NAMES,
}
_DEFAULT_ENVIRONMENT = {
    "HOME": "/tmp/agentteam-home",
    "LANG": "C.UTF-8",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "TMPDIR": "/tmp",
}
_EVALUATOR_LOADER_SCRIPT = (
    "import hashlib,os,subprocess,sys\n"
    "expected=sys.argv[1]\n"
    "command=sys.argv[2:]\n"
    "data=sys.stdin.buffer.read(4194305)\n"
    "if len(data)>4194304 or hashlib.sha256(data).hexdigest()!=expected:\n"
    " raise SystemExit(70)\n"
    "path='/tmp/agentteam-trusted-evaluator'\n"
    "fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o500)\n"
    "with os.fdopen(fd,'wb') as handle:\n"
    " handle.write(data); handle.flush(); os.fsync(handle.fileno())\n"
    "checked=subprocess.run([path,'--',*command],stdin=subprocess.DEVNULL,"
    "check=False)\n"
    "if checked.returncode!=0:\n"
    " raise SystemExit(checked.returncode)\n"
    "os.execv(command[0],command)\n"
)


class ExperimentSandboxError(RuntimeError):
    """The declared provider or evaluator boundary is unsafe."""


class ExperimentSandboxUnavailable(ExperimentSandboxError):
    """The namespace boundary cannot be conclusively enforced."""


class ExperimentEvaluationBlocked(ExperimentSandboxError):
    """Evaluation was blocked before a trusted acceptance result existed."""

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True)
class PreparedProviderLaunch:
    """Exact process arguments produced by one validated sandbox policy."""

    command: tuple[str, ...]
    cwd: str
    environment: dict[str, str]
    policy_sha256: str
    _source_descriptor_json: bytes
    _repository_workspace_sha256: str

    def source_authority(self):
        """Return bounded authority for supervisor-side pre-exec validation."""

        return {
            "descriptor_json": self._source_descriptor_json.decode("utf-8"),
            "policy_sha256": self.policy_sha256,
            "repository_workspace_sha256": (
                self._repository_workspace_sha256
            ),
        }

    def revalidate_mutable_sources(self):
        """Recheck every host source immediately before the launch permit."""

        _revalidate_prepared_source_authority(self.source_authority())
        return None


def _revalidate_prepared_source_authority(authority):
    """Fail closed unless every prepared source still matches at exec time."""

    if (
        not isinstance(authority, dict)
        or set(authority)
        != {
            "descriptor_json",
            "policy_sha256",
            "repository_workspace_sha256",
        }
        or not _is_sha256(authority.get("policy_sha256"))
        or not _is_sha256(
            authority.get("repository_workspace_sha256")
        )
    ):
        raise ExperimentSandboxError(
            "prepared provider source authority is invalid"
        )
    try:
        descriptor = json.loads(
            authority["descriptor_json"]
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxError(
            "prepared provider source authority is unreadable"
        ) from exc
    validate_provider_sandbox_descriptor(descriptor)
    if descriptor["policy_sha256"] != authority["policy_sha256"]:
        raise ExperimentSandboxError(
            "prepared provider policy differs from source authority"
        )
    observed_workspace = _repository_workspace_sha256(
        Path(descriptor["repository"]["source"]),
        require_git=descriptor.get("repository_identity") is not None,
    )
    if observed_workspace != authority["repository_workspace_sha256"]:
        raise ExperimentSandboxError(
            "prepared repository workspace content changed"
        )
    return None


def build_provider_sandbox_descriptor(
    repository_root,
    *,
    runtime_views,
    library_views=(),
    credential_mounts=(),
    environment=None,
    network_policy="disabled",
    bwrap_path=None,
    repository_target=None,
    repository_identity=None,
    forbidden_paths=(),
):
    """Build an immutable, least-view bubblewrap launch descriptor.

    Runtime and library views are read-only.  The repository is the only
    writable host view.  Credentials are read-only, inventory bounded, and
    may only appear below ``/run/agentteam-credentials`` in the namespace.
    Evaluator-only paths are accepted only as negative validation inputs and
    are deliberately omitted from the returned descriptor.  Network access is
    either disabled or explicitly enabled only for the provider launch; the
    canary probe and trusted evaluator always retain an isolated network
    namespace.
    """

    repository_source = _existing_path(
        repository_root,
        "repository",
        require_directory=True,
    )
    repository_target = _absolute_target(
        repository_target or repository_source,
        "repository",
    )
    forbidden = [
        _existing_path(path, "evaluator-only path")
        for path in forbidden_paths
    ]
    bwrap = bwrap_path or _TRUSTED_BWRAP_PATH
    if not Path(bwrap).is_file():
        raise ExperimentSandboxUnavailable("bubblewrap executable is unavailable")
    bwrap = _existing_path(bwrap, "bubblewrap", require_file=True)
    if bwrap != _TRUSTED_BWRAP_PATH:
        raise ExperimentSandboxUnavailable(
            "bubblewrap executable is not the controller-approved system binary"
        )
    if not os.access(bwrap, os.X_OK):
        raise ExperimentSandboxUnavailable("bubblewrap executable is not executable")

    runtime = _normalize_views(runtime_views, "runtime")
    libraries = _normalize_views(library_views, "library")
    credentials = _normalize_credentials(credential_mounts)
    declared_sources = [
        repository_source,
        *(Path(view["source"]) for view in runtime),
        *(Path(view["source"]) for view in libraries),
        *(Path(view["source"]) for view in credentials),
    ]
    for evaluator_path in forbidden:
        for source in declared_sources:
            if _paths_overlap(source, evaluator_path):
                raise ExperimentSandboxError(
                    "evaluator-only path overlaps a provider mount"
                )

    bounded_environment = _normalize_environment(environment)
    forbidden_markers = _forbidden_markers(forbidden)
    for value in bounded_environment.values():
        encoded = value.encode("utf-8")
        if any(marker and marker in encoded for marker in forbidden_markers):
            raise ExperimentSandboxError(
                "evaluator-only material appears in provider environment"
            )

    if network_policy not in _NETWORK_POLICIES:
        raise ExperimentSandboxError(
            "provider network policy is invalid"
        )
    descriptor = {
        "schema_version": PROVIDER_SANDBOX_SCHEMA_VERSION,
        "bwrap_path": str(bwrap),
        "bwrap_sha256": hashlib.sha256(
            _read_bounded_regular_file(bwrap, max_bytes=16 * 1024 * 1024)
        ).hexdigest(),
        "repository": {
            "source": str(repository_source),
            "target": str(repository_target),
            "writable": True,
            "source_identity": _mount_source_identity(
                repository_source,
                hash_directory=False,
            ),
        },
        "repository_identity": _validated_repository_identity(
            repository_source,
            repository_identity,
        ),
        "runtime_views": runtime,
        "library_views": libraries,
        "credential_views": credentials,
        "environment": bounded_environment,
        "network_policy": network_policy,
        "namespace_evidence": None,
    }
    descriptor["policy_sha256"] = _sandbox_policy_sha256(descriptor)
    _validate_provider_sandbox_descriptor(
        descriptor,
        require_namespace_evidence=False,
    )
    return descriptor


def probe_gold_canary_denial(
    descriptor,
    canary_path,
    *,
    runner=None,
    probe_python=None,
    timeout_seconds=30,
):
    """Prove that the exact provider namespace cannot stat or read a canary."""

    _validate_provider_sandbox_descriptor(
        descriptor,
        require_namespace_evidence=False,
    )
    canary_path = _existing_path(
        canary_path,
        "gold canary",
        require_file=True,
        reject_symlink=True,
    )
    canary = _read_bounded_regular_file(canary_path, max_bytes=64 * 1024)
    if not canary:
        raise ExperimentSandboxError("gold canary must not be empty")
    if len(canary) > 64 * 1024:
        raise ExperimentSandboxError("gold canary exceeds the bounded probe size")
    _validate_descriptor_excludes_canary(descriptor, canary_path)
    probe_python = str(probe_python or _probe_python_for_descriptor(descriptor))
    script = (
        "import json,os,sys\n"
        "p=sys.argv[1]\n"
        "visible=os.path.lexists(p)\n"
        "readable=False\n"
        "try:\n"
        " open(p,'rb').read(1); readable=True\n"
        "except (OSError,PermissionError): pass\n"
        "print(json.dumps({'path_visible':visible,'content_readable':readable},"
        "sort_keys=True,separators=(',',':')))\n"
        "raise SystemExit(1 if visible or readable else 0)\n"
    )
    prepared = _prepare_probe_provider_launch(
        descriptor,
        [probe_python, "-c", script, str(canary_path)],
        cwd=descriptor["repository"]["source"],
    )
    run = runner or subprocess.run
    try:
        completed = run(
            list(prepared.command),
            cwd=prepared.cwd,
            env=dict(prepared.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=min(max(float(timeout_seconds), 1.0), 60.0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentSandboxUnavailable(
            f"bubblewrap canary probe was unavailable: {type(exc).__name__}"
        ) from exc
    try:
        observed = json.loads(str(completed.stdout).strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxUnavailable(
            "bubblewrap canary probe returned inconclusive output"
        ) from exc
    denied = (
        completed.returncode == 0
        and observed == {
            "content_readable": False,
            "path_visible": False,
        }
    )
    if not denied:
        raise ExperimentSandboxUnavailable(
            "bubblewrap namespace did not conclusively deny the gold canary"
        )
    return {
        "schema_version": NAMESPACE_PROBE_SCHEMA_VERSION,
        "evidence_status": "complete",
        "denial_status": "denied",
        "policy_sha256": descriptor["policy_sha256"],
        "canary_sha256": hashlib.sha256(canary).hexdigest(),
        "path_visible": False,
        "content_readable": False,
        "probe_returncode": 0,
    }


def _attach_namespace_evidence(descriptor, evidence):
    """Bind successful canary evidence to a sandbox descriptor."""

    candidate = json.loads(json.dumps(descriptor))
    _validate_namespace_evidence(candidate, evidence)
    candidate["namespace_evidence"] = dict(evidence)
    _validate_provider_sandbox_descriptor(
        candidate,
        require_namespace_evidence=False,
    )
    return candidate


def publish_provider_sandbox_reference(
    authority_root,
    descriptor,
    canary_path,
    *,
    reference_id="provider-sandbox",
):
    _validate_provider_sandbox_descriptor(
        descriptor,
        require_namespace_evidence=False,
    )
    if descriptor.get("namespace_evidence") is not None:
        raise ExperimentSandboxError(
            "sandbox publication requires a fresh controller probe"
        )
    evidence = probe_gold_canary_denial(descriptor, canary_path)
    descriptor = _attach_namespace_evidence(descriptor, evidence)
    validate_provider_sandbox_descriptor(descriptor)
    authority_dir = _experiment_authority_dir(authority_root)
    path = authority_dir / f"{_safe_reference_id(reference_id)}.sandbox.json"
    _publish_immutable_json(path, descriptor)
    payload = _read_bounded_regular_file(path, max_bytes=4 * 1024 * 1024)
    return {
        "schema_version": SANDBOX_REFERENCE_SCHEMA_VERSION,
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def load_provider_sandbox_reference(reference, authority_root):
    return _load_provider_sandbox_reference(
        reference,
        authority_root,
    )


def _load_historical_provider_sandbox_reference(
    reference,
    authority_root,
    *,
    sealed_result,
    cleanup_receipt,
    clean_snapshot_attestation,
):
    historical_context = _validate_historical_cleanup_context(
        sealed_result,
        cleanup_receipt,
        clean_snapshot_attestation,
    )
    return _load_provider_sandbox_reference(
        reference,
        authority_root,
        historical_context=historical_context,
    )


def _load_provider_sandbox_reference(
    reference,
    authority_root,
    *,
    historical_context=None,
):
    _path, payload = _load_authority_reference(
        reference,
        authority_root,
        schema_version=SANDBOX_REFERENCE_SCHEMA_VERSION,
        suffix=".sandbox.json",
    )
    try:
        descriptor = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxUnavailable(
            "provider sandbox authority is unreadable"
        ) from exc
    if historical_context is None:
        _validate_provider_sandbox_descriptor(
            descriptor,
            require_namespace_evidence=True,
        )
    else:
        _validate_provider_sandbox_descriptor(
            descriptor,
            require_namespace_evidence=True,
            historical_context=historical_context,
        )
    return descriptor


def publish_evaluator_reference(
    authority_root,
    evaluator_artifact,
    *,
    reference_id="trusted-evaluator",
    source_bytes=None,
):
    source = _existing_path(
        evaluator_artifact,
        "trusted evaluator source",
        require_file=True,
        reject_symlink=True,
    )
    content = (
        _read_bounded_regular_file(
            source,
            max_bytes=4 * 1024 * 1024,
        )
        if source_bytes is None
        else source_bytes
    )
    if not isinstance(content, bytes) or len(content) > 4 * 1024 * 1024:
        raise ExperimentSandboxError(
            "trusted evaluator source bytes are invalid"
        )
    authority_dir = _experiment_authority_dir(authority_root)
    path = authority_dir / f"{_safe_reference_id(reference_id)}.evaluator"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o500)
    except FileExistsError:
        existing = _read_bounded_regular_file(
            _existing_path(
                path,
                "trusted evaluator reference",
                require_file=True,
                reject_symlink=True,
            ),
            max_bytes=4 * 1024 * 1024,
        )
        if existing != content or not os.access(path, os.X_OK):
            raise ExperimentSandboxError(
                f"trusted evaluator reference conflicts: {path}"
            )
        return {
            "schema_version": EVALUATOR_REFERENCE_SCHEMA_VERSION,
            "path": str(path),
            "sha256": hashlib.sha256(existing).hexdigest(),
        }
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "schema_version": EVALUATOR_REFERENCE_SCHEMA_VERSION,
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def load_evaluator_reference(reference, authority_root):
    path, content = _load_authority_reference(
        reference,
        authority_root,
        schema_version=EVALUATOR_REFERENCE_SCHEMA_VERSION,
        suffix=".evaluator",
    )
    if not os.access(path, os.X_OK):
        raise ExperimentSandboxError(
            "trusted evaluator authority is not executable"
        )
    return path, hashlib.sha256(content).hexdigest()


def publish_experiment_protocol_reference(
    authority_root,
    protocol,
    *,
    reference_id="experiment-protocol",
):
    from .experiment_contract import validate_experiment_protocol

    validate_experiment_protocol(protocol)
    authority_dir = _experiment_authority_dir(authority_root)
    path = authority_dir / f"{_safe_reference_id(reference_id)}.protocol.json"
    _publish_immutable_json(path, protocol)
    payload = _read_bounded_regular_file(path, max_bytes=4 * 1024 * 1024)
    return {
        "schema_version": PROTOCOL_REFERENCE_SCHEMA_VERSION,
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def load_experiment_protocol_reference(reference, authority_root):
    _path, payload = _load_authority_reference(
        reference,
        authority_root,
        schema_version=PROTOCOL_REFERENCE_SCHEMA_VERSION,
        suffix=".protocol.json",
    )
    try:
        protocol = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxUnavailable(
            "experiment protocol authority is unreadable"
        ) from exc
    from .experiment_contract import validate_experiment_protocol

    validate_experiment_protocol(protocol)
    return protocol


def publish_model_invocation_set_reference(
    authority_root,
    run_id,
    invocation_sets,
    *,
    reference_id="model-invocation-set",
):
    authority_root = Path(authority_root).resolve(strict=True)
    authority_dir = _experiment_authority_dir(authority_root)
    path = authority_dir / (
        f"{_safe_reference_id(reference_id)}.invocation-set.json"
    )
    with _experiment_lifecycle_registry_lock(authority_root) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            normalized_sets = _normalize_invocation_sets(
                invocation_sets,
                authority_root=authority_root,
            )
            run_id = _nonempty_text(run_id, "model invocation run id")
            for item in normalized_sets:
                _terminal_invocation_evidence(
                    Path(item["lifecycle_authority_root"])
                    / "model_invocations",
                    expected_invocation_ids=item["invocation_ids"],
                    expected_run_id=run_id,
                    expected_taskpack_id=item["taskpack_id"],
                    expected_sandbox_policy_sha256=item[
                        "sandbox_policy_sha256"
                    ],
                    expected_sandbox_reference_sha256=item[
                        "sandbox_reference"
                    ]["sha256"],
                )
            record = {
                "schema_version": (
                    "experiment_model_invocation_manifest.v1"
                ),
                "run_id": run_id,
                "invocation_sets": normalized_sets,
            }
            existing_manifests = sorted(
                authority_dir.glob("*.invocation-set.json")
            )
            if existing_manifests and existing_manifests != [path]:
                raise ExperimentSandboxError(
                    "model invocation manifest is already sealed"
                )
            if path.exists():
                existing = _read_json_object(
                    path,
                    "model invocation manifest",
                )
                if existing != record:
                    raise ExperimentSandboxError(
                        "model invocation manifest conflicts with replay"
                    )
            else:
                _publish_immutable_json(path, record)
            payload = _read_bounded_regular_file(
                path,
                max_bytes=4 * 1024 * 1024,
            )
            reference = {
                "schema_version": INVOCATION_SET_REFERENCE_SCHEMA_VERSION,
                "path": str(path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            seal = {
                "schema_version": (
                    LIFECYCLE_REGISTRY_SEAL_SCHEMA_VERSION
                ),
                "run_id": run_id,
                "invocation_set_reference": reference,
                "lifecycle_authority_roots": sorted(
                    item["lifecycle_authority_root"]
                    for item in normalized_sets
                ),
            }
            seal_path = _experiment_lifecycle_registry_seal_path(
                authority_root
            )
            if seal_path.exists():
                existing_seal = _read_json_object(
                    seal_path,
                    "experiment lifecycle registry seal",
                )
                if existing_seal != seal:
                    raise ExperimentSandboxError(
                        "experiment lifecycle registry seal conflicts"
                    )
            else:
                _publish_immutable_json(seal_path, seal)
            _validate_experiment_lifecycle_registry_seal(
                authority_root,
                reference,
                normalized_sets,
                expected_run_id=run_id,
            )
            return reference
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def publish_registered_model_invocation_set_reference(
    authority_root,
    run_id,
    *,
    reference_id="model-invocation-set",
):
    """Publish the complete terminal invocation set from controller authority."""

    authority_root = Path(authority_root).resolve(strict=True)
    mode_authority = load_experiment_mode_authority(authority_root)
    if mode_authority["experiment_run_id"] != run_id:
        raise ExperimentSandboxError(
            "model invocation run differs from mode authority"
        )
    registry = _experiment_lifecycle_registry_dir(authority_root)
    registered_roots = _registered_lifecycle_roots(authority_root)
    physical_roots = set()
    with os.scandir(registry) as entries:
        for entry in entries:
            path = Path(entry.path)
            if (
                entry.is_symlink()
                or not entry.is_dir(follow_symlinks=False)
            ):
                raise ExperimentSandboxError(
                    "experiment lifecycle registry contains an unsafe entry"
                )
            physical_roots.add(str(path))
    if physical_roots != registered_roots:
        raise ExperimentSandboxError(
            "experiment lifecycle registry differs from its ledger"
        )
    if not registered_roots:
        raise ExperimentSandboxError(
            "experiment lifecycle registry is empty"
        )

    invocation_sets = []
    seen_invocation_ids = set()
    for lifecycle_root_text in sorted(registered_roots):
        lifecycle_root = Path(lifecycle_root_text)
        registration = load_experiment_launch_registration(
            lifecycle_root
        )
        if registration is None:
            raise ExperimentSandboxError(
                "registered lifecycle has no launch policy"
            )
        if (
            registration["experiment_run_id"] != run_id
            or registration["mode"] != mode_authority["mode"]
            or registration["protocol_sha256"]
            != mode_authority["protocol_sha256"]
            or registration["run_manifest_sha256"]
            != mode_authority["run_manifest_sha256"]
        ):
            raise ExperimentSandboxError(
                "registered lifecycle differs from mode authority"
            )
        invocation_root = lifecycle_root / "model_invocations"
        if (
            not invocation_root.is_dir()
            or invocation_root.is_symlink()
        ):
            raise ExperimentSandboxError(
                "registered lifecycle was not consumed"
            )
        invocation_ids = []
        with os.scandir(invocation_root) as entries:
            for entry in entries:
                invocation_dir = Path(entry.path)
                if (
                    entry.is_symlink()
                    or not entry.is_dir(follow_symlinks=False)
                    or not entry.name.startswith("INV-")
                ):
                    raise ExperimentSandboxError(
                        "model invocation registry contains an unsafe entry"
                    )
                started = invocation_dir / "started.json"
                terminal = invocation_dir / "terminal.json"
                if (
                    not started.is_file()
                    or started.is_symlink()
                    or not terminal.is_file()
                    or terminal.is_symlink()
                ):
                    raise ExperimentSandboxError(
                        "registered model invocation is non-terminal"
                    )
                if entry.name in seen_invocation_ids:
                    raise ExperimentSandboxError(
                        "model invocation id is duplicated across lifecycles"
                    )
                seen_invocation_ids.add(entry.name)
                invocation_ids.append(entry.name)
        if not invocation_ids:
            raise ExperimentSandboxError(
                "registered lifecycle contains no model invocation"
            )
        invocation_sets.append(
            {
                "lifecycle_authority_root": str(lifecycle_root),
                "taskpack_id": registration["taskpack_id"],
                "invocation_ids": sorted(invocation_ids),
                "sandbox_reference": registration[
                    "sandbox_reference"
                ],
                "sandbox_policy_sha256": registration[
                    "sandbox_policy_sha256"
                ],
            }
        )
    return publish_model_invocation_set_reference(
        authority_root,
        run_id,
        invocation_sets,
        reference_id=reference_id,
    )


def load_model_invocation_set_reference(reference, authority_root):
    return _load_model_invocation_set_reference(
        reference,
        authority_root,
    )


def _load_historical_model_invocation_set_reference(
    reference,
    authority_root,
    *,
    sealed_result,
    cleanup_receipt,
    clean_snapshot_attestation,
):
    historical_context = _validate_historical_cleanup_context(
        sealed_result,
        cleanup_receipt,
        clean_snapshot_attestation,
    )
    return _load_model_invocation_set_reference(
        reference,
        authority_root,
        historical_context=historical_context,
    )


def _load_model_invocation_set_reference(
    reference,
    authority_root,
    *,
    historical_context=None,
):
    _path, payload = _load_authority_reference(
        reference,
        authority_root,
        schema_version=INVOCATION_SET_REFERENCE_SCHEMA_VERSION,
        suffix=".invocation-set.json",
    )
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxUnavailable(
            "model invocation set authority is unreadable"
        ) from exc
    if (
        not isinstance(record, dict)
        or set(record) != {"schema_version", "run_id", "invocation_sets"}
        or record["schema_version"]
        != "experiment_model_invocation_manifest.v1"
    ):
        raise ExperimentSandboxError(
            "invalid model invocation set authority"
        )
    normalized = _normalize_invocation_sets(
        record["invocation_sets"],
        authority_root=authority_root,
        historical_context=historical_context,
    )
    if record["invocation_sets"] != normalized:
        raise ExperimentSandboxError(
            "model invocation set authority is not canonical"
        )
    _nonempty_text(record["run_id"], "model invocation run id")
    _validate_experiment_lifecycle_registry_seal(
        authority_root,
        reference,
        normalized,
        expected_run_id=record["run_id"],
    )
    return record


def _validate_namespace_evidence(descriptor, evidence):
    if not isinstance(evidence, dict):
        raise ExperimentSandboxError("namespace evidence must be an object")
    expected = {
        "schema_version": NAMESPACE_PROBE_SCHEMA_VERSION,
        "evidence_status": "complete",
        "denial_status": "denied",
        "policy_sha256": descriptor.get("policy_sha256"),
        "path_visible": False,
        "content_readable": False,
        "probe_returncode": 0,
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise ExperimentSandboxError(
                f"namespace evidence does not prove {field}"
            )
    digest = evidence.get("canary_sha256")
    if not _is_sha256(digest):
        raise ExperimentSandboxError("namespace evidence has invalid canary digest")


def _validate_historical_cleanup_context(
    sealed_result,
    cleanup_receipt,
    clean_snapshot_attestation,
):
    if (
        not isinstance(sealed_result, dict)
        or not isinstance(sealed_result.get("bundle"), dict)
        or not isinstance(cleanup_receipt, dict)
        or not isinstance(clean_snapshot_attestation, dict)
    ):
        raise ExperimentSandboxError(
            "historical sandbox replay requires sealed cleanup authority"
        )
    bundle = sealed_result["bundle"]
    if (
        cleanup_receipt.get("schema_version")
        != "experiment_clean_snapshot_cleanup.v1"
        or cleanup_receipt.get("cleanup_status")
        not in {"removed", "already_absent"}
        or cleanup_receipt.get("result_preserved") is not True
        or cleanup_receipt.get("experiment_run_id")
        != bundle.get("experiment_run_id")
        or sealed_result.get("cleanup_status")
        != cleanup_receipt.get("cleanup_status")
    ):
        raise ExperimentSandboxError(
            "historical sandbox replay cleanup authority is incomplete"
        )
    result_authority = cleanup_receipt.get("sealed_result")
    if (
        not isinstance(result_authority, dict)
        or result_authority.get("path")
        != sealed_result.get("result_dir")
        or result_authority.get("bundle_sha256")
        != sealed_result.get("bundle_sha256")
        or result_authority.get("sha256_before_cleanup")
        != result_authority.get("sha256_after_cleanup")
        or not _is_sha256(
            result_authority.get("sha256_after_cleanup")
        )
    ):
        raise ExperimentSandboxError(
            "historical sandbox replay result binding is invalid"
        )
    attestation_reference = cleanup_receipt.get(
        "clean_snapshot_attestation"
    )
    if not isinstance(attestation_reference, dict):
        raise ExperimentSandboxError(
            "historical sandbox replay lacks snapshot authority"
        )
    try:
        attestation_path = Path(attestation_reference["path"])
        attestation_payload = _read_bounded_regular_file(
            attestation_path,
            max_bytes=4 * 1024 * 1024,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExperimentSandboxError(
            "historical snapshot attestation reference is invalid"
        ) from exc
    if (
        hashlib.sha256(attestation_payload).hexdigest()
        != attestation_reference.get("sha256")
        or attestation_payload
        != _canonical_json_bytes(clean_snapshot_attestation) + b"\n"
    ):
        raise ExperimentSandboxError(
            "historical snapshot attestation digest changed"
        )
    deleted_snapshot_path = Path(
        clean_snapshot_attestation.get("snapshot_path", "")
    )
    if (
        clean_snapshot_attestation.get("schema_version")
        != "experiment_clean_snapshot.v1"
        or clean_snapshot_attestation.get("experiment_run_id")
        != bundle.get("experiment_run_id")
        or str(deleted_snapshot_path)
        != cleanup_receipt.get("cleanup_target")
        or str(deleted_snapshot_path)
        != attestation_reference.get("snapshot_path")
        or not deleted_snapshot_path.is_absolute()
        or deleted_snapshot_path.exists()
        or deleted_snapshot_path.is_symlink()
    ):
        raise ExperimentSandboxError(
            "historical snapshot cleanup binding is invalid"
        )
    return {
        "sealed_result": sealed_result,
        "cleanup_receipt": cleanup_receipt,
        "clean_snapshot_attestation": clean_snapshot_attestation,
        "deleted_snapshot_path": deleted_snapshot_path,
    }


def validate_provider_sandbox_descriptor(
    descriptor,
):
    """Validate a live descriptor with non-downgradable namespace evidence."""

    return _validate_provider_sandbox_descriptor(
        descriptor,
        require_namespace_evidence=True,
        verify_repository_identity=True,
    )


def _validate_provider_sandbox_descriptor(
    descriptor,
    *,
    require_namespace_evidence=True,
    verify_repository_identity=False,
    historical_context=None,
):
    if not isinstance(descriptor, dict):
        raise ExperimentSandboxError("provider sandbox descriptor must be an object")
    required = {
        "schema_version",
        "bwrap_path",
        "bwrap_sha256",
        "repository",
        "repository_identity",
        "runtime_views",
        "library_views",
        "credential_views",
        "environment",
        "network_policy",
        "namespace_evidence",
        "policy_sha256",
    }
    if set(descriptor) != required:
        raise ExperimentSandboxError(
            "provider sandbox descriptor contains undeclared fields"
        )
    if descriptor["schema_version"] != PROVIDER_SANDBOX_SCHEMA_VERSION:
        raise ExperimentSandboxError("unsupported provider sandbox descriptor")
    if descriptor["network_policy"] not in _NETWORK_POLICIES:
        raise ExperimentSandboxError("provider network policy is invalid")
    if descriptor["policy_sha256"] != _sandbox_policy_sha256(descriptor):
        raise ExperimentSandboxError("provider sandbox policy digest mismatch")
    bwrap = _existing_path(
        descriptor["bwrap_path"],
        "bubblewrap",
        require_file=True,
    )
    if (
        bwrap != _TRUSTED_BWRAP_PATH
        or not os.access(bwrap, os.X_OK)
        or not _is_sha256(descriptor["bwrap_sha256"])
        or hashlib.sha256(
            _read_bounded_regular_file(bwrap, max_bytes=16 * 1024 * 1024)
        ).hexdigest()
        != descriptor["bwrap_sha256"]
    ):
        raise ExperimentSandboxUnavailable(
            "bubblewrap executable identity is unavailable or changed"
        )
    repository = descriptor["repository"]
    if (
        not isinstance(repository, dict)
        or set(repository)
        != {"source", "target", "writable", "source_identity"}
        or repository.get("writable") is not True
    ):
        raise ExperimentSandboxError("repository must be the sole writable view")
    repository_identity = descriptor["repository_identity"]
    repository_source = Path(repository["source"])
    historical_repository = (
        historical_context is not None
        and repository_source
        == historical_context["deleted_snapshot_path"]
    )
    if historical_repository:
        if repository_source.exists() or repository_source.is_symlink():
            raise ExperimentSandboxError(
                "historical cleanup context requires an absent snapshot"
            )
        attested_repository = historical_context[
            "clean_snapshot_attestation"
        ]["repository"]
        expected_repository_identity = {
            field: attested_repository[field]
            for field in ("commit", "tree", "git_object_format")
        }
        if repository_identity != expected_repository_identity:
            raise ExperimentSandboxError(
                "historical sandbox repository identity differs from "
                "clean snapshot attestation"
            )
    elif repository_identity is not None:
        _validated_repository_identity(
            repository_source,
            repository_identity,
            verify_workspace=verify_repository_identity,
        )
    if not historical_repository:
        _validate_mount_source(
            repository["source"],
            repository["source_identity"],
            "repository",
            require_directory=True,
        )
    _absolute_target(repository["target"], "repository")
    _validate_normalized_views(descriptor["runtime_views"], "runtime")
    _validate_normalized_views(descriptor["library_views"], "library")
    _validate_normalized_credentials(descriptor["credential_views"])
    _deny_overlapping_targets(
        [
            repository,
            *descriptor["runtime_views"],
            *descriptor["library_views"],
            *descriptor["credential_views"],
        ]
    )
    repository_target = Path(repository["target"])
    for view in (
        *descriptor["runtime_views"],
        *descriptor["library_views"],
        *descriptor["credential_views"],
    ):
        if _paths_overlap(repository_source, Path(view["source"])):
            raise ExperimentSandboxError(
                "repository source overlaps another provider mount"
            )
        if _paths_overlap(repository_target, Path(view["target"])):
            raise ExperimentSandboxError(
                "writable repository target overlaps a read-only mount"
            )
    if descriptor["environment"] != _normalize_environment(
        descriptor["environment"]
    ):
        raise ExperimentSandboxError("provider environment is not canonical")
    if require_namespace_evidence:
        _validate_namespace_evidence(
            descriptor,
            descriptor.get("namespace_evidence"),
        )
    return descriptor


def prepare_provider_launch(
    descriptor,
    command,
    *,
    cwd,
    include_credentials=True,
):
    """Apply one live descriptor with complete namespace evidence."""

    return _prepare_provider_launch(
        descriptor,
        command,
        cwd=cwd,
        require_namespace_evidence=True,
        include_credentials=include_credentials,
        allow_network=(
            descriptor.get("network_policy") == "provider_access"
        ),
    )


def _prepare_probe_provider_launch(
    descriptor,
    command,
    *,
    cwd,
):
    """Private pre-evidence launch used only by the canary probe."""

    return _prepare_provider_launch(
        descriptor,
        command,
        cwd=cwd,
        require_namespace_evidence=False,
        include_credentials=True,
        allow_network=False,
    )


def _prepare_provider_launch(
    descriptor,
    command,
    *,
    cwd,
    require_namespace_evidence,
    include_credentials,
    allow_network,
):
    """Build one launch after the caller selects its private/public policy."""

    _validate_provider_sandbox_descriptor(
        descriptor,
        require_namespace_evidence=require_namespace_evidence,
        verify_repository_identity=True,
    )
    command = _normalize_argv(command, "provider command")
    repository_source = Path(descriptor["repository"]["source"])
    cwd = _existing_path(cwd, "provider cwd", require_directory=True)
    try:
        relative_cwd = cwd.relative_to(repository_source)
    except ValueError as exc:
        raise ExperimentSandboxError(
            "provider cwd must remain inside the declared repository"
        ) from exc
    sandbox_cwd = Path(descriptor["repository"]["target"]) / relative_cwd

    arguments = [
        descriptor["bwrap_path"],
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if allow_network:
        if descriptor["network_policy"] != "provider_access":
            raise ExperimentSandboxError(
                "provider launch cannot enable undeclared network access"
            )
        arguments.append("--share-net")
    arguments.extend(
        [
        "--cap-drop",
        "ALL",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run",
        "--dir",
        str(_CREDENTIAL_ROOT),
        "--dir",
        "/tmp/agentteam-home",
        ]
    )
    credential_views = (
        descriptor["credential_views"] if include_credentials else []
    )
    views = [
        descriptor["repository"],
        *descriptor["runtime_views"],
        *descriptor["library_views"],
        *credential_views,
    ]
    arguments.extend(_target_parent_arguments(view["target"] for view in views))
    arguments.extend(
        [
            "--bind",
            descriptor["repository"]["source"],
            descriptor["repository"]["target"],
        ]
    )
    for view in (
        *descriptor["runtime_views"],
        *descriptor["library_views"],
        *credential_views,
    ):
        arguments.extend(["--ro-bind", view["source"], view["target"]])
    if include_credentials:
        launch_environment = dict(descriptor["environment"])
    else:
        launch_environment = _normalize_environment(None)
        launch_environment.update(
            {
                name: value
                for name, value in descriptor["environment"].items()
                if name in _TRUSTED_EVALUATOR_ENVIRONMENT_NAMES
            }
        )
    if not allow_network:
        for name in _PROXY_ENVIRONMENT_NAMES:
            launch_environment.pop(name, None)
    arguments.append("--clearenv")
    for name, value in launch_environment.items():
        arguments.extend(["--setenv", name, value])
    arguments.extend(["--chdir", str(sandbox_cwd), "--", *command])
    return PreparedProviderLaunch(
        command=tuple(arguments),
        cwd="/",
        environment=launch_environment,
        policy_sha256=descriptor["policy_sha256"],
        _source_descriptor_json=_canonical_json_bytes(descriptor),
        _repository_workspace_sha256=_repository_workspace_sha256(
            repository_source,
            require_git=descriptor.get("repository_identity") is not None,
        ),
    )


def prepare_candidate_evaluation_launch(
    descriptor,
    evaluator_artifact,
    evaluator_sha256,
    acceptance_command,
    *,
    cwd,
):
    """Build a runtime-owned bwrap command that contains the evaluator."""

    artifact = _existing_path(
        evaluator_artifact,
        "trusted evaluator authority",
        require_file=True,
        reject_symlink=True,
    )
    for view in (
        descriptor["repository"],
        *descriptor["runtime_views"],
        *descriptor["library_views"],
        *descriptor["credential_views"],
    ):
        if _paths_overlap(artifact, Path(view["source"])):
            raise ExperimentSandboxError(
                "trusted evaluator overlaps a provider-visible mount"
            )
    if not _is_sha256(evaluator_sha256):
        raise ExperimentSandboxError(
            "trusted evaluator digest is invalid"
        )
    loader_python = _probe_python_for_descriptor(descriptor)
    prepared = _prepare_provider_launch(
        descriptor,
        [
            str(loader_python),
            "-c",
            _EVALUATOR_LOADER_SCRIPT,
            evaluator_sha256,
            *_normalize_argv(
                acceptance_command,
                "candidate acceptance command",
            ),
        ],
        cwd=cwd,
        require_namespace_evidence=True,
        include_credentials=False,
        allow_network=False,
    )
    repository_source = Path(descriptor["repository"]["source"])
    git_source = _existing_path(
        repository_source / ".git",
        "candidate Git control directory",
        require_directory=True,
        reject_symlink=True,
    )
    repository_target = Path(descriptor["repository"]["target"])
    command = list(prepared.command)
    try:
        chdir_index = command.index("--chdir")
    except ValueError as exc:
        raise ExperimentSandboxError(
            "candidate sandbox command is missing its working directory"
        ) from exc
    command[chdir_index:chdir_index] = [
        "--ro-bind",
        str(git_source),
        str(repository_target / ".git"),
    ]
    return PreparedProviderLaunch(
        command=tuple(command),
        cwd=prepared.cwd,
        environment=dict(prepared.environment),
        policy_sha256=prepared.policy_sha256,
        _source_descriptor_json=prepared._source_descriptor_json,
        _repository_workspace_sha256=(
            prepared._repository_workspace_sha256
        ),
    )


def validate_provider_authority_separation(descriptor, *authority_roots):
    """Reject authority state that a provider-visible host mount can modify."""

    provider_sources = [
        Path(view["source"])
        for view in (
            descriptor["repository"],
            *descriptor["runtime_views"],
            *descriptor["library_views"],
            *descriptor["credential_views"],
        )
    ]
    for value in authority_roots:
        root = _existing_path(
            value,
            "experiment authority root",
            require_directory=True,
            reject_symlink=True,
        )
        if any(_is_relative_to(root, source) for source in provider_sources):
            raise ExperimentSandboxError(
                "experiment authority root is provider-visible"
            )


def experiment_lifecycle_authority_root(authority_root, lifecycle_id):
    """Allocate one controller-owned lifecycle root in the fixed registry."""

    authority_root = Path(authority_root).resolve(strict=True)
    lifecycle_id = _safe_reference_id(lifecycle_id)
    with _experiment_lifecycle_registry_lock(authority_root) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            authority_dir = _experiment_authority_dir(authority_root)
            if (
                _experiment_lifecycle_registry_seal_path(
                    authority_root
                ).exists()
                or any(authority_dir.glob("*.invocation-set.json"))
            ):
                raise ExperimentSandboxError(
                    "experiment lifecycle registry is sealed"
                )
            registry = _experiment_lifecycle_registry_dir(authority_root)
            root = registry / lifecycle_id
            root.mkdir(mode=0o700, exist_ok=False)
            registration_path = authority_dir / (
                f"{lifecycle_id}.lifecycle-registration.json"
            )
            try:
                _publish_immutable_json(
                    registration_path,
                    {
                        "schema_version": (
                            "experiment_lifecycle_registration.v1"
                        ),
                        "lifecycle_id": lifecycle_id,
                        "lifecycle_authority_root": str(root),
                    },
                )
            except Exception:
                root.rmdir()
                raise
            return root
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def validate_experiment_lifecycle_authority(
    authority_root,
    lifecycle_authority_root,
):
    root = _existing_path(
        lifecycle_authority_root,
        "model invocation lifecycle authority",
        require_directory=True,
        reject_symlink=True,
    )
    if root.parent != _experiment_lifecycle_registry_dir(authority_root):
        raise ExperimentSandboxError(
            "model invocation lifecycle authority is outside the registry"
        )
    if str(root) not in _registered_lifecycle_roots(authority_root):
        raise ExperimentSandboxError(
            "model invocation lifecycle authority is not registered"
        )
    return root


def publish_experiment_launch_registration(
    authority_root,
    lifecycle_authority_root,
    *,
    experiment_run_id,
    protocol_sha256,
    run_manifest_sha256,
    mode,
    usage_stage,
    taskpack_id,
    workspace_root,
    sandbox_reference,
    controller_reference,
    model_policy,
    tool_budget_route=None,
):
    """Bind one registered lifecycle to its non-optional launch policy."""
    authority_root = Path(authority_root).resolve(strict=True)
    mode_authority = load_experiment_mode_authority(authority_root)
    selected_model_policy = _normalize_experiment_model_policy(model_policy)
    expected_mode_binding = {
        "experiment_run_id": experiment_run_id,
        "protocol_sha256": protocol_sha256,
        "run_manifest_sha256": run_manifest_sha256,
        "mode": mode,
    }
    if any(
        mode_authority.get(field) != value
        for field, value in expected_mode_binding.items()
    ):
        raise ExperimentSandboxError(
            "launch registration differs from mode authority"
        )
    normalized_tool_route = None
    if tool_budget_route is None:
        if mode_authority.get("tool_budget_risk_target") is not None:
            raise ExperimentSandboxError(
                "tool budget route is required by mode authority"
            )
        if mode_authority["model_policy"] != selected_model_policy:
            raise ExperimentSandboxError(
                "launch registration differs from mode authority"
            )
    else:
        if not isinstance(tool_budget_route, dict):
            raise ExperimentSandboxError("tool budget route is invalid")
        try:
            expected_route = select_tool_budget_route(
                mode_authority["model_policy"],
                role=tool_budget_route.get("role"),
                risk_target=tool_budget_route.get("risk_target"),
            )
        except ValueError as exc:
            raise ExperimentSandboxError(
                f"tool budget route is invalid: {exc}"
            ) from exc
        if tool_budget_route != expected_route:
            raise ExperimentSandboxError(
                "tool budget route differs from mode authority"
            )
        authorized_risk = mode_authority.get("tool_budget_risk_target")
        if (
            authorized_risk is not None
            and expected_route["risk_target"] != authorized_risk
        ):
            raise ExperimentSandboxError(
                "tool budget route risk differs from mode authority"
            )
        if selected_model_policy != _normalize_experiment_model_policy(
            expected_route["policy"]
        ):
            raise ExperimentSandboxError(
                "selected model policy differs from tool budget route"
            )
        normalized_tool_route = expected_route
    lifecycle_root = validate_experiment_lifecycle_authority(
        authority_root,
        lifecycle_authority_root,
    )
    workspace_root = _existing_path(
        workspace_root,
        "experiment launch workspace",
        require_directory=True,
        reject_symlink=True,
    )
    descriptor = load_provider_sandbox_reference(
        sandbox_reference,
        authority_root,
    )
    if (
        descriptor["network_policy"]
        != selected_model_policy["network_policy"]
    ):
        raise ExperimentSandboxError(
            "sandbox network policy differs from mode authority"
        )
    validate_provider_authority_separation(
        descriptor,
        authority_root,
        lifecycle_root,
    )
    if Path(descriptor["repository"]["source"]) != workspace_root:
        raise ExperimentSandboxError(
            "launch workspace differs from sandbox repository"
        )
    from .experiment_controller import (
        validate_experiment_controller_reference,
    )

    controller_reference = validate_experiment_controller_reference(
        controller_reference,
    )
    if (
        mode_authority["controller_reference"]
        != controller_reference
    ):
        raise ExperimentSandboxError(
            "launch controller differs from mode authority"
        )
    validate_provider_authority_separation(
        descriptor,
        controller_reference["controller_root"],
    )
    record = {
        "schema_version": LAUNCH_REGISTRATION_SCHEMA_VERSION,
        "experiment_run_id": _nonempty_text(
            experiment_run_id,
            "experiment launch run id",
        ),
        "protocol_sha256": _require_sha256(
            protocol_sha256,
            "experiment launch protocol digest",
        ),
        "run_manifest_sha256": _require_sha256(
            run_manifest_sha256,
            "experiment launch manifest digest",
        ),
        "mode": _experiment_mode(mode),
        "usage_stage": _nonempty_text(
            usage_stage,
            "experiment launch usage stage",
        ),
        "taskpack_id": _nonempty_text(
            taskpack_id,
            "experiment launch taskpack id",
        ),
        "lifecycle_authority_root": str(lifecycle_root),
        "workspace_root": str(workspace_root),
        "sandbox_reference": dict(sandbox_reference),
        "sandbox_policy_sha256": descriptor["policy_sha256"],
        "controller_reference": controller_reference,
        "model_policy": selected_model_policy,
        **(
            {"tool_budget_routing": normalized_tool_route}
            if normalized_tool_route is not None
            else {}
        ),
    }
    path = _experiment_authority_dir(authority_root) / (
        f"{lifecycle_root.name}.launch-registration.json"
    )
    _publish_immutable_json(path, record)
    payload = _read_bounded_regular_file(path, max_bytes=256 * 1024)
    return {
        "schema_version": (
            "experiment_launch_registration_reference.v1"
        ),
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "registration": record,
    }


def publish_experiment_mode_authority(
    authority_root,
    *,
    experiment_run_id,
    protocol_sha256,
    run_manifest_sha256,
    mode,
    model_policy,
    tool_budget_risk_target=None,
    controller_reference,
    sandbox_configuration_sha256,
):
    """Mark an authority root as requiring registered launch descriptors."""
    from .experiment_controller import (
        validate_experiment_controller_reference,
    )

    controller_reference = validate_experiment_controller_reference(
        controller_reference
    )
    if tool_budget_risk_target not in {None, "L1", "L2", "L3"}:
        raise ExperimentSandboxError(
            "experiment mode tool budget risk target is invalid"
        )
    record = {
        "schema_version": MODE_AUTHORITY_SCHEMA_VERSION,
        "experiment_run_id": _nonempty_text(
            experiment_run_id,
            "experiment mode run id",
        ),
        "protocol_sha256": _require_sha256(
            protocol_sha256,
            "experiment mode protocol digest",
        ),
        "run_manifest_sha256": _require_sha256(
            run_manifest_sha256,
            "experiment mode manifest digest",
        ),
        "mode": _experiment_mode(mode),
        "model_policy": _normalize_experiment_model_policy(
            model_policy
        ),
        **(
            {"tool_budget_risk_target": tool_budget_risk_target}
            if tool_budget_risk_target is not None
            else {}
        ),
        "controller_reference": controller_reference,
        "sandbox_configuration_sha256": _require_sha256(
            sandbox_configuration_sha256,
            "experiment sandbox configuration digest",
        ),
    }
    path = _experiment_authority_dir(authority_root) / (
        "experiment-mode-controller.json"
    )
    if path.exists():
        existing = load_experiment_mode_authority(authority_root)
        if existing != record:
            raise ExperimentSandboxError(
                "experiment mode authority conflicts with replay"
            )
        return existing
    try:
        _publish_immutable_json(path, record)
    except ExperimentSandboxError:
        if not path.exists():
            raise
        existing = load_experiment_mode_authority(authority_root)
        if existing != record:
            raise
        return existing
    return record


def load_experiment_mode_authority(authority_root):
    path = _experiment_authority_dir(authority_root) / (
        "experiment-mode-controller.json"
    )
    if not path.is_file() or path.is_symlink():
        raise ExperimentSandboxError(
            "experiment mode authority is unavailable"
        )
    try:
        record = json.loads(
            _read_bounded_regular_file(
                path,
                max_bytes=64 * 1024,
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxError(
            "experiment mode authority is unreadable"
        ) from exc
    required = {
        "schema_version",
        "experiment_run_id",
        "protocol_sha256",
        "run_manifest_sha256",
        "mode",
        "model_policy",
        "controller_reference",
        "sandbox_configuration_sha256",
    }
    optional = {"tool_budget_risk_target"}
    if (
        not isinstance(record, dict)
        or not required.issubset(record)
        or set(record) - required - optional
        or record["schema_version"] != MODE_AUTHORITY_SCHEMA_VERSION
    ):
        raise ExperimentSandboxError(
            "experiment mode authority fields are invalid"
        )
    _nonempty_text(record["experiment_run_id"], "experiment mode run id")
    _require_sha256(
        record["protocol_sha256"],
        "experiment mode protocol digest",
    )
    _require_sha256(
        record["run_manifest_sha256"],
        "experiment mode manifest digest",
    )
    _experiment_mode(record["mode"])
    record["model_policy"] = _normalize_experiment_model_policy(
        record["model_policy"]
    )
    if record.get("tool_budget_risk_target") not in {
        None,
        "L1",
        "L2",
        "L3",
    }:
        raise ExperimentSandboxError(
            "experiment mode tool budget risk target is invalid"
        )
    from .experiment_controller import (
        validate_experiment_controller_reference,
    )

    record["controller_reference"] = (
        validate_experiment_controller_reference(
            record["controller_reference"]
        )
    )
    _require_sha256(
        record["sandbox_configuration_sha256"],
        "experiment sandbox configuration digest",
    )
    return record


def load_experiment_launch_registration(lifecycle_authority_root):
    """Resolve and validate the controller-owned launch policy for a lifecycle."""
    candidate = Path(lifecycle_authority_root)
    if not candidate.exists() and not candidate.is_symlink():
        return None
    lifecycle_root = _existing_path(
        candidate,
        "experiment launch lifecycle",
        require_directory=True,
        reject_symlink=True,
    )
    if lifecycle_root.parent.name != "experiment_lifecycles":
        return None
    authority_root = lifecycle_root.parent.parent
    validate_experiment_lifecycle_authority(
        authority_root,
        lifecycle_root,
    )
    authority_dir = _experiment_authority_dir(authority_root)
    path = authority_dir / (
        f"{lifecycle_root.name}.launch-registration.json"
    )
    if not path.exists():
        if (authority_dir / "experiment-mode-controller.json").is_file():
            raise ExperimentSandboxError(
                "experiment launch registration is unavailable"
            )
        return None
    path = _existing_path(
        path,
        "experiment launch registration",
        require_file=True,
        reject_symlink=True,
    )
    try:
        record = json.loads(
            _read_bounded_regular_file(
                path,
                max_bytes=256 * 1024,
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxError(
            "experiment launch registration is unreadable"
        ) from exc
    required = {
        "schema_version",
        "experiment_run_id",
        "protocol_sha256",
        "run_manifest_sha256",
        "mode",
        "usage_stage",
        "taskpack_id",
        "lifecycle_authority_root",
        "workspace_root",
        "sandbox_reference",
        "sandbox_policy_sha256",
        "controller_reference",
        "model_policy",
    }
    optional = {"tool_budget_routing"}
    if (
        not isinstance(record, dict)
        or not required.issubset(record)
        or set(record) - required - optional
        or record["schema_version"]
        != LAUNCH_REGISTRATION_SCHEMA_VERSION
        or record["lifecycle_authority_root"] != str(lifecycle_root)
    ):
        raise ExperimentSandboxError(
            "experiment launch registration fields are invalid"
        )
    _nonempty_text(record["experiment_run_id"], "experiment launch run id")
    _require_sha256(
        record["protocol_sha256"],
        "experiment launch protocol digest",
    )
    _require_sha256(
        record["run_manifest_sha256"],
        "experiment launch manifest digest",
    )
    _experiment_mode(record["mode"])
    _nonempty_text(record["usage_stage"], "experiment launch usage stage")
    _nonempty_text(record["taskpack_id"], "experiment launch taskpack id")
    model_policy = _normalize_experiment_model_policy(
        record["model_policy"]
    )
    workspace_root = _existing_path(
        record["workspace_root"],
        "experiment launch workspace",
        require_directory=True,
        reject_symlink=True,
    )
    descriptor = load_provider_sandbox_reference(
        record["sandbox_reference"],
        authority_root,
    )
    if (
        Path(descriptor["repository"]["source"]) != workspace_root
        or descriptor["policy_sha256"]
        != record["sandbox_policy_sha256"]
    ):
        raise ExperimentSandboxError(
            "experiment launch sandbox binding changed"
        )
    from .experiment_controller import (
        validate_experiment_controller_reference,
    )

    controller_reference = validate_experiment_controller_reference(
        record["controller_reference"],
    )
    mode_authority = load_experiment_mode_authority(authority_root)
    if mode_authority["controller_reference"] != controller_reference:
        raise ExperimentSandboxError(
            "experiment launch controller binding changed"
        )
    route = record.get("tool_budget_routing")
    if route is None:
        if mode_authority.get("tool_budget_risk_target") is not None:
            raise ExperimentSandboxError(
                "experiment launch tool budget route is unavailable"
            )
        if model_policy != mode_authority["model_policy"]:
            raise ExperimentSandboxError(
                "registered lifecycle differs from mode authority"
            )
    else:
        try:
            expected_route = select_tool_budget_route(
                mode_authority["model_policy"],
                role=route.get("role"),
                risk_target=route.get("risk_target"),
            )
        except (AttributeError, ValueError) as exc:
            raise ExperimentSandboxError(
                f"experiment launch tool budget route is invalid: {exc}"
            ) from exc
        if route != expected_route or model_policy != expected_route["policy"]:
            raise ExperimentSandboxError(
                "experiment launch tool budget binding changed"
            )
        authorized_risk = mode_authority.get("tool_budget_risk_target")
        if (
            authorized_risk is not None
            and expected_route["risk_target"] != authorized_risk
        ):
            raise ExperimentSandboxError(
                "experiment launch tool budget risk binding changed"
            )
    validate_provider_authority_separation(
        descriptor,
        authority_root,
        lifecycle_root,
        controller_reference["controller_root"],
    )
    return {
        **record,
        "workspace_root": str(workspace_root),
        "controller_reference": controller_reference,
        "model_policy": model_policy,
        "authority_root": str(authority_root),
        "registration_path": str(path),
    }


def scan_canary_leakage(
    scan_groups,
    *,
    canary_path,
    max_files=DEFAULT_MAX_SCAN_FILES,
    max_bytes=DEFAULT_MAX_SCAN_BYTES,
):
    """Scan bounded retained inputs/artifacts for canary, digest, or path."""

    if not isinstance(scan_groups, dict) or set(scan_groups) != {
        "prompt",
        "context",
        "taskpack",
        "artifacts",
    }:
        raise ExperimentSandboxError(
            "leak scan groups must be prompt, context, taskpack, and artifacts"
        )
    if any(
        not isinstance(scan_groups[group], (str, os.PathLike, list, tuple))
        or (
            isinstance(scan_groups[group], (list, tuple))
            and not scan_groups[group]
        )
        for group in scan_groups
    ):
        raise ExperimentSandboxError(
            "every leak scan group must declare at least one path"
        )
    canary_path = _existing_path(
        canary_path,
        "gold canary",
        require_file=True,
        reject_symlink=True,
    )
    canary = _read_bounded_regular_file(canary_path, max_bytes=64 * 1024)
    if not canary:
        raise ExperimentSandboxError("gold canary must not be empty")
    digest = hashlib.sha256(canary).hexdigest()
    needles = (
        ("canary_content", canary),
        ("canary_sha256", digest.encode("ascii")),
        ("canary_sha256", digest.upper().encode("ascii")),
        ("canary_path", str(canary_path).encode("utf-8")),
    )
    try:
        max_files = int(max_files)
        max_bytes = int(max_bytes)
    except (TypeError, ValueError) as exc:
        raise ExperimentSandboxError("canary leak scan bounds are invalid") from exc
    if not 1 <= max_files <= DEFAULT_MAX_SCAN_FILES:
        raise ExperimentSandboxError("canary leak scan file bound is invalid")
    if not 1 <= max_bytes <= DEFAULT_MAX_SCAN_BYTES:
        raise ExperimentSandboxError("canary leak scan byte bound is invalid")

    findings = []
    scanned_files = 0
    scanned_entries = 0
    scanned_bytes = 0
    group_counts = {}
    for group in ("prompt", "context", "taskpack", "artifacts"):
        files, entry_count = _bounded_regular_files(
            scan_groups[group],
            max_entries=max_files - scanned_entries,
        )
        scanned_entries += entry_count
        group_counts[group] = len(files)
        for path in files:
            scanned_files += 1
            if scanned_files > max_files:
                raise ExperimentSandboxUnavailable(
                    "canary leak scan file bound was exceeded"
                )
            content = _read_bounded_regular_file(
                path,
                max_bytes=max_bytes - scanned_bytes,
            )
            scanned_bytes += len(content)
            for kind, needle in needles:
                if needle and needle in content:
                    findings.append(
                        {
                            "group": group,
                            "path_sha256": hashlib.sha256(
                                str(path).encode("utf-8")
                            ).hexdigest(),
                            "match": kind,
                        }
                    )
    return {
        "scan_status": "leak_detected" if findings else "clean",
        "scan_scope_sha256": scan_scope_sha256(scan_groups),
        "canary_sha256": digest,
        "scanned_files": scanned_files,
        "scanned_entries": scanned_entries,
        "scanned_bytes": scanned_bytes,
        "group_file_counts": group_counts,
        "findings": findings,
    }


def run_trusted_argv_evaluator(
    *,
    authority_root,
    invocation_set_reference,
    provider_sandbox_reference,
    experiment_protocol_reference,
    scan_scope_reference,
    command,
    cwd,
    evaluator_reference,
    canary_path,
    timeout_seconds,
    evidence_path=None,
    max_output_bytes=DEFAULT_MAX_EVALUATOR_OUTPUT_BYTES,
    resource_envelope_binding=None,
    resource_mode=None,
    resource_envelope_required=False,
    resource_project_id=None,
    resource_hierarchy_reference=None,
):
    """Run post-model acceptance and return schema-validated bounded evidence."""

    started_at = _utc_timestamp()
    base = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "evaluation_status": "blocked",
        "evidence_status": "complete",
        "started_at": started_at,
        "finished_at": started_at,
        "evaluator_started": False,
        "promotion_eligible": False,
        "execution_boundary": "not_started",
        "systemd_unit": None,
        "argv": [],
        "cwd": str(Path(cwd).resolve(strict=False)),
        "timeout_seconds": timeout_seconds,
        "evaluator_artifact": (
            str(evaluator_reference.get("path"))
            if isinstance(evaluator_reference, dict)
            else "<unresolved>"
        ),
        "evaluator_sha256": (
            evaluator_reference.get("sha256")
            if isinstance(evaluator_reference, dict)
            and _is_sha256(evaluator_reference.get("sha256"))
            else "0" * 64
        ),
        "environment_sha256": "0" * 64,
        "candidate_repository": None,
        "run_id": "<unresolved>",
        "taskpack_ids": [],
        "expected_invocation_ids": [],
        "invocation_sets": [],
        "invocation_set_reference_sha256": (
            invocation_set_reference.get("sha256")
            if isinstance(invocation_set_reference, dict)
            and _is_sha256(invocation_set_reference.get("sha256"))
            else "0" * 64
        ),
        "experiment_protocol_sha256": "0" * 64,
        "experiment_protocol_reference_sha256": (
            experiment_protocol_reference.get("sha256")
            if isinstance(experiment_protocol_reference, dict)
            and _is_sha256(experiment_protocol_reference.get("sha256"))
            else "0" * 64
        ),
        "acceptance_command_sha256": "0" * 64,
        "acceptance_executable_sha256": "0" * 64,
        "scan_scope_sha256": "0" * 64,
        "provider_sandbox_reference_sha256": (
            provider_sandbox_reference.get("sha256")
            if isinstance(provider_sandbox_reference, dict)
            and _is_sha256(provider_sandbox_reference.get("sha256"))
            else "0" * 64
        ),
        "provider_sandbox_policy_sha256": "0" * 64,
        "invocation_set_seal_sha256": "0" * 64,
        "terminal_invocations": [],
        "pre_run_leak_scan": None,
        "post_run_leak_scan": None,
        "returncode": None,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "stdout_truncated": False,
        "stderr_truncated": False,
        "max_output_bytes": max_output_bytes,
        "failure_reason": None,
    }
    try:
        timeout_seconds = float(timeout_seconds)
        if not 1 <= timeout_seconds <= MAX_EVALUATION_TIMEOUT_SECONDS:
            raise ExperimentSandboxError("evaluation timeout is out of bounds")
        base["timeout_seconds"] = timeout_seconds
        authority_root = _existing_path(
            authority_root,
            "experiment authority root",
            require_directory=True,
            reject_symlink=True,
        )
        invocation_manifest = load_model_invocation_set_reference(
            invocation_set_reference,
            authority_root,
        )
        base["run_id"] = invocation_manifest["run_id"]
        base["taskpack_ids"] = sorted(
            {
                item["taskpack_id"]
                for item in invocation_manifest["invocation_sets"]
            }
        )
        base["expected_invocation_ids"] = sorted(
            invocation_id
            for item in invocation_manifest["invocation_sets"]
            for invocation_id in item["invocation_ids"]
        )
        base["invocation_set_reference_sha256"] = (
            invocation_set_reference["sha256"]
        )
        experiment_protocol = load_experiment_protocol_reference(
            experiment_protocol_reference,
            authority_root,
        )
        base["experiment_protocol_reference_sha256"] = (
            experiment_protocol_reference["sha256"]
        )
        if resource_envelope_binding is not None or resource_envelope_required:
            from .experiment_protocol import (
                validate_protocol_resource_envelope,
            )

            validate_protocol_resource_envelope(
                experiment_protocol,
                resource_envelope_binding,
                require_binding=resource_envelope_required,
            )
        if resource_envelope_binding is not None and resource_mode not in {
            "single_codex",
            "agentteam_direct",
            "agentteam_full",
        }:
            raise ExperimentSandboxError(
                "resource-bound evaluator requires an experiment mode"
            )
        expected_command = list(experiment_protocol["acceptance"]["command"])
        if list(command) != expected_command:
            raise ExperimentSandboxError(
                "evaluator argv does not match the preregistered acceptance command"
            )
        if timeout_seconds != float(
            experiment_protocol["acceptance"]["timeout_seconds"]
        ):
            raise ExperimentSandboxError(
                "evaluation timeout does not match the preregistered protocol"
            )
        base["experiment_protocol_sha256"] = hashlib.sha256(
            _canonical_json_bytes(experiment_protocol)
        ).hexdigest()
        base["acceptance_command_sha256"] = hashlib.sha256(
            _canonical_json_bytes(expected_command)
        ).hexdigest()
        provider_sandbox_descriptor = load_provider_sandbox_reference(
            provider_sandbox_reference,
            authority_root,
        )
        validate_provider_authority_separation(
            provider_sandbox_descriptor,
            authority_root,
        )
        scan_groups, actual_scan_scope_sha256 = load_scan_scope_reference(
            scan_scope_reference,
            authority_root,
        )
        _validate_descriptor_excludes_canary(
            provider_sandbox_descriptor,
            canary_path,
        )
        repository_identity = provider_sandbox_descriptor[
            "repository_identity"
        ]
        protocol_repository_identity = {
            field: experiment_protocol["repository"][field]
            for field in ("commit", "tree", "git_object_format")
        }
        actual_canary_sha256 = hashlib.sha256(
            _read_bounded_regular_file(
                _existing_path(
                    canary_path,
                    "gold canary",
                    require_file=True,
                    reject_symlink=True,
                ),
                max_bytes=64 * 1024,
            )
        ).hexdigest()
        if (
            provider_sandbox_descriptor["namespace_evidence"][
                "canary_sha256"
            ]
            != actual_canary_sha256
        ):
            raise ExperimentSandboxError(
                "sandbox namespace evidence is bound to another canary"
            )
        base["scan_scope_sha256"] = actual_scan_scope_sha256
        base["provider_sandbox_policy_sha256"] = provider_sandbox_descriptor[
            "policy_sha256"
        ]
        terminal_invocations = []
        invocation_set_evidence = []
        for item in invocation_manifest["invocation_sets"]:
            invocation_sandbox = load_provider_sandbox_reference(
                item["sandbox_reference"],
                authority_root,
            )
            if (
                invocation_sandbox["namespace_evidence"]["canary_sha256"]
                != actual_canary_sha256
            ):
                raise ExperimentSandboxError(
                    "model invocation sandbox reference is bound to another canary"
                )
            terminals, seal_sha256 = _terminal_invocation_evidence(
                Path(item["lifecycle_authority_root"])
                / "model_invocations",
                expected_invocation_ids=item["invocation_ids"],
                expected_run_id=invocation_manifest["run_id"],
                expected_taskpack_id=item["taskpack_id"],
                expected_sandbox_policy_sha256=item[
                    "sandbox_policy_sha256"
                ],
                expected_sandbox_reference_sha256=item[
                    "sandbox_reference"
                ]["sha256"],
            )
            terminal_invocations.extend(terminals)
            invocation_set_evidence.append(
                {
                    "taskpack_id": item["taskpack_id"],
                    "expected_invocation_ids": item["invocation_ids"],
                    "sandbox_policy_sha256": item[
                        "sandbox_policy_sha256"
                    ],
                    "sandbox_reference_sha256": item[
                        "sandbox_reference"
                    ]["sha256"],
                    "seal_sha256": seal_sha256,
                }
            )
        base["terminal_invocations"] = sorted(
            terminal_invocations,
            key=lambda item: item["invocation_id"],
        )
        base["invocation_sets"] = sorted(
            invocation_set_evidence,
            key=lambda item: (
                item["taskpack_id"],
                item["expected_invocation_ids"],
            ),
        )
        base["invocation_set_seal_sha256"] = hashlib.sha256(
            _canonical_json_bytes(base["invocation_sets"])
        ).hexdigest()
        candidate_repository = _candidate_repository_state(
            provider_sandbox_descriptor["repository"]["source"],
            protocol_repository_identity,
        )
        if repository_identity != {
            "commit": candidate_repository["head_commit"],
            "tree": candidate_repository["head_tree"],
            "git_object_format": candidate_repository[
                "git_object_format"
            ],
        }:
            raise ExperimentSandboxError(
                "candidate sandbox identity does not match its certified HEAD"
            )
        base["candidate_repository"] = candidate_repository
        pre_scan = scan_canary_leakage(
            scan_groups,
            canary_path=canary_path,
        )
        base["pre_run_leak_scan"] = pre_scan
        if pre_scan["scan_scope_sha256"] != actual_scan_scope_sha256:
            raise ExperimentSandboxError("pre-run leak scan scope drift")
        if pre_scan["scan_status"] != "clean":
            raise ExperimentEvaluationBlocked(
                "canary leakage detected before trusted evaluation"
            )
        artifact, evaluator_sha256 = load_evaluator_reference(
            evaluator_reference,
            authority_root,
        )
        if (
            evaluator_sha256
            != experiment_protocol["evaluator"]["artifact_sha256"]
        ):
            raise ExperimentSandboxError("trusted evaluator digest mismatch")
        cwd_path = _existing_path(cwd, "evaluator cwd", require_directory=True)
        if cwd_path != Path(
            provider_sandbox_descriptor["repository"]["source"]
        ):
            raise ExperimentSandboxError(
                "evaluator cwd does not match the certified workspace"
            )
        for view in (
            provider_sandbox_descriptor["repository"],
            *provider_sandbox_descriptor["runtime_views"],
            *provider_sandbox_descriptor["library_views"],
            *provider_sandbox_descriptor["credential_views"],
        ):
            if _paths_overlap(artifact, Path(view["source"])):
                raise ExperimentSandboxError(
                    "trusted evaluator overlaps a provider-visible mount"
                )
        bounded_environment = _normalize_environment(None)
        base["environment_sha256"] = hashlib.sha256(
            _canonical_json_bytes(bounded_environment)
        ).hexdigest()
        acceptance_argv = _normalize_argv(command, "acceptance command")
        if _contains_canary_reference(
            "\0".join([*acceptance_argv, str(cwd_path)]),
            canary_path,
        ):
            raise ExperimentEvaluationBlocked(
                "evaluator argv or cwd contains evaluator-only canary material"
            )
        executable = _approved_acceptance_executable(
            acceptance_argv[0],
            cwd=cwd_path,
            environment=bounded_environment,
            descriptor=provider_sandbox_descriptor,
        )
        base["acceptance_executable_sha256"] = hashlib.sha256(
            _read_bounded_regular_file(
                executable,
                max_bytes=64 * 1024 * 1024,
            )
        ).hexdigest()
        evaluator_content = _read_digest_bound_evaluator(
            artifact,
            evaluator_sha256,
        )
        candidate_launch = prepare_candidate_evaluation_launch(
            provider_sandbox_descriptor,
            artifact,
            evaluator_sha256,
            acceptance_argv,
            cwd=cwd_path,
        )
        candidate_launch.revalidate_mutable_sources()
        evaluator_argv = list(candidate_launch.command)
        base["argv"] = evaluator_argv
        base["cwd"] = str(cwd_path)
        base["evaluator_artifact"] = str(artifact)
        base["evaluator_sha256"] = evaluator_sha256
        evaluator_arguments = {
            "cwd": cwd_path,
            "environment": bounded_environment,
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": int(max_output_bytes),
            "cpu_limit": experiment_protocol["environment"]["cpu_limit"],
            "memory_limit_bytes": experiment_protocol["environment"][
                "memory_limit_bytes"
            ],
            "input_bytes": evaluator_content,
            "prelaunch_source_authority": (
                candidate_launch.source_authority()
            ),
        }
        if resource_envelope_binding is not None:
            evaluator_arguments.update(
                {
                    "resource_envelope_binding": resource_envelope_binding,
                    "resource_mode": resource_mode,
                    "resource_run_id": base["run_id"],
                    "resource_hierarchy_reference": (
                        resource_hierarchy_reference
                    ),
                }
            )
            evaluator_arguments["resource_run_id"] = (
                resource_project_id or base["run_id"]
            )
        execution = _run_bounded_argv(
            evaluator_argv,
            **evaluator_arguments,
        )
        base["evaluator_started"] = True
        base.update(
            {
                "execution_boundary": execution["execution_boundary"],
                "systemd_unit": execution["systemd_unit"],
                "returncode": execution["returncode"],
                "timed_out": execution["timed_out"],
                "stdout": execution["stdout"],
                "stderr": execution["stderr"],
                "stdout_sha256": hashlib.sha256(
                    execution["stdout"].encode("utf-8")
                ).hexdigest(),
                "stderr_sha256": hashlib.sha256(
                    execution["stderr"].encode("utf-8")
                ).hexdigest(),
                "stdout_truncated": execution["stdout_truncated"],
                "stderr_truncated": execution["stderr_truncated"],
            }
        )
        resource_evidence = execution.get("resource_evidence")
        if resource_evidence is not None and evidence_path is not None:
            resource_path = Path(str(evidence_path) + ".resources.json")
            _publish_immutable_json(resource_path, resource_evidence)
        post_scan = scan_canary_leakage(
            scan_groups,
            canary_path=canary_path,
        )
        output_findings = _scan_output_bytes(
            execution["stdout_bytes"],
            execution["stderr_bytes"],
            canary_path,
        )
        post_scan = dict(post_scan)
        post_scan["findings"] = [
            *post_scan["findings"],
            *output_findings,
        ]
        if post_scan["findings"]:
            post_scan["scan_status"] = "leak_detected"
        base["post_run_leak_scan"] = post_scan
        post_candidate_repository = _candidate_repository_state(
            provider_sandbox_descriptor["repository"]["source"],
            protocol_repository_identity,
        )
        candidate_repository_unchanged = _candidate_source_unchanged(
            candidate_repository,
            post_candidate_repository,
        )
        if post_scan["scan_scope_sha256"] != actual_scan_scope_sha256:
            raise ExperimentSandboxError("post-run leak scan scope drift")
        passed = (
            execution["returncode"] == 0
            and not execution["timed_out"]
            and execution["execution_boundary"]
            == "systemd_user_transient_service"
            and bool(execution["systemd_unit"])
            and not execution["stdout_truncated"]
            and not execution["stderr_truncated"]
            and post_scan["scan_status"] == "clean"
            and candidate_repository_unchanged
        )
        base["evaluation_status"] = "passed" if passed else "failed"
        base["promotion_eligible"] = passed
        if not passed:
            resource_exhausted = (
                isinstance(resource_evidence, dict)
                and resource_evidence.get("outcome", {}).get(
                    "resource_exhausted"
                )
                is True
            )
            base["failure_reason"] = (
                "resource_limit_exhausted"
                if resource_exhausted
                else "post_run_canary_leak"
                if post_scan["scan_status"] != "clean"
                else "candidate_workspace_mutated_during_evaluation"
                if not candidate_repository_unchanged
                else "evaluator_timeout"
                if execution["timed_out"]
                else "evaluator_output_truncated"
                if (
                    execution["stdout_truncated"]
                    or execution["stderr_truncated"]
                )
                else "evaluator_nonzero_exit"
            )
    except ExperimentEvaluationBlocked as exc:
        base["failure_reason"] = str(exc)
        _finish_evidence(base, evidence_path)
        exc.evidence = base
        raise
    except ExperimentSandboxError as exc:
        base["failure_reason"] = str(exc)
        _finish_evidence(base, evidence_path)
        raise ExperimentEvaluationBlocked(str(exc), base) from exc
    _finish_evidence(
        base,
        evidence_path,
        authority_root=authority_root,
        invocation_set_reference=invocation_set_reference,
        experiment_protocol_reference=experiment_protocol_reference,
        provider_sandbox_reference=provider_sandbox_reference,
        canary_path=canary_path,
    )
    return base


def validate_evaluation_evidence(
    evidence,
    **validation_authority,
):
    return _validate_evaluation_evidence(
        evidence,
        **validation_authority,
    )


def _validate_historical_evaluation_evidence(
    evidence,
    *,
    sealed_result,
    cleanup_receipt,
    clean_snapshot_attestation,
    **validation_authority,
):
    historical_context = _validate_historical_cleanup_context(
        sealed_result,
        cleanup_receipt,
        clean_snapshot_attestation,
    )
    return _validate_evaluation_evidence(
        evidence,
        _historical_context=historical_context,
        **validation_authority,
    )


def _validate_evaluation_evidence(
    evidence,
    *,
    expected_run_id=None,
    expected_taskpack_ids=None,
    expected_protocol_sha256=None,
    expected_protocol_reference_sha256=None,
    expected_acceptance_command_sha256=None,
    expected_acceptance_executable_sha256=None,
    expected_evaluator_sha256=None,
    expected_invocation_set_reference_sha256=None,
    expected_provider_sandbox_reference_sha256=None,
    authority_root=None,
    invocation_set_reference=None,
    experiment_protocol_reference=None,
    provider_sandbox_reference=None,
    canary_path=None,
    _historical_context=None,
):
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "experiment_evaluation.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxError(
            "experiment evaluation schema is unavailable"
        ) from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(evidence),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise ExperimentSandboxError(
            f"evaluation evidence schema failed at {location}: {first.message}"
        )
    expected_bindings = {
        "run_id": expected_run_id,
        "experiment_protocol_sha256": expected_protocol_sha256,
        "experiment_protocol_reference_sha256": (
            expected_protocol_reference_sha256
        ),
        "acceptance_command_sha256": expected_acceptance_command_sha256,
        "acceptance_executable_sha256": (
            expected_acceptance_executable_sha256
        ),
        "evaluator_sha256": expected_evaluator_sha256,
        "invocation_set_reference_sha256": (
            expected_invocation_set_reference_sha256
        ),
        "provider_sandbox_reference_sha256": (
            expected_provider_sandbox_reference_sha256
        ),
    }
    for field, expected in expected_bindings.items():
        if expected is not None and evidence[field] != expected:
            raise ExperimentSandboxError(
                f"evaluation evidence {field} binding mismatch"
            )
    if (
        expected_taskpack_ids is not None
        and evidence["taskpack_ids"] != sorted(set(expected_taskpack_ids))
    ):
        raise ExperimentSandboxError(
            "evaluation evidence taskpack_ids binding mismatch"
        )
    if (authority_root is None) is not (invocation_set_reference is None):
        raise ExperimentSandboxError(
            "evaluation authority validation requires root and reference"
        )
    if experiment_protocol_reference is not None and authority_root is None:
        raise ExperimentSandboxError(
            "protocol authority validation requires an authority root"
        )
    if (provider_sandbox_reference is None) is not (canary_path is None):
        raise ExperimentSandboxError(
            "candidate sandbox validation requires reference and canary"
        )
    if provider_sandbox_reference is not None and authority_root is None:
        raise ExperimentSandboxError(
            "candidate sandbox validation requires an authority root"
        )
    if experiment_protocol_reference is not None:
        protocol = load_experiment_protocol_reference(
            experiment_protocol_reference,
            authority_root,
        )
        protocol_sha256 = hashlib.sha256(
            _canonical_json_bytes(protocol)
        ).hexdigest()
        command_sha256 = hashlib.sha256(
            _canonical_json_bytes(protocol["acceptance"]["command"])
        ).hexdigest()
        if (
            evidence["experiment_protocol_sha256"] != protocol_sha256
            or evidence["experiment_protocol_reference_sha256"]
            != experiment_protocol_reference["sha256"]
            or evidence["acceptance_command_sha256"] != command_sha256
            or evidence["evaluator_sha256"]
            != protocol["evaluator"]["artifact_sha256"]
        ):
            raise ExperimentSandboxError(
                "evaluation evidence protocol authority binding mismatch"
            )
    if authority_root is not None:
        manifest = _load_model_invocation_set_reference(
            invocation_set_reference,
            authority_root,
            historical_context=_historical_context,
        )
        authority_sets = []
        authority_terminals = []
        actual_canary_sha256 = None
        if canary_path is not None:
            actual_canary_sha256 = hashlib.sha256(
                _read_bounded_regular_file(
                    _existing_path(
                        canary_path,
                        "gold canary",
                        require_file=True,
                        reject_symlink=True,
                    ),
                    max_bytes=64 * 1024,
                )
            ).hexdigest()
        for item in manifest["invocation_sets"]:
            invocation_sandbox = _load_provider_sandbox_reference(
                item["sandbox_reference"],
                authority_root,
                historical_context=_historical_context,
            )
            if (
                actual_canary_sha256 is not None
                and invocation_sandbox["namespace_evidence"][
                    "canary_sha256"
                ]
                != actual_canary_sha256
            ):
                raise ExperimentSandboxError(
                    "model invocation sandbox canary binding mismatch"
                )
            terminals, seal_sha256 = _terminal_invocation_evidence(
                Path(item["lifecycle_authority_root"])
                / "model_invocations",
                expected_invocation_ids=item["invocation_ids"],
                expected_run_id=manifest["run_id"],
                expected_taskpack_id=item["taskpack_id"],
                expected_sandbox_policy_sha256=item[
                    "sandbox_policy_sha256"
                ],
                expected_sandbox_reference_sha256=item[
                    "sandbox_reference"
                ]["sha256"],
            )
            authority_terminals.extend(terminals)
            authority_sets.append(
                {
                    "taskpack_id": item["taskpack_id"],
                    "expected_invocation_ids": item["invocation_ids"],
                    "sandbox_policy_sha256": item[
                        "sandbox_policy_sha256"
                    ],
                    "sandbox_reference_sha256": item[
                        "sandbox_reference"
                    ]["sha256"],
                    "seal_sha256": seal_sha256,
                }
            )
        authority_sets = sorted(
            authority_sets,
            key=lambda item: (
                item["taskpack_id"],
                item["expected_invocation_ids"],
            ),
        )
        authority_terminals = sorted(
            authority_terminals,
            key=lambda item: item["invocation_id"],
        )
        if (
            evidence["run_id"] != manifest["run_id"]
            or evidence["invocation_set_reference_sha256"]
            != invocation_set_reference["sha256"]
            or evidence["invocation_sets"] != authority_sets
            or evidence["terminal_invocations"] != authority_terminals
        ):
            raise ExperimentSandboxError(
                "evaluation evidence invocation authority binding mismatch"
            )
    if provider_sandbox_reference is not None:
        candidate_sandbox = _load_provider_sandbox_reference(
            provider_sandbox_reference,
            authority_root,
            historical_context=_historical_context,
        )
        canary_sha256 = hashlib.sha256(
            _read_bounded_regular_file(
                _existing_path(
                    canary_path,
                    "gold canary",
                    require_file=True,
                    reject_symlink=True,
                ),
                max_bytes=64 * 1024,
            )
        ).hexdigest()
        pre_scan = evidence["pre_run_leak_scan"]
        post_scan = evidence["post_run_leak_scan"]
        if (
            evidence["provider_sandbox_reference_sha256"]
            != provider_sandbox_reference["sha256"]
            or evidence["provider_sandbox_policy_sha256"]
            != candidate_sandbox["policy_sha256"]
            or candidate_sandbox["namespace_evidence"]["canary_sha256"]
            != canary_sha256
            or (
                isinstance(pre_scan, dict)
                and pre_scan["canary_sha256"] != canary_sha256
            )
            or (
                isinstance(post_scan, dict)
                and post_scan["canary_sha256"] != canary_sha256
            )
        ):
            raise ExperimentSandboxError(
                "evaluation evidence candidate sandbox binding mismatch"
            )
    for stream in ("stdout", "stderr"):
        digest = hashlib.sha256(
            evidence[stream].encode("utf-8")
        ).hexdigest()
        if evidence[f"{stream}_sha256"] != digest:
            raise ExperimentSandboxError(
                f"evaluation evidence {stream} digest mismatch"
            )
    pre_scan = evidence["pre_run_leak_scan"]
    post_scan = evidence["post_run_leak_scan"]
    passed = evidence["evaluation_status"] == "passed"
    invocation_sets = evidence["invocation_sets"]
    expected_pairs = {
        (
            invocation_id,
            item["taskpack_id"],
            item["sandbox_policy_sha256"],
            item["sandbox_reference_sha256"],
        )
        for item in invocation_sets
        for invocation_id in item["expected_invocation_ids"]
    }
    terminal_pairs = {
        (
            item["invocation_id"],
            item["taskpack_id"],
            item["sandbox_policy_sha256"],
            item["sandbox_reference_sha256"],
        )
        for item in evidence["terminal_invocations"]
    }
    invocation_relation_valid = (
        len(expected_pairs)
        == sum(
            len(item["expected_invocation_ids"])
            for item in invocation_sets
        )
        and sorted(
            invocation_id for invocation_id, _, _, _ in expected_pairs
        )
        == evidence["expected_invocation_ids"]
        and sorted(
            {taskpack_id for _, taskpack_id, _, _ in expected_pairs}
        )
        == evidence["taskpack_ids"]
        and expected_pairs == terminal_pairs
        and evidence["invocation_set_seal_sha256"]
        == hashlib.sha256(
            _canonical_json_bytes(invocation_sets)
        ).hexdigest()
    )
    if evidence["promotion_eligible"] is not passed:
        raise ExperimentSandboxError(
            "evaluation promotion eligibility is inconsistent"
        )
    if passed and (
        provider_sandbox_reference is None
        or canary_path is None
        or not evidence["evaluator_started"]
        or evidence["returncode"] != 0
        or evidence["timed_out"]
        or evidence["execution_boundary"]
        != "systemd_user_transient_service"
        or not evidence["systemd_unit"]
        or evidence["stdout_truncated"]
        or evidence["stderr_truncated"]
        or not evidence["terminal_invocations"]
        or not invocation_relation_valid
        or evidence["run_id"] == "<unresolved>"
        or not isinstance(evidence["candidate_repository"], dict)
        or not isinstance(pre_scan, dict)
        or pre_scan["scan_status"] != "clean"
        or not isinstance(post_scan, dict)
        or post_scan["scan_status"] != "clean"
        or evidence["failure_reason"] is not None
        or any(
            invocation["terminal_status"] != "completed"
            for invocation in evidence["terminal_invocations"]
        )
    ):
        raise ExperimentSandboxError(
            "passing evaluation evidence lacks required proof"
        )
    for scan in (pre_scan, post_scan):
        if isinstance(scan, dict):
            if scan["scan_scope_sha256"] != evidence["scan_scope_sha256"]:
                raise ExperimentSandboxError(
                    "evaluation leak scan scope is inconsistent"
                )
            has_findings = bool(scan["findings"])
            if (scan["scan_status"] == "leak_detected") is not has_findings:
                raise ExperimentSandboxError(
                    "evaluation leak scan status is inconsistent"
                )
    return evidence


def _finish_evidence(evidence, evidence_path, **validation_authority):
    evidence["finished_at"] = _utc_timestamp()
    validate_evaluation_evidence(evidence, **validation_authority)
    if evidence_path is not None:
        _publish_immutable_json(evidence_path, evidence)


def _terminal_invocation_evidence(
    invocation_root,
    *,
    expected_invocation_ids,
    expected_run_id,
    expected_taskpack_id,
    expected_sandbox_policy_sha256,
    expected_sandbox_reference_sha256,
):
    root = _existing_path(
        invocation_root,
        "model invocation root",
        require_directory=True,
        reject_symlink=True,
    )
    if (
        not isinstance(expected_invocation_ids, (list, tuple))
        or not expected_invocation_ids
        or not all(
            isinstance(invocation_id, str) and invocation_id
            for invocation_id in expected_invocation_ids
        )
        or len(set(expected_invocation_ids)) != len(expected_invocation_ids)
    ):
        raise ExperimentEvaluationBlocked(
            "trusted evaluation requires a bounded expected invocation set"
        )
    if (
        not _is_sha256(expected_sandbox_policy_sha256)
        or not _is_sha256(expected_sandbox_reference_sha256)
    ):
        raise ExperimentEvaluationBlocked(
            "trusted evaluation requires a sandbox reference identity"
        )
    if (
        not isinstance(expected_run_id, str)
        or not expected_run_id
        or not isinstance(expected_taskpack_id, str)
        or not expected_taskpack_id
    ):
        raise ExperimentEvaluationBlocked(
            "trusted evaluation requires run and taskpack identities"
        )
    invocation_dirs = sorted(root.iterdir())
    actual_ids = [path.name for path in invocation_dirs]
    if set(actual_ids) != set(expected_invocation_ids):
        raise ExperimentEvaluationBlocked(
            "model invocation set does not match the sealed expected set"
        )
    evidence = []
    for invocation_dir in invocation_dirs:
        if invocation_dir.is_symlink() or not invocation_dir.is_dir():
            raise ExperimentEvaluationBlocked(
                "model invocation root contains an unsafe entry"
            )
        start_path = invocation_dir / "started.json"
        if start_path.is_symlink() or not start_path.is_file():
            raise ExperimentEvaluationBlocked(
                "all model invocations require a durable start before evaluation"
            )
        terminal_path = start_path.with_name("terminal.json")
        if terminal_path.is_symlink() or not terminal_path.is_file():
            raise ExperimentEvaluationBlocked(
                "all model invocations must terminate before evaluation"
            )
        try:
            start = json.loads(
                _read_bounded_regular_file(
                    start_path,
                    max_bytes=4 * 1024 * 1024,
                ).decode("utf-8")
            )
            terminal = json.loads(
                _read_bounded_regular_file(
                    terminal_path,
                    max_bytes=4 * 1024 * 1024,
                ).decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentEvaluationBlocked(
                "model invocation lifecycle evidence is unreadable"
            ) from exc
        _validate_schema_record(
            start,
            "model_invocation_started.schema.json",
            "model invocation start",
        )
        _validate_schema_record(
            terminal,
            "model_invocation_usage.schema.json",
            "model invocation terminal",
        )
        invocation_id = start.get("invocation_id")
        terminal_status = terminal.get("terminal_status")
        sandbox_policy_sha256 = start.get(
            "experiment_sandbox_policy_sha256"
        )
        sandbox_reference_sha256 = start.get(
            "experiment_sandbox_reference_sha256"
        )
        start_sha256 = hashlib.sha256(
            _read_bounded_regular_file(
                start_path,
                max_bytes=4 * 1024 * 1024,
            )
        ).hexdigest()
        shared_lifecycle_fields = (
            "invocation_id",
            "project",
            "run_id",
            "pursue_id",
            "round_index",
            "taskpack_id",
            "implementation_run_id",
            "gate_epoch",
            "task_id",
            "attempt_id",
            "runtime_execution_session_id",
            "provider_predecessor_invocation_id",
            "provider_predecessor_turn_id",
            "lifecycle_owner_token",
            "agent_id",
            "role",
            "usage_stage",
            "backend",
            "model",
            "coverage_class",
            "experiment_sandbox_policy_sha256",
            "experiment_sandbox_reference_sha256",
            "started_at",
        )
        if (
            not isinstance(invocation_id, str)
            or terminal.get("invocation_id") != invocation_id
            or invocation_dir.name != invocation_id
            or start.get("run_id") != expected_run_id
            or terminal.get("run_id") != expected_run_id
            or start.get("taskpack_id") != expected_taskpack_id
            or terminal.get("taskpack_id") != expected_taskpack_id
            or terminal.get("experiment_sandbox_policy_sha256")
            != expected_sandbox_policy_sha256
            or terminal.get("experiment_sandbox_reference_sha256")
            != expected_sandbox_reference_sha256
            or terminal.get("start_sha256") != start_sha256
            or any(
                terminal.get(field) != start.get(field)
                for field in shared_lifecycle_fields
            )
            or terminal_status
            not in {
                "completed",
                "failed",
                "blocked",
                "cancelled",
                "timed_out",
                "launch_failed",
                "missing_result",
                "invalid_result",
                "recovered_orphan",
            }
            or sandbox_policy_sha256 != expected_sandbox_policy_sha256
            or sandbox_reference_sha256
            != expected_sandbox_reference_sha256
        ):
            raise ExperimentEvaluationBlocked(
                "model invocation terminal record does not bind its start and sandbox"
            )
        evidence.append(
            {
                "invocation_id": invocation_id,
                "taskpack_id": expected_taskpack_id,
                "terminal_status": terminal_status,
                "start_sha256": start_sha256,
                "sandbox_policy_sha256": sandbox_policy_sha256,
                "sandbox_reference_sha256": sandbox_reference_sha256,
                "terminal_sha256": hashlib.sha256(
                    _read_bounded_regular_file(
                        terminal_path,
                        max_bytes=4 * 1024 * 1024,
                    )
                ).hexdigest(),
            }
        )
    seal_sha256 = _seal_model_invocation_set(
        root,
        expected_invocation_ids=expected_invocation_ids,
        expected_run_id=expected_run_id,
        expected_taskpack_id=expected_taskpack_id,
        expected_sandbox_policy_sha256=expected_sandbox_policy_sha256,
        expected_sandbox_reference_sha256=(
            expected_sandbox_reference_sha256
        ),
    )
    return evidence, seal_sha256


def _seal_model_invocation_set(
    invocation_root,
    *,
    expected_invocation_ids,
    expected_run_id,
    expected_taskpack_id,
    expected_sandbox_policy_sha256,
    expected_sandbox_reference_sha256,
):
    authority_root = Path(invocation_root).parent
    lock_path = authority_root / "model_invocations.lock"
    seal_path = authority_root / "model_invocations.sealed.json"
    seal = {
        "schema_version": "experiment_model_invocation_set.v1",
        "expected_invocation_ids": sorted(expected_invocation_ids),
        "run_id": expected_run_id,
        "taskpack_id": expected_taskpack_id,
        "sandbox_policy_sha256": expected_sandbox_policy_sha256,
        "sandbox_reference_sha256": expected_sandbox_reference_sha256,
    }
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                invocation_dirs = sorted(invocation_root.iterdir())
            except OSError as exc:
                raise ExperimentEvaluationBlocked(
                    "model invocation set changed before sealing"
                ) from exc
            if (
                [path.name for path in invocation_dirs]
                != sorted(expected_invocation_ids)
                or any(
                    path.is_symlink()
                    or not path.is_dir()
                    or not (path / "started.json").is_file()
                    or (path / "started.json").is_symlink()
                    or not (path / "terminal.json").is_file()
                    or (path / "terminal.json").is_symlink()
                    for path in invocation_dirs
                )
            ):
                raise ExperimentEvaluationBlocked(
                    "model invocation set changed before sealing"
                )
            if seal_path.exists():
                try:
                    existing = json.loads(
                        _read_bounded_regular_file(
                            seal_path,
                            max_bytes=64 * 1024,
                        ).decode("utf-8")
                    )
                except (
                    OSError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as exc:
                    raise ExperimentEvaluationBlocked(
                        "model invocation set seal is unreadable"
                    ) from exc
                if existing != seal:
                    raise ExperimentEvaluationBlocked(
                        "model invocation set seal conflicts with evaluation"
                    )
            else:
                _publish_immutable_json(seal_path, seal)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return hashlib.sha256(
        _read_bounded_regular_file(seal_path, max_bytes=64 * 1024)
    ).hexdigest()


def _run_bounded_argv(
    argv,
    *,
    cwd,
    environment,
    timeout_seconds,
    max_output_bytes,
    cpu_limit,
    memory_limit_bytes,
    input_bytes=None,
    prelaunch_source_authority=None,
    resource_envelope_binding=None,
    resource_mode=None,
    resource_run_id=None,
    resource_hierarchy_reference=None,
    resource_hierarchy_factory=None,
):
    systemd_run = Path("/usr/bin/systemd-run")
    systemctl = Path("/usr/bin/systemctl")
    env_binary = _TRUSTED_ENV_PATH
    if (
        not sys.platform.startswith("linux")
        or not systemd_run.is_file()
        or not systemctl.is_file()
        or not env_binary.is_file()
    ):
        raise ExperimentSandboxUnavailable(
            "systemd user transient services are unavailable"
        )
    systemd_run = _existing_path(
        systemd_run,
        "systemd-run",
        require_file=True,
    )
    systemctl = _existing_path(
        systemctl,
        "systemctl",
        require_file=True,
    )
    env_binary = _existing_path(
        env_binary,
        "env",
        require_file=True,
    )
    try:
        cpu_limit = int(cpu_limit)
        memory_limit_bytes = int(memory_limit_bytes)
    except (TypeError, ValueError) as exc:
        raise ExperimentSandboxError(
            "evaluator resource limits are invalid"
        ) from exc
    if not 1 <= cpu_limit <= 64 or not 64 * 1024 * 1024 <= memory_limit_bytes:
        raise ExperimentSandboxError("evaluator resource limits are invalid")
    guarded_argv = list(argv)
    source_guard_path = None
    if prelaunch_source_authority is not None:
        if not isinstance(prelaunch_source_authority, dict):
            raise ExperimentSandboxError(
                "evaluator source authority is invalid"
            )
        authority_bytes = _canonical_json_bytes(
            prelaunch_source_authority
        )
        if len(authority_bytes) > 1024 * 1024:
            raise ExperimentSandboxError(
                "evaluator source authority exceeds its bound"
            )
        guard_root = Path(
            f"/tmp/agentteam-source-guards-{os.getuid()}"
        )
        guard_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        guard_metadata = guard_root.lstat()
        if (
            not stat.S_ISDIR(guard_metadata.st_mode)
            or guard_metadata.st_uid != os.getuid()
            or stat.S_IMODE(guard_metadata.st_mode) != 0o700
        ):
            raise ExperimentSandboxError(
                "evaluator source guard directory is unsafe"
            )
        descriptor, source_guard_path = tempfile.mkstemp(
            prefix="authority-",
            suffix=".json",
            dir=guard_root,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(authority_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            Path(source_guard_path).unlink(missing_ok=True)
            raise
        guarded_argv = [
            sys.executable,
            "-B",
            str(Path(__file__).with_name("model_invocation.py")),
            "_source_guard",
            source_guard_path,
            hashlib.sha256(authority_bytes).hexdigest(),
            repr(float(timeout_seconds)),
            "--",
            *guarded_argv,
        ]
    unit = f"agentteam-eval-{os.urandom(12).hex()}.service"
    resource_hierarchy = None
    resource_arguments = []
    if resource_envelope_binding is not None:
        from .resource_envelope import SystemdResourceHierarchy

        hierarchy_factory = (
            resource_hierarchy_factory or SystemdResourceHierarchy
        )
        resource_hierarchy = hierarchy_factory(
            resource_envelope_binding,
            run_id=resource_run_id,
            mode=resource_mode,
            owner_reference=resource_hierarchy_reference,
        )
        resource_hierarchy.prepare()
        resource_arguments = resource_hierarchy.leaf_arguments(
            evaluator=True
        )
    legacy_resource_arguments = []
    if resource_hierarchy is None:
        legacy_resource_arguments = [
            "--property",
            "TasksMax=256",
            "--property",
            f"MemoryMax={memory_limit_bytes}",
            "--property",
            f"CPUQuota={cpu_limit * 100}%",
        ]
    command = [
        str(systemd_run),
        "--user",
        "--quiet",
        "--wait",
        *([] if resource_hierarchy is not None else ["--collect"]),
        "--pipe",
        "--service-type=exec",
        "--unit",
        unit,
        "--property",
        "KillMode=control-group",
        *resource_arguments,
        "--property",
        "ExitType=main",
        "--property",
        "TimeoutStopSec=5s",
        *legacy_resource_arguments,
        "--working-directory",
        str(cwd),
    ]
    if (
        prelaunch_source_authority is None
        and Path(argv[0]).resolve(strict=False) == _TRUSTED_BWRAP_PATH
    ):
        bounded_argv = list(argv)
    else:
        bounded_argv = [
            str(env_binary),
            "-i",
            *(f"{name}={value}" for name, value in sorted(environment.items())),
            *guarded_argv,
        ]
    if resource_hierarchy is not None:
        from .resource_envelope import _resource_wrapped_command

        bounded_argv = _resource_wrapped_command(bounded_argv, unit=unit)
    command.extend(["--", *bounded_argv])

    resource_monitor = None
    if resource_hierarchy is not None:
        from .resource_envelope import ResourceUnitMonitor

        resource_monitor = ResourceUnitMonitor(
            resource_hierarchy,
            unit,
            scope="evaluator",
            evaluator=True,
        ).start()

    def terminate_unit():
        subprocess.run(
            [
                str(systemctl),
                "--user",
                "kill",
                "--kill-whom=all",
                "--signal=SIGKILL",
                unit,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )

    try:
        try:
            result = _capture_bounded_process(
                command,
                cwd="/",
                environment=dict(os.environ),
                timeout_seconds=(
                    timeout_seconds
                    + PRELAUNCH_SOURCE_REVALIDATION_TIMEOUT_SECONDS
                    if prelaunch_source_authority is not None
                    else timeout_seconds
                ),
                max_output_bytes=max_output_bytes,
                timeout_callback=terminate_unit,
                input_bytes=input_bytes,
            )
        finally:
            if source_guard_path is not None:
                Path(source_guard_path).unlink(missing_ok=True)
    except Exception:
        if resource_monitor is not None:
            resource_monitor.cancel()
        if resource_hierarchy is not None:
            terminate_unit()
            resource_hierarchy.cleanup()
        raise
    resource_evidence = None
    if resource_hierarchy is not None:
        try:
            resource_evidence = resource_monitor.finish(
                binding=resource_envelope_binding,
                timed_out=result["timed_out"],
            )
        finally:
            subprocess.run(
                [str(systemctl), "--user", "stop", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            cleanup = resource_hierarchy.cleanup()
        resource_evidence["cleanup"] = cleanup
    result.update(
        {
            "execution_boundary": "systemd_user_transient_service",
            "systemd_unit": unit,
            "resource_evidence": resource_evidence,
        }
    )
    return result


def _capture_bounded_process(
    argv,
    *,
    cwd,
    environment,
    timeout_seconds,
    max_output_bytes,
    timeout_callback=None,
    input_bytes=None,
):
    if not 1 <= max_output_bytes <= 16 * 1024 * 1024:
        raise ExperimentSandboxError("evaluator output bound is invalid")
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=environment,
            stdin=(
                subprocess.PIPE
                if input_bytes is not None
                else subprocess.DEVNULL
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise ExperimentSandboxError(
            f"trusted evaluator launch failed: {type(exc).__name__}"
        ) from exc
    output = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream, target, index):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            remaining = max_output_bytes - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[index] = True

    threads = [
        threading.Thread(
            target=drain,
            args=(process.stdout, output[0], 0),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, output[1], 1),
            daemon=True,
        ),
    ]
    if input_bytes is not None:
        if (
            not isinstance(input_bytes, bytes)
            or len(input_bytes) > 4 * 1024 * 1024
        ):
            process.kill()
            process.wait()
            raise ExperimentSandboxError(
                "evaluator input is not bounded bytes"
            )

        def feed_input():
            try:
                process.stdin.write(input_bytes)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        threads.append(threading.Thread(target=feed_input, daemon=True))
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if timeout_callback is not None:
            timeout_callback()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    for thread in threads:
        thread.join(timeout=2)
    process.stdout.close()
    process.stderr.close()
    stdout_bytes = bytes(output[0])
    stderr_bytes = bytes(output[1])
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_truncated": truncated[0],
        "stderr_truncated": truncated[1],
    }


def _scan_output_bytes(stdout, stderr, canary_path):
    canary_path = Path(canary_path)
    canary = _read_bounded_regular_file(canary_path, max_bytes=64 * 1024)
    digest = hashlib.sha256(canary).hexdigest()
    needles = (
        ("canary_content", canary),
        ("canary_sha256", digest.encode("ascii")),
        ("canary_sha256", digest.upper().encode("ascii")),
        ("canary_path", str(canary_path).encode("utf-8")),
    )
    findings = []
    for stream, encoded in (("stdout", stdout), ("stderr", stderr)):
        for kind, needle in needles:
            if needle and needle in encoded:
                findings.append(
                    {
                        "group": "evaluator_output",
                        "path_sha256": hashlib.sha256(
                            stream.encode("ascii")
                        ).hexdigest(),
                        "match": kind,
                    }
                )
    return findings


def _contains_canary_reference(text, canary_path):
    canary_path = Path(canary_path)
    canary = _read_bounded_regular_file(canary_path, max_bytes=64 * 1024)
    digest = hashlib.sha256(canary).hexdigest()
    encoded = str(text).encode("utf-8")
    return any(
        needle and needle in encoded
        for needle in (
            canary,
            digest.encode("ascii"),
            digest.upper().encode("ascii"),
            str(canary_path).encode("utf-8"),
        )
    )


def scan_scope_sha256(scan_groups):
    scope = _normalized_scan_scope(scan_groups)
    return hashlib.sha256(_canonical_json_bytes(scope)).hexdigest()


def publish_scan_scope_reference(
    authority_root,
    scan_groups,
    *,
    reference_id="evaluation-scan-scope",
):
    scope = _normalized_scan_scope(scan_groups)
    scope_sha256 = hashlib.sha256(_canonical_json_bytes(scope)).hexdigest()
    record = {
        "schema_version": "experiment_scan_scope.v1",
        "scan_groups": scope,
        "scan_scope_sha256": scope_sha256,
    }
    authority_dir = _experiment_authority_dir(authority_root)
    path = authority_dir / f"{_safe_reference_id(reference_id)}.scan-scope.json"
    _publish_immutable_json(path, record)
    payload = _read_bounded_regular_file(path, max_bytes=4 * 1024 * 1024)
    return {
        "schema_version": SCAN_SCOPE_REFERENCE_SCHEMA_VERSION,
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def load_scan_scope_reference(reference, authority_root):
    _path, payload = _load_authority_reference(
        reference,
        authority_root,
        schema_version=SCAN_SCOPE_REFERENCE_SCHEMA_VERSION,
        suffix=".scan-scope.json",
    )
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxUnavailable(
            "leak scan scope authority is unreadable"
        ) from exc
    if (
        not isinstance(record, dict)
        or set(record)
        != {"schema_version", "scan_groups", "scan_scope_sha256"}
        or record["schema_version"] != "experiment_scan_scope.v1"
    ):
        raise ExperimentSandboxError("invalid leak scan scope authority")
    scope = _normalized_scan_scope(record["scan_groups"])
    digest = hashlib.sha256(_canonical_json_bytes(scope)).hexdigest()
    if record["scan_groups"] != scope or record["scan_scope_sha256"] != digest:
        raise ExperimentSandboxError("leak scan scope authority drift")
    return scope, digest


def _normalized_scan_scope(scan_groups):
    if not isinstance(scan_groups, dict) or set(scan_groups) != {
        "prompt",
        "context",
        "taskpack",
        "artifacts",
    }:
        raise ExperimentSandboxError(
            "leak scan groups must be prompt, context, taskpack, and artifacts"
        )
    scope = {}
    for group in ("prompt", "context", "taskpack", "artifacts"):
        values = scan_groups[group]
        if isinstance(values, (str, os.PathLike)):
            values = [values]
        if not isinstance(values, (list, tuple)) or not values:
            raise ExperimentSandboxError(
                "every leak scan group must declare at least one path"
            )
        scope[group] = sorted(
            str(
                _existing_path(
                    value,
                    f"{group} leak scan path",
                    reject_symlink=True,
                )
            )
            for value in values
        )
    return scope


def _normalize_invocation_sets(
    invocation_sets,
    *,
    authority_root,
    historical_context=None,
):
    if (
        not isinstance(invocation_sets, (list, tuple))
        or not invocation_sets
        or len(invocation_sets) > 1024
    ):
        raise ExperimentSandboxError(
            "model invocation manifest requires bounded invocation sets"
        )
    normalized = []
    seen_roots = set()
    seen_invocation_ids = set()
    for item in invocation_sets:
        required_keys = {
            "lifecycle_authority_root",
            "taskpack_id",
            "invocation_ids",
            "sandbox_reference",
        }
        if (
            not isinstance(item, dict)
            or set(item) not in (
                required_keys,
                required_keys | {"sandbox_policy_sha256"},
            )
        ):
            raise ExperimentSandboxError(
                "invalid model invocation set entry"
            )
        lifecycle_authority_root = validate_experiment_lifecycle_authority(
            authority_root,
            item["lifecycle_authority_root"],
        )
        root = _existing_path(
            lifecycle_authority_root / "model_invocations",
            "canonical model invocation root",
            require_directory=True,
            reject_symlink=True,
        )
        root_key = str(root)
        if root_key in seen_roots:
            raise ExperimentSandboxError(
                "model invocation roots must be unique"
            )
        seen_roots.add(root_key)
        taskpack_id = _nonempty_text(
            item["taskpack_id"],
            "model invocation taskpack id",
        )
        sandbox_reference = item["sandbox_reference"]
        sandbox_descriptor = _load_provider_sandbox_reference(
            sandbox_reference,
            authority_root,
            historical_context=historical_context,
        )
        validate_provider_authority_separation(
            sandbox_descriptor,
            authority_root,
            lifecycle_authority_root,
        )
        sandbox_policy_sha256 = sandbox_descriptor["policy_sha256"]
        if (
            "sandbox_policy_sha256" in item
            and item["sandbox_policy_sha256"] != sandbox_policy_sha256
        ):
            raise ExperimentSandboxError(
                "model invocation sandbox policy does not match its reference"
            )
        invocation_ids = item["invocation_ids"]
        if (
            not isinstance(invocation_ids, (list, tuple))
            or not invocation_ids
            or len(invocation_ids) > 4096
            or not all(
                isinstance(invocation_id, str)
                and invocation_id.startswith("INV-")
                and len(invocation_id) <= 256
                for invocation_id in invocation_ids
            )
            or len(set(invocation_ids)) != len(invocation_ids)
        ):
            raise ExperimentSandboxError(
                "model invocation ids must be bounded and unique"
            )
        duplicate_ids = seen_invocation_ids.intersection(invocation_ids)
        if duplicate_ids:
            raise ExperimentSandboxError(
                "model invocation ids must be globally unique"
            )
        seen_invocation_ids.update(invocation_ids)
        normalized.append(
            {
                "lifecycle_authority_root": str(
                    lifecycle_authority_root
                ),
                "taskpack_id": taskpack_id,
                "invocation_ids": sorted(invocation_ids),
                "sandbox_reference": dict(sandbox_reference),
                "sandbox_policy_sha256": sandbox_policy_sha256,
            }
        )
    try:
        actual_roots = {
            str(_existing_path(
                entry.path,
                "registered model invocation lifecycle authority",
                require_directory=True,
                reject_symlink=True,
            ))
            for entry in os.scandir(
                _experiment_lifecycle_registry_dir(authority_root)
            )
        }
    except OSError as exc:
        raise ExperimentSandboxError(
            "model invocation lifecycle registry is unavailable"
        ) from exc
    declared_roots = {
        item["lifecycle_authority_root"]
        for item in normalized
    }
    registered_roots = _registered_lifecycle_roots(authority_root)
    if (
        declared_roots != registered_roots
        or actual_roots != registered_roots
    ):
        raise ExperimentSandboxError(
            "model invocation manifest does not cover the lifecycle registry"
        )
    return sorted(
        normalized,
        key=lambda item: (
            item["taskpack_id"],
            item["sandbox_reference"]["sha256"],
            item["sandbox_policy_sha256"],
            item["lifecycle_authority_root"],
            item["invocation_ids"],
        ),
    )


def _experiment_authority_dir(authority_root):
    root = Path(authority_root).resolve(strict=True)
    path = root / "experiment_authority"
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExperimentSandboxError(
            "experiment authority directory permissions are unsafe"
        )
    return path


def _experiment_lifecycle_registry_dir(authority_root):
    root = Path(authority_root).resolve(strict=True)
    path = root / "experiment_lifecycles"
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExperimentSandboxError(
            "experiment lifecycle registry permissions are unsafe"
        )
    return path


@contextmanager
def _experiment_lifecycle_registry_lock(authority_root):
    path = _experiment_authority_dir(authority_root) / (
        "lifecycle-registry.lock"
    )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ExperimentSandboxError(
            "experiment lifecycle registry lock is unavailable"
        ) from exc
    lock_file = None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ExperimentSandboxError(
                "experiment lifecycle registry lock is unsafe"
            )
        lock_file = os.fdopen(descriptor, "r+b")
        descriptor = -1
        yield lock_file
    finally:
        if lock_file is not None:
            lock_file.close()
        elif descriptor >= 0:
            os.close(descriptor)


def _experiment_lifecycle_registry_seal_path(authority_root):
    return _experiment_authority_dir(authority_root) / (
        "model-invocation-registry.sealed.json"
    )


def _validate_experiment_lifecycle_registry_seal(
    authority_root,
    invocation_set_reference,
    invocation_sets,
    *,
    expected_run_id,
):
    seal_path = _experiment_lifecycle_registry_seal_path(authority_root)
    seal = _read_json_object(
        seal_path,
        "experiment lifecycle registry seal",
    )
    expected_roots = sorted(
        item["lifecycle_authority_root"] for item in invocation_sets
    )
    expected = {
        "schema_version": LIFECYCLE_REGISTRY_SEAL_SCHEMA_VERSION,
        "run_id": expected_run_id,
        "invocation_set_reference": dict(invocation_set_reference),
        "lifecycle_authority_roots": expected_roots,
    }
    if seal != expected:
        raise ExperimentSandboxError(
            "experiment lifecycle registry seal is invalid"
        )
    metadata = seal_path.stat()
    if (
        metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or set(expected_roots) != _registered_lifecycle_roots(
            authority_root
        )
    ):
        raise ExperimentSandboxError(
            "experiment lifecycle registry seal is unsafe"
        )
    return seal


def _registered_lifecycle_roots(authority_root):
    authority_dir = _experiment_authority_dir(authority_root)
    registrations = {}
    try:
        entries = list(os.scandir(authority_dir))
    except OSError as exc:
        raise ExperimentSandboxError(
            "experiment lifecycle registration ledger is unavailable"
        ) from exc
    for entry in entries:
        if not entry.name.endswith(".lifecycle-registration.json"):
            continue
        path = _existing_path(
            entry.path,
            "experiment lifecycle registration",
            require_file=True,
            reject_symlink=True,
        )
        try:
            record = json.loads(
                _read_bounded_regular_file(
                    path,
                    max_bytes=64 * 1024,
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperimentSandboxError(
                "experiment lifecycle registration is unreadable"
            ) from exc
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "schema_version",
                "lifecycle_id",
                "lifecycle_authority_root",
            }
            or record["schema_version"]
            != "experiment_lifecycle_registration.v1"
            or path.name
            != f"{_safe_reference_id(record['lifecycle_id'])}."
            "lifecycle-registration.json"
        ):
            raise ExperimentSandboxError(
                "experiment lifecycle registration is invalid"
            )
        root = Path(record["lifecycle_authority_root"])
        if (
            root.parent != _experiment_lifecycle_registry_dir(authority_root)
            or root.name != record["lifecycle_id"]
            or str(root) in registrations
        ):
            raise ExperimentSandboxError(
                "experiment lifecycle registration is invalid"
            )
        registrations[str(root)] = record["lifecycle_id"]
    return set(registrations)


def _read_json_object(path, label):
    path = _existing_path(
        path,
        label,
        require_file=True,
        reject_symlink=True,
    )
    try:
        value = json.loads(
            _read_bounded_regular_file(
                path,
                max_bytes=4 * 1024 * 1024,
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentSandboxError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ExperimentSandboxError(f"{label} must be an object")
    return value


def _safe_reference_id(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        raise ExperimentSandboxError("invalid experiment authority reference id")
    return value


def _require_sha256(value, label):
    if not _is_sha256(value):
        raise ExperimentSandboxError(f"{label} is invalid")
    return value


def _experiment_mode(value):
    if value not in {
        "single_codex",
        "agentteam_direct",
        "agentteam_full",
    }:
        raise ExperimentSandboxError(
            "experiment launch mode is invalid"
        )
    return value


def _normalize_experiment_model_policy(value):
    base_fields = {
        "backend",
        "codex_cli_version",
        "model",
        "reasoning_profile",
        "service_configuration_sha256",
        "sandbox_policy",
        "permission_policy",
        "network_policy",
        "tool_allowlist",
        "max_inflight_model_invocations",
    }
    if not isinstance(value, dict) or set(value) not in (
        base_fields,
        base_fields | LEGACY_CONTEXT_POLICY_FIELDS,
        base_fields | CONTEXT_BUDGET_POLICY_FIELDS,
    ):
        raise ExperimentSandboxError(
            "experiment model policy fields are invalid"
        )
    if value["backend"] != "codex":
        raise ExperimentSandboxError(
            "experiment model policy backend is invalid"
        )
    normalized = dict(value)
    for field in (
        "codex_cli_version",
        "model",
        "reasoning_profile",
        "sandbox_policy",
        "permission_policy",
        "network_policy",
    ):
        normalized[field] = _nonempty_text(
            value[field],
            f"experiment model policy {field}",
        )
    normalized["service_configuration_sha256"] = _require_sha256(
        value["service_configuration_sha256"],
        "experiment model policy service configuration digest",
    )
    tools = value["tool_allowlist"]
    if (
        not isinstance(tools, list)
        or not tools
        or len(tools) > 128
        or not all(isinstance(item, str) and item for item in tools)
        or len(set(tools)) != len(tools)
    ):
        raise ExperimentSandboxError(
            "experiment model policy tool allowlist is invalid"
        )
    normalized["tool_allowlist"] = list(tools)
    try:
        normalized.update(normalize_context_budget_policy(value))
    except ValueError as exc:
        raise ExperimentSandboxError(
            f"experiment model context policy is invalid: {exc}"
        ) from exc
    if value["max_inflight_model_invocations"] != 1:
        raise ExperimentSandboxError(
            "experiment model policy must use one provider lane"
        )
    return normalized


def _load_authority_reference(
    reference,
    authority_root,
    *,
    schema_version,
    suffix,
):
    if (
        not isinstance(reference, dict)
        or set(reference) != {"schema_version", "path", "sha256"}
        or reference.get("schema_version") != schema_version
        or not _is_sha256(reference.get("sha256"))
    ):
        raise ExperimentSandboxError("invalid experiment authority reference")
    authority_dir = _experiment_authority_dir(authority_root)
    path = _existing_path(
        reference["path"],
        "experiment authority reference",
        require_file=True,
        reject_symlink=True,
    )
    if path.parent != authority_dir or not path.name.endswith(suffix):
        raise ExperimentSandboxError(
            "experiment authority reference escapes the run authority"
        )
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExperimentSandboxError(
            "experiment authority reference permissions are unsafe"
        )
    payload = _read_bounded_regular_file(path, max_bytes=4 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
        raise ExperimentSandboxError("experiment authority reference digest mismatch")
    return path, payload


def _validate_descriptor_excludes_canary(descriptor, canary_path):
    canary_path = _existing_path(
        canary_path,
        "gold canary",
        require_file=True,
        reject_symlink=True,
    )
    for view in (
        descriptor["repository"],
        *descriptor["runtime_views"],
        *descriptor["library_views"],
        *descriptor["credential_views"],
    ):
        if _paths_overlap(Path(view["source"]), canary_path):
            raise ExperimentSandboxError(
                "gold canary overlaps a provider mount source"
            )
    markers = _forbidden_markers([canary_path])
    for value in descriptor["environment"].values():
        encoded = value.encode("utf-8")
        if any(marker and marker in encoded for marker in markers):
            raise ExperimentSandboxError(
                "evaluator-only material appears in provider environment"
            )


def _forbidden_markers(paths):
    markers = []
    for path in paths:
        path = Path(path)
        markers.append(str(path).encode("utf-8"))
        if not path.is_file():
            continue
        content = _read_bounded_regular_file(path, max_bytes=64 * 1024)
        markers.extend(
            (
                content,
                hashlib.sha256(content).hexdigest().encode("ascii"),
                hashlib.sha256(content).hexdigest().upper().encode("ascii"),
            )
        )
    return tuple(markers)


def _normalize_views(views, label):
    if not isinstance(views, (list, tuple)):
        raise ExperimentSandboxError(f"{label} views must be a list")
    if len(views) > 64:
        raise ExperimentSandboxError(f"{label} views exceed their bound")
    normalized = []
    for index, view in enumerate(views):
        if isinstance(view, (str, os.PathLike)):
            source = _existing_path(view, f"{label} view {index}")
            target = _absolute_target(source, f"{label} view {index}")
        elif isinstance(view, dict) and set(view) in (
            {"source", "target"},
            {"source", "target", "identity_policy"},
        ):
            source = _existing_path(view["source"], f"{label} view {index}")
            target = _absolute_target(view["target"], f"{label} view {index}")
        else:
            raise ExperimentSandboxError(f"invalid {label} view {index}")
        identity_policy = (
            view.get("identity_policy") if isinstance(view, dict) else None
        )
        if identity_policy not in (
            None,
            DEPENDENCY_TREE_IDENTITY_POLICY,
            PUBLIC_DEPENDENCY_TREE_IDENTITY_POLICY,
        ):
            raise ExperimentSandboxError(
                f"invalid {label} view {index} identity policy"
            )
        if identity_policy is not None and not source.is_dir():
            raise ExperimentSandboxError(
                f"{label} view {index} dependency tree must be a directory"
            )
        normalized_view = {
            "source": str(source),
            "target": str(target),
            "writable": False,
            "source_identity": _mount_source_identity(
                source,
                dependency_tree_policy=identity_policy,
            ),
        }
        if identity_policy is not None:
            normalized_view["identity_policy"] = identity_policy
        normalized.append(normalized_view)
    _deny_duplicate_targets(normalized)
    return normalized


def _validate_normalized_views(views, label):
    if not isinstance(views, list):
        raise ExperimentSandboxError(f"{label} views must be a list")
    for index, view in enumerate(views):
        valid_fields = (
            {"source", "target", "writable", "source_identity"},
            {
                "source",
                "target",
                "writable",
                "source_identity",
                "identity_policy",
            },
        )
        if (
            not isinstance(view, dict)
            or set(view) not in valid_fields
            or view.get("writable") is not False
        ):
            raise ExperimentSandboxError(f"invalid {label} view {index}")
        identity_policy = view.get("identity_policy")
        if identity_policy not in (
            None,
            DEPENDENCY_TREE_IDENTITY_POLICY,
            PUBLIC_DEPENDENCY_TREE_IDENTITY_POLICY,
        ):
            raise ExperimentSandboxError(
                f"invalid {label} view {index} identity policy"
            )
        identity_kind = (
            view.get("source_identity", {}).get("kind")
            if isinstance(view.get("source_identity"), dict)
            else None
        )
        expected_identity_kind = {
            None: None,
            DEPENDENCY_TREE_IDENTITY_POLICY: "bounded_dependency_directory",
            PUBLIC_DEPENDENCY_TREE_IDENTITY_POLICY: (
                "bounded_public_dependency_directory"
            ),
        }[identity_policy]
        if identity_policy is None:
            identity_matches = identity_kind != "bounded_dependency_directory" and (
                identity_kind != "bounded_public_dependency_directory"
            )
        else:
            identity_matches = identity_kind == expected_identity_kind
        if not identity_matches:
            raise ExperimentSandboxError(
                f"{label} view {index} identity policy differs from identity"
            )
        _validate_mount_source(
            view["source"],
            view["source_identity"],
            f"{label} view {index}",
            require_directory=identity_policy is not None,
        )
        _absolute_target(view["target"], f"{label} view {index}")
    _deny_duplicate_targets(views)


def _normalize_credentials(mounts):
    if not isinstance(mounts, (list, tuple)):
        raise ExperimentSandboxError("credential mounts must be a list")
    if len(mounts) > DEFAULT_MAX_CREDENTIAL_FILES:
        raise ExperimentSandboxError("credential mounts exceed their bound")
    normalized = []
    for index, mount in enumerate(mounts):
        if not isinstance(mount, dict) or set(mount) != {"source", "target"}:
            raise ExperimentSandboxError(f"invalid credential mount {index}")
        source = _existing_path(
            mount["source"],
            f"credential mount {index}",
            reject_symlink=True,
        )
        target = _absolute_target(
            mount["target"],
            f"credential mount {index}",
        )
        if not _is_relative_to(target, _CREDENTIAL_ROOT) or target == _CREDENTIAL_ROOT:
            raise ExperimentSandboxError(
                "credential target must be below /run/agentteam-credentials"
            )
        inventory = _credential_inventory(source)
        normalized.append(
            {
                "source": str(source),
                "target": str(target),
                "writable": False,
                "source_identity": _mount_source_identity(source),
                **inventory,
            }
        )
    _deny_duplicate_targets(normalized)
    return normalized


def _validate_normalized_credentials(views):
    if not isinstance(views, list):
        raise ExperimentSandboxError("credential views must be a list")
    rebuilt = _normalize_credentials(
        [
            {"source": view.get("source"), "target": view.get("target")}
            for view in views
            if isinstance(view, dict)
        ]
    )
    if views != rebuilt:
        raise ExperimentSandboxError("credential inventory or policy drift")


def _credential_inventory(path):
    files, _entry_count = _bounded_regular_files(
        [path],
        max_entries=DEFAULT_MAX_CREDENTIAL_FILES * 4,
    )
    count = 0
    total = 0
    digest = hashlib.sha256()
    for candidate in sorted(files):
        content = _read_bounded_regular_file(
            candidate,
            max_bytes=DEFAULT_MAX_CREDENTIAL_BYTES - total,
        )
        count += 1
        total += len(content)
        if count > DEFAULT_MAX_CREDENTIAL_FILES:
            raise ExperimentSandboxError("credential file bound was exceeded")
        if total > DEFAULT_MAX_CREDENTIAL_BYTES:
            raise ExperimentSandboxError("credential byte bound was exceeded")
        relative = (
            candidate.name
            if path.is_file()
            else candidate.relative_to(path).as_posix()
        )
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(content).digest())
    if count == 0:
        raise ExperimentSandboxError("credential mount contains no regular files")
    return {
        "file_count": count,
        "total_bytes": total,
        "content_sha256": digest.hexdigest(),
    }


def _normalize_environment(environment):
    values = dict(_DEFAULT_ENVIRONMENT)
    if environment is not None:
        if not isinstance(environment, dict):
            raise ExperimentSandboxError("provider environment must be an object")
        if not all(isinstance(name, str) for name in environment):
            raise ExperimentSandboxError(
                "provider environment names must be strings"
            )
        values.update(environment)
    normalized = {}
    for name in sorted(values):
        value = values[name]
        if (
            not isinstance(name, str)
            or (
                _ENVIRONMENT_NAME.fullmatch(name) is None
                and name not in _LOWERCASE_PROXY_ENVIRONMENT_NAMES
            )
            or not isinstance(value, str)
            or "\x00" in value
            or len(value) > 4096
        ):
            raise ExperimentSandboxError("provider environment is not bounded")
        normalized[name] = value
    if len(normalized) > 64:
        raise ExperimentSandboxError("provider environment has too many entries")
    return normalized


def _validated_repository_identity(
    repository,
    identity,
    *,
    verify_workspace=True,
):
    if identity is None:
        return None
    if not isinstance(identity, dict) or set(identity) != {
        "commit",
        "tree",
        "git_object_format",
    }:
        raise ExperimentSandboxError(
            "repository identity must bind commit, tree, and object format"
        )
    object_format = identity["git_object_format"]
    digest_lengths = {"sha1": 40, "sha256": 64}
    if object_format not in digest_lengths:
        raise ExperimentSandboxError(
            "repository identity object format is unsupported"
        )
    digest_length = digest_lengths[object_format]
    for field in ("commit", "tree"):
        value = identity[field]
        if (
            not isinstance(value, str)
            or len(value) != digest_length
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ExperimentSandboxError(
                f"repository identity {field} is invalid"
            )
    if not verify_workspace:
        return dict(identity)
    _validate_standalone_git_control(repository)
    observed = {}
    for field, arguments in (
        ("commit", ("rev-parse", "--verify", "HEAD")),
        ("tree", ("rev-parse", "HEAD^{tree}")),
        ("git_object_format", ("rev-parse", "--show-object-format")),
    ):
        try:
            completed = subprocess.run(
                _sanitized_git_argv(repository, arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=30,
                env=_sanitized_git_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExperimentSandboxError(
                "repository identity is unavailable"
            ) from exc
        if completed.returncode != 0 or len(completed.stdout) > 4096:
            raise ExperimentSandboxError(
                "repository identity is unavailable"
            )
        observed[field] = completed.stdout.strip()
    if observed != identity:
        raise ExperimentSandboxError(
            "repository identity does not match the certified workspace"
        )
    return dict(identity)


def _sanitized_git_argv(repository, arguments):
    git = _existing_path(
        _TRUSTED_GIT_PATH,
        "trusted Git executable",
        require_file=True,
        reject_symlink=True,
    )
    if git != _TRUSTED_GIT_PATH or not os.access(git, os.X_OK):
        raise ExperimentSandboxUnavailable(
            "trusted Git executable is unavailable"
        )
    return [
        str(git),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "submodule.recurse=false",
        "-C",
        str(repository),
        *arguments,
    ]


def _sanitized_git_environment():
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def _candidate_source_unchanged(before, after):
    """Ignore evaluator-created untracked files while protecting source state."""

    protected = (
        "baseline_commit",
        "baseline_tree",
        "head_commit",
        "head_tree",
        "git_object_format",
        "tracked_status_sha256",
    )
    return all(after.get(field) == before.get(field) for field in protected)


def _candidate_repository_state(repository, baseline_identity):
    repository = _existing_path(
        repository,
        "candidate repository",
        require_directory=True,
        reject_symlink=True,
    )
    git_directory = _validate_standalone_git_control(repository)

    def git_output(*arguments, binary=False):
        try:
            completed = subprocess.run(
                _sanitized_git_argv(repository, arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=not binary,
                check=False,
                timeout=30,
                env=_sanitized_git_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExperimentSandboxError(
                "candidate repository identity is unavailable"
            ) from exc
        stdout = completed.stdout
        if (
            completed.returncode != 0
            or len(stdout) > 4 * 1024 * 1024
        ):
            raise ExperimentSandboxError(
                "candidate repository identity is unavailable"
            )
        return stdout

    head_commit = git_output("rev-parse", "--verify", "HEAD").strip()
    head_tree = git_output("rev-parse", "HEAD^{tree}").strip()
    object_format = git_output(
        "rev-parse",
        "--show-object-format",
    ).strip()
    try:
        ancestor = subprocess.run(
            _sanitized_git_argv(
                repository,
                (
                    "merge-base",
                    "--is-ancestor",
                    baseline_identity["commit"],
                    head_commit,
                ),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            env=_sanitized_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentSandboxError(
            "candidate repository ancestry is unavailable"
        ) from exc
    if (
        ancestor.returncode != 0
        or object_format != baseline_identity["git_object_format"]
    ):
        raise ExperimentSandboxError(
            "candidate repository does not descend from the certified baseline"
        )
    status = git_output(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
        binary=True,
    )
    workspace_inventory = _bounded_tree_identity(
        repository,
        excluded_roots={git_directory},
        max_entries=DEFAULT_MAX_SCAN_FILES,
        max_bytes=DEFAULT_MAX_SCAN_BYTES,
    )
    git_control_inventory = _bounded_tree_identity(
        git_directory,
        excluded_roots={git_directory / "objects"},
        max_entries=DEFAULT_MAX_SCAN_FILES,
        max_bytes=DEFAULT_MAX_SCAN_BYTES,
    )
    return {
        "baseline_commit": baseline_identity["commit"],
        "baseline_tree": baseline_identity["tree"],
        "head_commit": head_commit,
        "head_tree": head_tree,
        "git_object_format": object_format,
        "tracked_status_sha256": hashlib.sha256(status).hexdigest(),
        "working_tree_sha256": workspace_inventory["sha256"],
        "working_tree_files": workspace_inventory["files"],
        "working_tree_directories": workspace_inventory["directories"],
        "working_tree_bytes": workspace_inventory["bytes"],
        "git_control_sha256": git_control_inventory["sha256"],
        "git_control_files": git_control_inventory["files"],
        "git_control_directories": git_control_inventory["directories"],
        "git_control_bytes": git_control_inventory["bytes"],
    }


def certify_candidate_repository(repository, baseline_identity):
    """Return bounded identity proof for one standalone candidate repository."""

    if not isinstance(baseline_identity, dict) or set(
        baseline_identity
    ) != {"commit", "tree", "git_object_format"}:
        raise ExperimentSandboxError(
            "candidate baseline identity is invalid"
        )
    return _candidate_repository_state(repository, baseline_identity)


def _validate_standalone_git_control(repository):
    git_directory = _existing_path(
        Path(repository) / ".git",
        "candidate Git control directory",
        require_directory=True,
        reject_symlink=True,
    )
    config_path = git_directory / "config"
    payload = _read_bounded_regular_file(
        config_path,
        max_bytes=64 * 1024,
    )
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
    )
    parser.optionxform = str.lower
    try:
        parser.read_string(payload.decode("utf-8"))
    except (
        UnicodeDecodeError,
        configparser.Error,
    ) as exc:
        raise ExperimentSandboxError(
            "candidate Git config is invalid"
        ) from exc
    allowed = {
        "core": {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "ignorecase",
            "precomposeunicode",
            "symlinks",
        },
        "extensions": {"objectformat"},
        "user": {"name", "email"},
    }
    for section in parser.sections():
        normalized_section = section.lower()
        if normalized_section not in allowed or any(
            option.lower() not in allowed[normalized_section]
            for option in parser.options(section)
        ):
            raise ExperimentSandboxError(
                "candidate Git config contains undeclared behavior"
            )
    return git_directory


def _bounded_tree_identity(root, *, excluded_roots, max_entries, max_bytes):
    root = _existing_path(
        root,
        "bounded tree root",
        require_directory=True,
        reject_symlink=True,
    )
    excluded = {
        Path(path).resolve(strict=False)
        for path in excluded_roots
    }
    stack = [root]
    entries = []
    while stack:
        directory = stack.pop()
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda entry: os.fsencode(entry.name),
            )
        except OSError as exc:
            raise ExperimentSandboxError(
                "candidate repository changed during inventory"
            ) from exc
        for entry in children:
            path = Path(entry.path)
            if path in excluded:
                continue
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ExperimentSandboxError(
                    "candidate repository changed during inventory"
                ) from exc
            relative = path.relative_to(root)
            if entry.is_symlink():
                kind = "symlink"
            elif entry.is_dir(follow_symlinks=False):
                kind = "directory"
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
            else:
                raise ExperimentSandboxError(
                    "candidate repository contains a non-regular entry"
                )
            entries.append((relative, path, kind, metadata))
            if len(entries) > max_entries:
                raise ExperimentSandboxError(
                    "candidate repository inventory exceeds its entry bound"
                )
    digest = hashlib.sha256()
    file_count = 0
    directory_count = 0
    content_bytes = 0
    for relative, path, kind, metadata in sorted(
        entries,
        key=lambda item: os.fsencode(item[0]),
    ):
        if kind == "symlink":
            try:
                content = os.fsencode(os.readlink(path))
            except OSError as exc:
                raise ExperimentSandboxError(
                    "candidate repository changed during inventory"
                ) from exc
            file_count += 1
        elif kind == "file":
            content = _read_bounded_regular_file(
                path,
                max_bytes=max_bytes - content_bytes,
            )
            file_count += 1
        else:
            content = b""
            directory_count += 1
        content_bytes += len(content)
        if content_bytes > max_bytes:
            raise ExperimentSandboxError(
                "candidate repository exceeds its content bound"
            )
        digest.update(os.fsencode(relative) + b"\0")
        digest.update(kind.encode("ascii") + b"\0")
        digest.update(
            f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii") + b"\0"
        )
        digest.update(hashlib.sha256(content).digest())
    return {
        "sha256": digest.hexdigest(),
        "files": file_count,
        "directories": directory_count,
        "bytes": content_bytes,
    }


def _repository_workspace_sha256(repository, *, require_git=True):
    """Bind provider-visible files and, for live launches, the Git index."""

    repository = _existing_path(
        repository,
        "repository workspace",
        require_directory=True,
        reject_symlink=True,
    )
    git_directory = (
        _validate_standalone_git_control(repository)
        if require_git
        else repository / ".git"
    )
    excluded_roots = (
        {git_directory}
        if git_directory.exists() or git_directory.is_symlink()
        else set()
    )
    excluded_roots = (
        {git_directory / "objects"}
        if require_git
        else excluded_roots
    )
    tree = _bounded_tree_identity(
        repository,
        excluded_roots=excluded_roots,
        max_entries=DEFAULT_MAX_SCAN_FILES,
        max_bytes=DEFAULT_MAX_SCAN_BYTES,
    )
    if not require_git:
        return hashlib.sha256(_canonical_json_bytes(tree)).hexdigest()
    try:
        index = subprocess.run(
            _sanitized_git_argv(
                repository,
                ("ls-files", "--stage", "-z"),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            env=_sanitized_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentSandboxError(
            "repository workspace index is unavailable"
        ) from exc
    if (
        index.returncode != 0
        or len(index.stdout) > DEFAULT_MAX_SCAN_BYTES
    ):
        raise ExperimentSandboxError(
            "repository workspace index is unavailable"
        )
    digest = hashlib.sha256()
    digest.update(_canonical_json_bytes(tree))
    digest.update(hashlib.sha256(index.stdout).digest())
    return digest.hexdigest()


def _sandbox_policy_sha256(descriptor):
    policy = {
        key: value
        for key, value in descriptor.items()
        if key not in {"policy_sha256", "namespace_evidence"}
    }
    return hashlib.sha256(_canonical_json_bytes(policy)).hexdigest()


def _probe_python_for_descriptor(descriptor):
    for view in (
        *descriptor["runtime_views"],
        *descriptor["library_views"],
    ):
        source = Path(view["source"])
        target = Path(view["target"])
        try:
            relative = Path(sys_executable()).relative_to(source)
        except ValueError:
            continue
        return target / relative
    raise ExperimentSandboxUnavailable(
        "probe Python is not present in declared runtime/library views"
    )


def sys_executable():
    import sys

    return str(Path(sys.executable).resolve())


def _target_parent_arguments(targets):
    directories = set()
    for target in targets:
        parent = Path(target).parent
        while parent != Path("/"):
            directories.add(str(parent))
            parent = parent.parent
    arguments = []
    for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
        if directory in {"/proc", "/dev", "/tmp", "/run", str(_CREDENTIAL_ROOT)}:
            continue
        arguments.extend(["--dir", directory])
    return arguments


def _bounded_regular_files(paths, *, max_entries):
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    if not isinstance(paths, (list, tuple)):
        raise ExperimentSandboxError("leak scan paths must be a list")
    if max_entries < 1:
        raise ExperimentSandboxUnavailable(
            "canary leak scan entry bound was exceeded"
        )
    files = []
    stack = []
    entry_count = 0
    for raw in paths:
        path = _existing_path(raw, "leak scan path", reject_symlink=True)
        entry_count += 1
        if entry_count > max_entries:
            raise ExperimentSandboxUnavailable(
                "canary leak scan entry bound was exceeded"
            )
        stack.append(path)
    while stack:
        candidate = stack.pop()
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise ExperimentSandboxUnavailable(
                "leak scan entry became unavailable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ExperimentSandboxUnavailable(
                "leak scan encountered a symlink"
            )
        if stat.S_ISREG(metadata.st_mode):
            files.append(candidate)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise ExperimentSandboxUnavailable(
                "leak scan encountered a non-regular entry"
            )
        try:
            with os.scandir(candidate) as entries:
                children = []
                for entry in entries:
                    entry_count += 1
                    if entry_count > max_entries:
                        raise ExperimentSandboxUnavailable(
                            "canary leak scan entry bound was exceeded"
                        )
                    children.append(Path(entry.path))
        except OSError as exc:
            raise ExperimentSandboxUnavailable(
                "leak scan directory became unavailable"
            ) from exc
        stack.extend(
            reversed(
                sorted(
                    children,
                    key=lambda value: os.fsencode(value.name),
                )
            )
        )
    return files, entry_count


def _read_bounded_regular_file(path, *, max_bytes):
    if max_bytes < 0:
        raise ExperimentSandboxUnavailable(
            "canary leak scan byte bound was exceeded"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExperimentSandboxUnavailable(
            "leak scan file became unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ExperimentSandboxUnavailable(
                "leak scan encountered a non-regular entry"
            )
        content = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes - len(content) + 1))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > max_bytes:
                raise ExperimentSandboxUnavailable(
                    "canary leak scan byte bound was exceeded"
                )
        return bytes(content)
    finally:
        os.close(descriptor)


def _read_digest_bound_evaluator(path, expected_sha256):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExperimentSandboxError(
            "trusted evaluator authority cannot be opened"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size > 4 * 1024 * 1024
        ):
            raise ExperimentSandboxError(
                "trusted evaluator authority is unsafe"
            )
        content = bytearray()
        remaining = 4 * 1024 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            content.extend(chunk)
            remaining -= len(chunk)
        if (
            remaining == 0
            or hashlib.sha256(content).hexdigest() != expected_sha256
        ):
            raise ExperimentSandboxError(
                "trusted evaluator authority changed before execution"
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _approved_acceptance_executable(
    value,
    *,
    cwd,
    environment,
    descriptor,
):
    del cwd, environment
    value_path = Path(value)
    if not value_path.is_absolute() or ".." in value_path.parts:
        raise ExperimentSandboxError(
            "acceptance command executable must be an absolute path"
        )
    mapped_sources = []
    for view_kind, views in (
        ("runtime", descriptor["runtime_views"]),
        ("library", descriptor["library_views"]),
    ):
        for view in views:
            source_root = Path(view["source"])
            try:
                relative = value_path.relative_to(Path(view["target"]))
            except ValueError:
                continue
            candidate = source_root
            try:
                for part in relative.parts:
                    candidate = candidate / part
                    metadata = candidate.lstat()
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ExperimentSandboxError(
                            "acceptance executable namespace path contains a symlink"
                        )
                metadata = candidate.stat()
            except OSError as exc:
                raise ExperimentSandboxError(
                    "acceptance executable namespace mapping is unavailable"
                ) from exc
            if stat.S_ISREG(metadata.st_mode) and os.access(candidate, os.X_OK):
                mapped_sources.append(
                    (candidate, view_kind, Path(view["target"]))
                )
    if len({item[0] for item in mapped_sources}) != 1:
        raise ExperimentSandboxError(
            "acceptance command executable is not uniquely mapped"
        )
    executable, view_kind, target_root = mapped_sources[0]
    approved_system_paths = {
        path.resolve()
        for path in (
            Path(sys.executable),
            Path("/usr/bin/python3"),
            Path("/usr/local/bin/python3"),
        )
        if path.is_file()
    }
    if executable in approved_system_paths:
        return executable
    public_environment_root = Path("/opt/agentteam/benchmark-env")
    if view_kind == "library" and (
        target_root == public_environment_root
        or public_environment_root in target_root.parents
    ):
        return executable
    raise ExperimentSandboxError(
        "acceptance command executable is not approved"
    )


def _validate_schema_record(record, schema_name, label):
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / schema_name
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentEvaluationBlocked(f"{label} schema is unavailable") from exc
    errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(record),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise ExperimentEvaluationBlocked(
            f"{label} schema failed at {location}: {first.message}"
        )


def _normalize_argv(command, label):
    if (
        not isinstance(command, (list, tuple))
        or not command
        or len(command) > 128
        or not all(
            isinstance(argument, str)
            and argument
            and "\x00" not in argument
            for argument in command
        )
        or sum(len(argument) for argument in command) > 64 * 1024
    ):
        raise ExperimentSandboxError(f"{label} must be a bounded argv list")
    return list(command)


def _existing_path(
    value,
    label,
    *,
    require_directory=False,
    require_file=False,
    reject_symlink=False,
):
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ExperimentSandboxError(f"{label} is not a path") from exc
    if not path.is_absolute():
        path = path.resolve(strict=False)
    if reject_symlink and path.is_symlink():
        raise ExperimentSandboxError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExperimentSandboxError(f"{label} is unavailable") from exc
    if require_directory and not resolved.is_dir():
        raise ExperimentSandboxError(f"{label} must be a directory")
    if require_file and not resolved.is_file():
        raise ExperimentSandboxError(f"{label} must be a file")
    if not require_directory and not require_file and not (
        resolved.is_file() or resolved.is_dir()
    ):
        raise ExperimentSandboxError(f"{label} is not a regular path")
    return resolved


def _mount_source_identity(
    path,
    *,
    hash_directory=True,
    dependency_tree_policy=None,
):
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ExperimentSandboxError(
            "sandbox mount source identity is unavailable"
        ) from exc
    if stat.S_ISREG(metadata.st_mode):
        kind = "file"
        content_sha256 = _sha256_bounded_regular_file(
            path,
            max_bytes=DEFAULT_MAX_RUNTIME_FILE_BYTES,
            expected_metadata=metadata,
        )
    elif stat.S_ISDIR(metadata.st_mode):
        if not hash_directory:
            kind = "directory_root"
            content_sha256 = None
        elif _is_privileged_system_tree(path):
            kind = "privileged_system_directory"
            content_sha256 = None
        elif dependency_tree_policy is not None:
            if dependency_tree_policy == DEPENDENCY_TREE_IDENTITY_POLICY:
                max_entries = DEPENDENCY_TREE_MAX_ENTRIES
                max_bytes = DEPENDENCY_TREE_MAX_BYTES
                kind = "bounded_dependency_directory"
            elif dependency_tree_policy == PUBLIC_DEPENDENCY_TREE_IDENTITY_POLICY:
                max_entries = PUBLIC_DEPENDENCY_TREE_MAX_ENTRIES
                max_bytes = PUBLIC_DEPENDENCY_TREE_MAX_BYTES
                kind = "bounded_public_dependency_directory"
            else:
                raise ExperimentSandboxError(
                    "sandbox mount dependency identity policy is invalid"
                )
            tree = _bounded_tree_identity(
                path,
                excluded_roots=set(),
                max_entries=max_entries,
                max_bytes=max_bytes,
            )
            content_sha256 = tree["sha256"]
        else:
            tree = _bounded_tree_identity(
                path,
                excluded_roots=set(),
                max_entries=DEFAULT_MAX_SCAN_FILES,
                max_bytes=DEFAULT_MAX_SCAN_BYTES,
            )
            kind = "bounded_directory"
            content_sha256 = tree["sha256"]
    else:
        raise ExperimentSandboxError(
            "sandbox mount source must be a regular file or directory"
        )
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "kind": kind,
        "content_sha256": content_sha256,
    }


def _sha256_bounded_regular_file(
    path,
    *,
    max_bytes,
    expected_metadata,
):
    """Hash a large mount source without retaining its bytes in memory."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExperimentSandboxUnavailable(
            "sandbox mount source became unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > max_bytes
        ):
            raise ExperimentSandboxUnavailable(
                "sandbox mount source byte bound was exceeded"
            )
        if any(
            getattr(expected_metadata, field) != getattr(before, field)
            for field in identity_fields
        ):
            raise ExperimentSandboxUnavailable(
                "sandbox mount source changed before hashing"
            )
        digest = hashlib.sha256()
        observed_bytes = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed_bytes += len(chunk)
            if observed_bytes > max_bytes:
                raise ExperimentSandboxUnavailable(
                    "sandbox mount source byte bound was exceeded"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = Path(path).lstat()
        except OSError as exc:
            raise ExperimentSandboxUnavailable(
                "sandbox mount source changed while hashing"
            ) from exc
        if (
            observed_bytes != before.st_size
            or any(
                getattr(before, field) != getattr(after, field)
                for field in identity_fields
            )
            or any(
                getattr(after, field) != getattr(path_after, field)
                for field in identity_fields
            )
        ):
            raise ExperimentSandboxUnavailable(
                "sandbox mount source changed while hashing"
            )
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _is_privileged_system_tree(path):
    path = Path(path)
    if not _is_relative_to(path, Path("/usr")) and not any(
        _is_relative_to(path, root)
        for root in (Path("/bin"), Path("/lib"), Path("/lib64"))
    ):
        return False
    try:
        trusted_system_uid = _TRUSTED_BWRAP_PATH.stat().st_uid
    except OSError:
        return False
    if trusted_system_uid != 0:
        return False
    current = path
    while True:
        try:
            metadata = current.stat()
        except OSError:
            return False
        if (
            metadata.st_uid != trusted_system_uid
            or metadata.st_uid == os.geteuid()
            or os.access(current, os.W_OK)
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            return False
        if current == Path("/"):
            return True
        current = current.parent


def _validate_mount_source(
    value,
    expected_identity,
    label,
    *,
    require_directory=False,
):
    try:
        lexical = Path(value)
    except (TypeError, ValueError) as exc:
        raise ExperimentSandboxError(f"{label} source is not a path") from exc
    if not lexical.is_absolute() or lexical.is_symlink():
        raise ExperimentSandboxError(
            f"{label} source must remain a canonical non-symlink path"
        )
    resolved = _existing_path(
        lexical,
        f"{label} source",
        require_directory=require_directory,
    )
    identity_kind = expected_identity.get("kind")
    hash_directory = identity_kind != "directory_root"
    dependency_tree_policy = {
        "bounded_dependency_directory": DEPENDENCY_TREE_IDENTITY_POLICY,
        "bounded_public_dependency_directory": (
            PUBLIC_DEPENDENCY_TREE_IDENTITY_POLICY
        ),
    }.get(identity_kind)
    if (
        resolved != lexical
        or _mount_source_identity(
            lexical,
            hash_directory=hash_directory,
            dependency_tree_policy=dependency_tree_policy,
        )
        != expected_identity
    ):
        raise ExperimentSandboxError(
            f"{label} source identity is unavailable or changed"
        )
    return resolved


def _absolute_target(value, label):
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise ExperimentSandboxError(f"{label} target is not a path") from exc
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ExperimentSandboxError(f"{label} target must be a bounded absolute path")
    return path


def _deny_duplicate_targets(views):
    targets = [str(view["target"]) for view in views]
    if len(set(targets)) != len(targets):
        raise ExperimentSandboxError("sandbox mount targets must be unique")


def _deny_overlapping_targets(views):
    for index, left in enumerate(views):
        left_target = Path(left["target"])
        for right in views[index + 1 :]:
            if _paths_overlap(left_target, Path(right["target"])):
                raise ExperimentSandboxError(
                    "sandbox mount targets must not overlap"
                )


def _paths_overlap(left, right):
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _is_relative_to(path, root):
    try:
        Path(path).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonempty_text(value, label):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\x00" in value
    ):
        raise ExperimentSandboxError(f"{label} must be bounded text")
    return value


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _publish_immutable_json(path, value):
    path = Path(path)
    try:
        publication = publish_immutable_json(
            path,
            value,
            label="experiment sandbox authority",
        )
    except ExperimentContractError as exc:
        raise ExperimentSandboxError(str(exc)) from exc
    if not publication["created"]:
        raise ExperimentSandboxError(
            f"evaluation evidence already exists: {path}"
        )


def _utc_timestamp():
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

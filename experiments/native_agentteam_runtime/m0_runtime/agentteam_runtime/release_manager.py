import io
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path


RELEASE_POINTER_SCHEMA_VERSION = "agentteam_active_release.v1"
RELEASE_MANIFEST_SCHEMA_VERSION = "agentteam_release_manifest.v1"
RELEASE_MANIFEST_SCHEMA_VERSION_V2 = "agentteam_release_manifest.v2"
PROJECT_RELEASE_REF_SCHEMA_VERSION = "agentteam_project_release_ref.v1"
RUNTIME_RELEASE_BINDING_SCHEMA_VERSION = "runtime_release_binding.v1"
RUN_IDENTITY_SCHEMA_VERSION = "run_identity.v1"
RUNTIME_RELEASE_STORE_ENV = "AGENTTEAM_RUNTIME_RELEASE_ROOT"
TERMINAL_RUN_STATUSES = {"idle", "completed", "failed", "cancelled", "canceled"}
RUN_NAMESPACE_PATTERN = re.compile(r"^v[1-9][0-9]*$")


class AgentTeamReleaseError(RuntimeError):
    pass


def update_status(profile):
    work_root = Path(profile["work_root"]).resolve()
    releases = known_releases(work_root)
    active = read_active_release(work_root)
    latest = latest_installed_release(releases)
    active_release_id = active.get("release_id")
    latest_release_id = latest.get("release_id") if isinstance(latest, dict) else None
    return {
        "update_status": "status",
        "project": profile.get("project_key") or "unknown",
        "work_root": str(work_root),
        "active_release": active,
        "latest_installed_release": latest,
        "active_is_latest": bool(active_release_id and active_release_id == latest_release_id),
        "known_releases": releases,
        "run_staging": run_staging_status(work_root),
        **run_release_bindings(work_root),
    }


def install_release_from_checkout(checkout_root, work_root, release_id=None, activate=True, prune_keep_latest=1):
    checkout_root = Path(checkout_root).resolve()
    work_root = Path(work_root).resolve()
    if not checkout_root.exists():
        raise AgentTeamReleaseError(f"release source checkout not found: {checkout_root}")
    _require_clean_checkout(checkout_root)
    release_id = _safe_release_id(release_id or _source_release_id(checkout_root))
    release_root = releases_root(work_root) / release_id
    if release_root.exists():
        raise AgentTeamReleaseError(f"release already exists: {release_id}")
    release_root.parent.mkdir(parents=True, exist_ok=True)
    _copy_release_files(checkout_root, release_root)
    manifest = {
        "manifest_schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "release_id": release_id,
        "release_root": str(release_root),
        "source_root": str(checkout_root),
        "source_git_commit": _git_commit(checkout_root),
        "git_object_format": _git_object_format(checkout_root),
        "installed_at": _utc_now(),
        "launcher_path": str(release_root / "agentteam"),
        "runtime_root": str(release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"),
    }
    _write_json(release_root / "manifest.json", manifest)
    active_release = None
    if activate:
        active_release = activate_release(work_root, release_id)
    release_prune = None
    if activate and prune_keep_latest is not None:
        release_prune = prune_releases(work_root, keep_latest=prune_keep_latest)
    return {
        "update_status": "installed",
        "release": manifest,
        "active_release": active_release,
        "known_releases": known_releases(work_root),
        "release_prune": release_prune,
    }


def install_release_from_git(source_repo, source_ref, work_root, release_id=None, activate=True):
    if not source_ref:
        raise AgentTeamReleaseError("--ref is required with --from-git")
    source_repo_path = Path(source_repo).expanduser()
    if source_repo_path.exists():
        source_repo_path = source_repo_path.resolve()
        _require_git_repository(source_repo_path)
        source_commit = _resolve_local_git_ref(source_repo_path, source_ref)
        return _install_resolved_git_release(
            source_repo_path,
            str(source_repo_path),
            source_ref,
            source_commit,
            work_root,
            release_id=release_id,
            activate=activate,
        )
    source_commit = _resolve_remote_git_ref(source_repo, source_ref)
    with tempfile.TemporaryDirectory(prefix="agentteam-release-git-") as tmp:
        checkout_root = Path(tmp) / "checkout"
        _checkout_remote_git_commit(source_repo, source_commit, checkout_root)
        return _install_resolved_git_release(
            checkout_root,
            str(source_repo),
            source_ref,
            source_commit,
            work_root,
            release_id=release_id,
            activate=activate,
        )


def _install_resolved_git_release(
    source_repo_path,
    source_repo_identity,
    source_ref,
    source_commit,
    work_root,
    release_id=None,
    activate=True,
):
    source_key = _source_key(source_repo_identity)
    release_id = _safe_release_id(release_id or _release_id_from_ref(source_ref, source_commit))
    release_store_root = runtime_release_store_root()
    release_root = release_store_root / source_key / release_id
    reused_existing_release = False
    if release_root.exists():
        manifest = _read_json_if_exists(release_root / "manifest.json")
        if manifest.get("source_commit") != source_commit:
            raise AgentTeamReleaseError(
                f"release id already exists for another commit: {release_id}"
            )
        _validate_release_root(release_root)
        reused_existing_release = True
    else:
        release_root.parent.mkdir(parents=True, exist_ok=True)
        temp_release_root = release_root.with_name(f".{release_root.name}.tmp")
        if temp_release_root.exists():
            shutil.rmtree(temp_release_root)
        temp_release_root.mkdir(parents=True)
        try:
            _export_git_tree(source_repo_path, source_commit, temp_release_root)
            _validate_release_root(temp_release_root)
            manifest = _git_release_manifest(
                release_id,
                release_root,
                source_key,
                source_repo_identity,
                source_ref,
                source_commit,
            )
            _write_json(temp_release_root / "manifest.json", manifest)
            temp_release_root.rename(release_root)
        except Exception:
            if temp_release_root.exists():
                shutil.rmtree(temp_release_root)
            raise
    project_ref = _write_project_release_ref(
        work_root,
        {**manifest, "reused_existing_release": reused_existing_release},
    )
    active_release = None
    if activate:
        active_release = activate_release(work_root, release_id)
    return {
        "update_status": "installed",
        "release": {**project_ref, "reused_existing_release": reused_existing_release},
        "active_release": active_release,
        "known_releases": known_releases(work_root),
        "release_prune": None,
    }


def activate_release(work_root, release_id, update_status="activated"):
    work_root = Path(work_root).resolve()
    release_id = _safe_release_id(release_id)
    manifest = release_manifest(work_root, release_id)
    if not manifest:
        raise AgentTeamReleaseError(f"release not found: {release_id}")
    release_root = manifest.get("release_root") or str(releases_root(work_root) / release_id)
    activated_at = _utc_now()
    pointer = {
        "pointer_schema_version": RELEASE_POINTER_SCHEMA_VERSION,
        "release_id": release_id,
        "release_root": release_root,
        "activated_at": activated_at,
        "update_status": update_status,
    }
    for key in (
        "manifest_schema_version",
        "install_method",
        "source_key",
        "source_repo",
        "source_ref",
        "source_commit",
        "source_root",
        "source_git_commit",
    ):
        if manifest.get(key):
            pointer[key] = manifest[key]
    _write_json(active_release_path(work_root), pointer)
    event_type = "rollback_activated" if update_status == "rollback_activated" else "update_activated"
    release_event = _append_release_event(work_root, pointer, event_type)
    return {**pointer, "release_event": release_event}


def read_active_release(work_root):
    pointer = _read_json_if_exists(active_release_path(work_root))
    if not pointer:
        return {
            "release_id": None,
            "release_root": None,
            "managed": False,
        }
    return {
        "release_id": pointer.get("release_id"),
        "release_root": pointer.get("release_root"),
        "managed": bool(pointer.get("release_id")),
        "activated_at": pointer.get("activated_at"),
        "install_method": pointer.get("install_method"),
        "source_key": pointer.get("source_key"),
        "source_repo": pointer.get("source_repo"),
        "source_ref": pointer.get("source_ref"),
        "source_commit": pointer.get("source_commit"),
    }


def known_releases(work_root):
    root = releases_root(work_root)
    if not root.exists():
        return []
    releases_by_id = {}
    release_order = []

    def add_release(manifest):
        if not manifest:
            return
        release_id = manifest.get("release_id")
        if not release_id:
            return
        if release_id not in releases_by_id:
            release_order.append(release_id)
        releases_by_id[release_id] = manifest

    for manifest_path in sorted(root.glob("*/manifest.json")):
        add_release(_read_json_if_exists(manifest_path))
    for ref_path in sorted(project_release_refs_root(work_root).glob("*.json")):
        add_release(_read_json_if_exists(ref_path))
    return [releases_by_id[release_id] for release_id in release_order]


def latest_installed_release(releases):
    releases = [release for release in releases if isinstance(release, dict) and release.get("release_id")]
    if not releases:
        return {"release_id": None, "reason": "no_installed_releases"}
    with_installed_at = [release for release in releases if release.get("installed_at")]
    if with_installed_at:
        return max(
            enumerate(with_installed_at),
            key=lambda item: (item[1].get("installed_at") or "", item[0]),
        )[1]
    if len(releases) == 1:
        return releases[0]
    return {"release_id": None, "reason": "missing_installed_at"}


def run_release_bindings(work_root):
    run_root = Path(work_root).resolve() / "runs"
    runs_by_release = {}
    unmanaged_runs = []
    if not run_root.exists():
        return {"runs_by_release": runs_by_release, "unmanaged_runs": unmanaged_runs}
    for run_dir in _iter_run_identity_directories(run_root):
        try:
            pair = validate_run_binding(run_dir, expected_project_key=None)
        except AgentTeamReleaseError:
            pair = None
        if pair:
            release_id = pair["binding"]["release_id"]
            runs_by_release.setdefault(release_id, []).append(run_dir.name)
            continue
        state = _run_state(run_dir)
        release_id = state.get("runtime_release_id") if isinstance(state, dict) else None
        if release_id:
            runs_by_release.setdefault(release_id, []).append(run_dir.name)
        else:
            unmanaged_runs.append(run_dir.name)
    return {
        "runs_by_release": runs_by_release,
        "unmanaged_runs": unmanaged_runs,
    }


def prune_releases(work_root, keep_latest=1):
    work_root = Path(work_root).resolve()
    keep_latest = max(0, int(keep_latest))
    releases = known_releases(work_root)
    latest_release_ids = set(_latest_release_ids(releases, keep_latest))
    active_release_id = read_active_release(work_root).get("release_id")
    protected_release_ids = {
        release_id
        for release_id in [
            active_release_id,
            *latest_release_ids,
            *_bound_run_release_ids(work_root),
            *_nonterminal_run_release_ids(work_root),
            *_frozen_taskpack_release_ids(work_root),
        ]
        if release_id
    }
    deleted_releases = []
    for release in releases:
        raw_release_id = release.get("release_id")
        try:
            release_id = _safe_release_id(raw_release_id)
        except AgentTeamReleaseError:
            continue
        if not release_id or release_id in protected_release_ids:
            continue
        release_root = releases_root(work_root) / release_id
        if release_root.exists():
            shutil.rmtree(release_root)
            deleted_releases.append(
                {
                    "release_id": release_id,
                    "release_root": str(release_root),
                }
            )
    retained_release_ids = [release["release_id"] for release in known_releases(work_root) if release.get("release_id")]
    return {
        "prune_status": "pruned",
        "keep_latest": keep_latest,
        "deleted_release_ids": [release["release_id"] for release in deleted_releases],
        "deleted_releases": deleted_releases,
        "protected_release_ids": sorted(protected_release_ids),
        "retained_release_ids": retained_release_ids,
        "run_staging_gc": cleanup_stale_run_staging(work_root),
    }


def run_staging_status(work_root):
    staging_root = Path(work_root).resolve() / "run-staging"
    entries = []
    if staging_root.exists():
        for path in sorted(staging_root.iterdir(), key=lambda item: item.name):
            owner_pid = _staging_owner_pid(path.name)
            entries.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "owner_pid": owner_pid,
                    "owner_alive": _pid_alive(owner_pid) if owner_pid else None,
                    "safe_directory": bool(path.is_dir() and not path.is_symlink()),
                    "recovery_required": path.name.startswith("adopt."),
                }
            )
    return {
        "staging_root": str(staging_root),
        "stale_count": sum(item.get("owner_alive") is False for item in entries),
        "entries": entries,
    }


def cleanup_stale_run_staging(work_root, limit=20):
    status = run_staging_status(work_root)
    removed = []
    for item in status["entries"]:
        if len(removed) >= max(0, int(limit)):
            break
        if (
            item["owner_alive"] is not False
            or not item["safe_directory"]
            or item["recovery_required"]
        ):
            continue
        path = Path(item["path"])
        staging_root = Path(status["staging_root"])
        if path.parent.resolve() != staging_root.resolve():
            continue
        shutil.rmtree(path)
        removed.append(item["name"])
    return {
        "removed_count": len(removed),
        "removed": removed,
        "limit": max(0, int(limit)),
        "remaining": run_staging_status(work_root),
    }


def _staging_owner_pid(name):
    match = re.fullmatch(r".+\.([1-9][0-9]*)\.[0-9a-f]{16}", name)
    return int(match.group(1)) if match else None


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def prune_global_releases(work_root=None, force=False, release_store_root=None):
    store_root = Path(release_store_root).expanduser().resolve() if release_store_root else runtime_release_store_root()
    work_roots = _discover_known_work_roots(work_root, store_root)
    references = _global_release_references(work_roots)
    references_by_root = {}
    for reference in references:
        release_root = reference.get("release_root")
        if release_root:
            references_by_root.setdefault(release_root, []).append(reference)

    global_releases = []
    deleted_global_releases = []
    protected_release_ids = []
    deletable_release_ids = []
    for release in _global_release_candidates(store_root):
        release_references = references_by_root.get(release["release_root"], [])
        protection_reasons = sorted(
            {
                reference["reference_type"]
                for reference in release_references
                if reference.get("reference_type")
            }
        )
        status = "protected" if release_references else "deletable"
        record = {
            **release,
            "status": status,
            "protection_reasons": protection_reasons,
            "references": release_references,
        }
        if status == "protected":
            protected_release_ids.append(release["release_id"])
        else:
            deletable_release_ids.append(release["release_id"])
            if force and Path(release["release_root"]).exists():
                shutil.rmtree(release["release_root"])
                deleted_global_releases.append(release)
        global_releases.append(record)

    return {
        "prune_status": "pruned" if force else "dry_run",
        "release_store_root": str(store_root),
        "force": bool(force),
        "force_required": bool(deletable_release_ids and not force),
        "known_work_roots": [str(path) for path in work_roots],
        "protected_global_release_ids": sorted(protected_release_ids),
        "deletable_global_release_ids": sorted(deletable_release_ids),
        "deleted_global_release_ids": sorted(
            release["release_id"] for release in deleted_global_releases
        ),
        "deleted_global_releases": deleted_global_releases,
        "global_releases": global_releases,
    }


def record_active_release_for_run(run_dir, work_root):
    try:
        pair = validate_run_binding(run_dir, expected_project_key=None)
    except AgentTeamReleaseError:
        pair = None
    if pair:
        return {
            "recorded": False,
            "reason": "immutable_binding_exists",
            "runtime_release_id": pair["binding"]["release_id"],
            "runtime_release_root": pair["binding"]["release_root"],
        }
    active = read_active_release(work_root)
    if not active.get("release_id"):
        return {"recorded": False, "reason": "no_active_release"}
    state_path = _run_state_path(run_dir)
    state = _read_json_if_exists(state_path)
    if not state:
        return {"recorded": False, "reason": "missing_state"}
    if state.get("runtime_release_id"):
        return {
            "recorded": False,
            "reason": "already_pinned",
            "runtime_release_id": state.get("runtime_release_id"),
        }
    state["runtime_release_id"] = active["release_id"]
    state["runtime_release_root"] = active["release_root"]
    _write_json(state_path, state)
    return {
        "recorded": True,
        "runtime_release_id": active["release_id"],
        "runtime_release_root": active["release_root"],
    }


def releases_root(work_root):
    return Path(work_root).resolve() / "releases"


def project_release_refs_root(work_root):
    return releases_root(work_root) / "refs"


def active_release_path(work_root):
    return releases_root(work_root) / "active.json"


def release_events_path(work_root):
    return releases_root(work_root) / "events.jsonl"


def runtime_release_store_root():
    configured = os.environ.get(RUNTIME_RELEASE_STORE_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "agentteam" / "runtime-releases").resolve()


def selected_release_identity(work_root, release_id, expected=None):
    """Resolve one installed release without consulting the mutable active pointer."""
    work_root = Path(work_root).resolve()
    manifest = release_manifest(work_root, release_id)
    if not isinstance(manifest, dict) or not manifest:
        raise AgentTeamReleaseError(f"release manifest not found: {release_id}")
    release_root = Path(
        manifest.get("release_root") or releases_root(work_root) / _safe_release_id(release_id)
    ).expanduser().resolve()
    manifest_path = release_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AgentTeamReleaseError(f"release manifest is missing or unsafe: {manifest_path}")
    root_manifest = _read_json_strict(manifest_path, "release manifest")
    if root_manifest.get("release_id") != release_id:
        raise AgentTeamReleaseError("release manifest ID does not match selected release")
    declared_root = root_manifest.get("release_root")
    if declared_root and Path(declared_root).expanduser().resolve() != release_root:
        raise AgentTeamReleaseError("release manifest root does not match installed release")
    runtime_root = Path(
        root_manifest.get("runtime_root")
        or release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"
    ).expanduser().resolve()
    expected_runtime_root = (
        release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"
    )
    if runtime_root != expected_runtime_root:
        raise AgentTeamReleaseError("release runtime root is outside the immutable release layout")
    if not (runtime_root / "agentteam_runtime" / "agentteam.py").is_file():
        raise AgentTeamReleaseError(f"release runtime module is missing: {runtime_root}")
    source_commit = root_manifest.get("source_commit") or root_manifest.get("source_git_commit")
    git_object_format = (
        root_manifest.get("git_object_format") or _git_object_format_for_oid(source_commit)
    )
    oid_length = 40 if git_object_format == "sha1" else 64 if git_object_format == "sha256" else 0
    if not oid_length or not isinstance(source_commit, str) or not re.fullmatch(
        rf"[0-9a-f]{{{oid_length}}}", source_commit
    ):
        raise AgentTeamReleaseError("release manifest has an invalid source commit or Git object format")
    identity = {
        "release_id": release_id,
        "release_root": str(release_root),
        "runtime_root": str(runtime_root),
        "release_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_commit": source_commit,
        "git_object_format": git_object_format,
    }
    _validate_expected_release(identity, expected or {})
    return identity


def active_release_identity(work_root, expected=None):
    active = read_active_release(work_root)
    release_id = active.get("release_id")
    if not release_id:
        raise AgentTeamReleaseError("an active runtime release is required")
    identity = selected_release_identity(work_root, release_id, expected=expected)
    if active.get("release_root") and (
        Path(active["release_root"]).expanduser().resolve()
        != Path(identity["release_root"])
    ):
        raise AgentTeamReleaseError("active release pointer root does not match its manifest")
    return identity


def publish_implementation_run(
    work_root,
    *,
    project_key,
    run_id,
    taskpack_id,
    release_identity,
    expected_release=None,
    implementation_run_id=None,
    run_root=None,
):
    """Publish paired immutable records with a same-filesystem no-replace rename."""
    work_root = Path(work_root).resolve()
    canonical_run_root = work_root / "runs"
    run_root = _validated_run_root(
        canonical_run_root,
        run_root,
    )
    staging_root = work_root / "run-staging"
    state_root = work_root / "state"
    for path in (canonical_run_root, run_root, staging_root, state_root):
        path.mkdir(parents=True, exist_ok=True)
    with (state_root / "run_creation.lock").open("a+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        scan = _scan_run_identities_for_creation(
            work_root,
            expected_project_key=project_key,
        )
        sequence = max(
            (item["identity"]["creation_sequence"] for item in scan["implementation_runs"]),
            default=0,
        ) + 1
        target = run_root / run_id
        if target.exists() or target.is_symlink():
            raise AgentTeamReleaseError(f"run already exists: {target}")
        expected = dict(expected_release or {})
        _validate_expected_release(release_identity, expected)
        _validate_release_identity_files(release_identity)
        now = _utc_now()
        identity = {
            "schema_version": RUN_IDENTITY_SCHEMA_VERSION,
            "project_key": _required_slug(project_key, "project_key"),
            "run_id": _required_slug(run_id, "run_id"),
            "taskpack_id": _required_slug(taskpack_id, "taskpack_id"),
            "run_kind": "implementation",
            "created_at": now,
            "creation_sequence": sequence,
        }
        if implementation_run_id:
            identity["implementation_run_id"] = _required_slug(
                implementation_run_id, "implementation_run_id"
            )
        binding = {
            "schema_version": RUNTIME_RELEASE_BINDING_SCHEMA_VERSION,
            **{
                key: release_identity[key]
                for key in (
                    "release_id",
                    "release_root",
                    "runtime_root",
                    "release_manifest_sha256",
                    "source_commit",
                    "git_object_format",
                )
            },
            "bound_at": now,
            "approval_bound": bool(expected),
            "expected_release": expected,
        }
        staged = staging_root / f"{run_id}.{os.getpid()}.{os.urandom(8).hex()}"
        try:
            (staged / "state").mkdir(parents=True, exist_ok=False)
            _write_json_fsync(staged / "state" / "runtime_release_binding.v1.json", binding)
            _write_json_fsync(staged / "state" / "run_identity.v1.json", identity)
            _fsync_directory(staged / "state")
            _fsync_directory(staged)
            _fsync_directory(staging_root)
            _rename_noreplace(staged, target)
            _fsync_directory(run_root)
            _fsync_directory(staging_root)
        except Exception:
            if staged.exists():
                shutil.rmtree(staged)
            raise
    pair = validate_run_binding(
        target,
        expected_project_key=project_key,
        expected_release=expected,
    )
    return pair


def publish_acceptance_run_identity(
    work_root,
    *,
    project_key,
    run_id,
    taskpack_id,
    implementation_run_id,
    gate_epoch,
):
    if not isinstance(gate_epoch, int) or isinstance(gate_epoch, bool) or gate_epoch <= 0:
        raise AgentTeamReleaseError("gate_epoch must be a positive integer")
    work_root = Path(work_root).resolve()
    run_root = work_root / "runs"
    staging_root = work_root / "run-staging"
    state_root = work_root / "state"
    for path in (run_root, staging_root, state_root):
        path.mkdir(parents=True, exist_ok=True)
    with (state_root / "run_creation.lock").open("a+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        _scan_run_identities_for_creation(
            work_root,
            expected_project_key=project_key,
        )
        target = run_root / run_id
        if target.exists() or target.is_symlink():
            raise AgentTeamReleaseError(f"run already exists: {target}")
        identity = {
            "schema_version": RUN_IDENTITY_SCHEMA_VERSION,
            "project_key": _required_slug(project_key, "project_key"),
            "run_id": _required_slug(run_id, "run_id"),
            "taskpack_id": _required_slug(taskpack_id, "taskpack_id"),
            "run_kind": "acceptance_evidence",
            "created_at": _utc_now(),
            "implementation_run_id": _required_slug(
                implementation_run_id, "implementation_run_id"
            ),
            "gate_epoch": gate_epoch,
        }
        staged = staging_root / f"{run_id}.{os.getpid()}.{os.urandom(8).hex()}"
        try:
            (staged / "state").mkdir(parents=True, exist_ok=False)
            _write_json_fsync(staged / "state" / "run_identity.v1.json", identity)
            _fsync_directory(staged / "state")
            _fsync_directory(staged)
            _fsync_directory(staging_root)
            _rename_noreplace(staged, target)
            _fsync_directory(run_root)
            _fsync_directory(staging_root)
        except Exception:
            if staged.exists():
                shutil.rmtree(staged)
            raise
    return {"run_dir": str(target), "identity": identity}


def adopt_legacy_implementation_run(
    work_root,
    *,
    project_key,
    run_id,
    taskpack_id,
    release_identity,
):
    """Atomically republish one explicit legacy run with immutable records."""
    work_root = Path(work_root).resolve()
    run_root = work_root / "runs"
    staging_root = work_root / "run-staging"
    state_root = work_root / "state"
    for path in (run_root, staging_root, state_root):
        path.mkdir(parents=True, exist_ok=True)
    run_id = _required_slug(run_id, "run_id")
    taskpack_id = _required_slug(taskpack_id, "taskpack_id")
    project_key = _required_slug(project_key, "project_key")
    target = run_root / run_id
    with (state_root / "run_creation.lock").open("a+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        scan = _scan_run_identities_for_creation(
            work_root,
            expected_project_key=project_key,
            legacy_run_id=run_id,
        )
        if target.is_symlink() or not target.is_dir():
            raise AgentTeamReleaseError(f"legacy run directory is missing or unsafe: {target}")
        state_dir = target / "state"
        if state_dir.is_symlink():
            raise AgentTeamReleaseError("legacy run state directory must not be a symlink")
        identity_path = state_dir / "run_identity.v1.json"
        binding_path = state_dir / "runtime_release_binding.v1.json"
        if (
            identity_path.exists()
            or identity_path.is_symlink()
            or binding_path.exists()
            or binding_path.is_symlink()
        ):
            raise AgentTeamReleaseError("legacy run already has immutable binding records")
        _validate_release_identity_files(release_identity)
        sequence = max(
            (item["identity"]["creation_sequence"] for item in scan["implementation_runs"]),
            default=0,
        ) + 1
        now = _utc_now()
        identity = {
            "schema_version": RUN_IDENTITY_SCHEMA_VERSION,
            "project_key": project_key,
            "run_id": run_id,
            "taskpack_id": taskpack_id,
            "run_kind": "implementation",
            "created_at": now,
            "creation_sequence": sequence,
        }
        binding = {
            "schema_version": RUNTIME_RELEASE_BINDING_SCHEMA_VERSION,
            **{
                key: release_identity[key]
                for key in (
                    "release_id",
                    "release_root",
                    "runtime_root",
                    "release_manifest_sha256",
                    "source_commit",
                    "git_object_format",
                )
            },
            "bound_at": now,
            "approval_bound": False,
            "expected_release": {},
        }
        staged = staging_root / f"adopt.{run_id}.{os.getpid()}.{os.urandom(8).hex()}"
        moved = False
        try:
            _rename_noreplace(target, staged)
            moved = True
            _fsync_directory(run_root)
            _fsync_directory(staging_root)
            state_dir = staged / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            if state_dir.is_symlink():
                raise AgentTeamReleaseError("legacy run state directory must not be a symlink")
            _write_json_fsync(state_dir / "runtime_release_binding.v1.json", binding)
            _write_json_fsync(state_dir / "run_identity.v1.json", identity)
            _fsync_directory(state_dir)
            _fsync_directory(staged)
            _rename_noreplace(staged, target)
            moved = False
            _fsync_directory(run_root)
            _fsync_directory(staging_root)
        except Exception:
            if moved and staged.exists() and not target.exists():
                try:
                    _rename_noreplace(staged, target)
                    _fsync_directory(run_root)
                    _fsync_directory(staging_root)
                except Exception:
                    pass
            raise
    return validate_run_binding(
        target,
        expected_project_key=project_key,
    )


def _validated_run_root(canonical_run_root, requested_run_root):
    canonical_run_root = Path(canonical_run_root).resolve()
    if requested_run_root is None:
        return canonical_run_root
    requested = Path(requested_run_root).expanduser()
    if requested.is_symlink():
        raise AgentTeamReleaseError(
            f"versioned run root must not be a symlink: {requested}"
        )
    requested = requested.resolve()
    if requested == canonical_run_root:
        return requested
    if (
        requested.parent != canonical_run_root
        or not RUN_NAMESPACE_PATTERN.fullmatch(requested.name)
    ):
        raise AgentTeamReleaseError(
            "run_root must be the project runs root or one bounded vN namespace"
        )
    direct_identity = requested / "state" / "run_identity.v1.json"
    if direct_identity.exists() or direct_identity.is_symlink():
        raise AgentTeamReleaseError(
            "versioned run root conflicts with an existing direct vN run"
        )
    return requested


def _iter_run_identity_directories(run_root):
    yield from _iter_versioned_artifact_directories(
        run_root,
        artifact_kind="run",
    )


def _iter_versioned_artifact_directories(root, *, artifact_kind):
    root = Path(root).resolve()
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if child.is_symlink() or not child.is_dir():
            raise AgentTeamReleaseError(
                f"unsafe direct {artifact_kind} child blocks scan: {child.name}"
            )
        marker_path = (
            child / "state" / "run_identity.v1.json"
            if artifact_kind == "run"
            else child / "taskpack.yaml"
        )
        if marker_path.exists() or marker_path.is_symlink():
            if marker_path.is_symlink() or not marker_path.is_file():
                raise AgentTeamReleaseError(
                    f"unsafe direct {artifact_kind} marker blocks scan: "
                    f"{child.name}"
                )
            yield child
            continue
        if not RUN_NAMESPACE_PATTERN.fullmatch(child.name):
            yield child
            continue
        for nested in sorted(child.iterdir(), key=lambda path: path.name):
            if nested.is_symlink() or not nested.is_dir():
                raise AgentTeamReleaseError(
                    f"unsafe versioned {artifact_kind} child blocks scan: "
                    f"{child.name}/{nested.name}"
                )
            yield nested


def _scan_run_identities_for_creation(
    work_root,
    *,
    expected_project_key,
    legacy_run_id=None,
):
    """Scan valid identities while leaving unmarked legacy directories excluded."""
    run_root = Path(work_root).resolve() / "runs"
    if not run_root.exists():
        return {"implementation_runs": [], "acceptance_evidence_runs": [], "legacy_runs": []}
    implementations = []
    evidence = []
    legacy = []
    sequences = {}
    for child in _iter_run_identity_directories(run_root):
        if child.is_symlink() or not child.is_dir():
            raise AgentTeamReleaseError(f"unsafe direct run child blocks creation: {child.name}")
        state_dir = child / "state"
        if state_dir.is_symlink():
            raise AgentTeamReleaseError(f"symlink run state blocks creation: {child.name}")
        identity_path = state_dir / "run_identity.v1.json"
        binding_path = state_dir / "runtime_release_binding.v1.json"
        if not identity_path.exists() and not identity_path.is_symlink():
            if binding_path.exists() or binding_path.is_symlink():
                raise AgentTeamReleaseError(
                    f"run child has a binding without an identity: {child.name}"
                )
            legacy.append(child.name)
            continue
        if identity_path.is_symlink() or not identity_path.is_file():
            raise AgentTeamReleaseError(f"run identity is missing or unsafe: {child.name}")
        identity = _read_json_strict(identity_path, "run identity")
        _validate_run_identity(identity, child.name, expected_project_key)
        if identity["run_kind"] == "acceptance_evidence":
            if binding_path.exists() or binding_path.is_symlink():
                raise AgentTeamReleaseError(
                    "acceptance evidence run must not contain a release binding"
                )
            evidence.append({"run_dir": str(child.resolve()), "identity": identity})
            continue
        pair = validate_run_binding(child, expected_project_key=expected_project_key)
        sequence = identity["creation_sequence"]
        if sequence in sequences:
            raise AgentTeamReleaseError(
                f"duplicate implementation creation_sequence {sequence}: "
                f"{sequences[sequence]} and {child.name}"
            )
        sequences[sequence] = child.name
        implementations.append(pair)
    if legacy_run_id is not None and legacy_run_id not in legacy:
        raise AgentTeamReleaseError(f"run is not an unbound legacy run: {legacy_run_id}")
    return {
        "implementation_runs": implementations,
        "acceptance_evidence_runs": evidence,
        "legacy_runs": legacy,
    }


def scan_run_identities(work_root, expected_project_key=None):
    """Strict direct-or-versioned scan used by allocation and implicit selection."""
    run_root = Path(work_root).resolve() / "runs"
    if not run_root.exists():
        return {"implementation_runs": [], "acceptance_evidence_runs": []}
    implementations = []
    evidence = []
    sequences = {}
    for child in _iter_run_identity_directories(run_root):
        if child.is_symlink() or not child.is_dir():
            raise AgentTeamReleaseError(f"unsafe run child blocks implicit selection: {child.name}")
        state_dir = child / "state"
        if state_dir.is_symlink():
            raise AgentTeamReleaseError(
                f"symlink run state blocks implicit selection: {child.name}"
            )
        identity_path = state_dir / "run_identity.v1.json"
        if identity_path.is_symlink() or not identity_path.is_file():
            raise AgentTeamReleaseError(
                f"run child lacks an immutable identity and blocks implicit selection: {child.name}"
            )
        identity = _read_json_strict(identity_path, "run identity")
        _validate_run_identity(identity, child.name, expected_project_key)
        if identity["run_kind"] == "acceptance_evidence":
            binding_path = child / "state" / "runtime_release_binding.v1.json"
            if binding_path.exists() or binding_path.is_symlink():
                raise AgentTeamReleaseError("acceptance evidence run must not contain a release binding")
            evidence.append({"run_dir": str(child.resolve()), "identity": identity})
            continue
        pair = validate_run_binding(child, expected_project_key=expected_project_key)
        sequence = identity["creation_sequence"]
        if sequence in sequences:
            raise AgentTeamReleaseError(
                f"duplicate implementation creation_sequence {sequence}: "
                f"{sequences[sequence]} and {child.name}"
            )
        sequences[sequence] = child.name
        implementations.append(pair)
    return {
        "implementation_runs": implementations,
        "acceptance_evidence_runs": evidence,
    }


def select_latest_implementation_run(work_root, expected_project_key=None):
    scan = scan_run_identities(work_root, expected_project_key=expected_project_key)
    if not scan["implementation_runs"]:
        raise AgentTeamReleaseError("no bound implementation runs found")
    return max(
        scan["implementation_runs"],
        key=lambda item: item["identity"]["creation_sequence"],
    )


def validate_run_binding(
    run_dir,
    *,
    expected_project_key=None,
    expected_release=None,
    expected_identity_sha256=None,
    validate_release=True,
):
    run_dir = Path(run_dir)
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise AgentTeamReleaseError(f"run directory is missing or unsafe: {run_dir}")
    run_dir = run_dir.resolve()
    state_dir = run_dir / "state"
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise AgentTeamReleaseError(f"run state directory is missing or unsafe: {state_dir}")
    identity_path = state_dir / "run_identity.v1.json"
    binding_path = state_dir / "runtime_release_binding.v1.json"
    for path, label in ((identity_path, "run identity"), (binding_path, "runtime release binding")):
        if path.is_symlink() or not path.is_file():
            raise AgentTeamReleaseError(f"{label} is missing or unsafe: {path}")
    identity = _read_json_strict(identity_path, "run identity")
    _validate_run_identity(identity, run_dir.name, expected_project_key)
    if identity["run_kind"] != "implementation":
        raise AgentTeamReleaseError("selected run is not an implementation run")
    identity_digest = _canonical_json_sha256(identity)
    if expected_identity_sha256 and identity_digest != expected_identity_sha256:
        raise AgentTeamReleaseError("launcher-selected run identity digest changed")
    binding = _read_json_strict(binding_path, "runtime release binding")
    _validate_binding_shape(binding)
    _validate_expected_release(binding, expected_release or {})
    if validate_release:
        release_root = Path(binding["release_root"]).expanduser().resolve()
        manifest_path = release_root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise AgentTeamReleaseError("bound release manifest is missing")
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != binding["release_manifest_sha256"]:
            raise AgentTeamReleaseError("bound release manifest digest mismatch")
        manifest = _read_json_strict(manifest_path, "bound release manifest")
        source_commit = manifest.get("source_commit") or manifest.get("source_git_commit")
        runtime_root = Path(
            manifest.get("runtime_root")
            or release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"
        ).expanduser().resolve()
        if manifest.get("release_id") != binding["release_id"]:
            raise AgentTeamReleaseError("bound release ID does not match its manifest")
        if source_commit != binding["source_commit"]:
            raise AgentTeamReleaseError("bound release source commit does not match its manifest")
        if runtime_root != Path(binding["runtime_root"]).expanduser().resolve():
            raise AgentTeamReleaseError("bound runtime root does not match its manifest")
        if not (runtime_root / "agentteam_runtime" / "agentteam.py").is_file():
            raise AgentTeamReleaseError("bound runtime module is missing")
    return {
        "run_dir": str(run_dir),
        "identity": identity,
        "binding": binding,
        "identity_sha256": identity_digest,
    }


def validate_acceptance_run_identity(
    run_dir,
    *,
    expected_project_key=None,
    expected_implementation_run_id=None,
    expected_gate_epoch=None,
):
    run_dir = Path(run_dir)
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise AgentTeamReleaseError(f"acceptance run directory is missing or unsafe: {run_dir}")
    run_dir = run_dir.resolve()
    state_dir = run_dir / "state"
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise AgentTeamReleaseError("acceptance run state directory is missing or unsafe")
    identity_path = state_dir / "run_identity.v1.json"
    if identity_path.is_symlink() or not identity_path.is_file():
        raise AgentTeamReleaseError("acceptance evidence run identity is missing or unsafe")
    identity = _read_json_strict(identity_path, "acceptance evidence run identity")
    _validate_run_identity(identity, run_dir.name, expected_project_key)
    if identity["run_kind"] != "acceptance_evidence":
        raise AgentTeamReleaseError("evidence run is not marked acceptance_evidence")
    if (
        expected_implementation_run_id
        and identity["implementation_run_id"] != expected_implementation_run_id
    ):
        raise AgentTeamReleaseError("evidence run belongs to a different implementation run")
    if expected_gate_epoch and identity["gate_epoch"] != expected_gate_epoch:
        raise AgentTeamReleaseError("evidence run belongs to a different gate epoch")
    binding_path = run_dir / "state" / "runtime_release_binding.v1.json"
    if binding_path.exists() or binding_path.is_symlink():
        raise AgentTeamReleaseError("acceptance evidence run must not have a release binding")
    return {"run_dir": str(run_dir), "identity": identity}


def _validate_expected_release(identity, expected):
    aliases = {
        "release_id": ("release_id", "runtime_release_id", "expected_release_id"),
        "source_commit": (
            "source_commit",
            "runtime_release_source_commit",
            "expected_source_commit",
        ),
        "git_object_format": ("git_object_format", "expected_git_object_format"),
    }
    for actual_key, keys in aliases.items():
        value = next((expected.get(key) for key in keys if expected.get(key) is not None), None)
        if value is not None and identity.get(actual_key) != value:
            raise AgentTeamReleaseError(
                f"selected release {actual_key} does not match approval-bound expectation"
            )


def _validate_release_identity_files(identity):
    release_root = Path(str(identity.get("release_root") or "")).expanduser().resolve()
    manifest_path = release_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AgentTeamReleaseError("selected release manifest is missing")
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != identity.get(
        "release_manifest_sha256"
    ):
        raise AgentTeamReleaseError("selected release manifest digest changed before binding")
    manifest = _read_json_strict(manifest_path, "selected release manifest")
    source_commit = manifest.get("source_commit") or manifest.get("source_git_commit")
    runtime_root = Path(
        manifest.get("runtime_root")
        or release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"
    ).expanduser().resolve()
    if manifest.get("release_id") != identity.get("release_id"):
        raise AgentTeamReleaseError("selected release ID changed before binding")
    if source_commit != identity.get("source_commit"):
        raise AgentTeamReleaseError("selected release source commit changed before binding")
    if runtime_root != Path(str(identity.get("runtime_root") or "")).expanduser().resolve():
        raise AgentTeamReleaseError("selected runtime root changed before binding")
    if not (runtime_root / "agentteam_runtime" / "agentteam.py").is_file():
        raise AgentTeamReleaseError("selected runtime module is missing before binding")


def _validate_run_identity(identity, directory_name, expected_project_key):
    if not isinstance(identity, dict) or identity.get("schema_version") != RUN_IDENTITY_SCHEMA_VERSION:
        raise AgentTeamReleaseError("invalid run identity schema version")
    common = {
        "schema_version",
        "project_key",
        "run_id",
        "taskpack_id",
        "run_kind",
        "created_at",
    }
    for key in ("project_key", "run_id", "taskpack_id", "created_at"):
        if not isinstance(identity.get(key), str) or not identity[key]:
            raise AgentTeamReleaseError(f"run identity {key} must be a non-empty string")
    _required_slug(identity["run_id"], "run_id")
    if identity["run_id"] != directory_name:
        raise AgentTeamReleaseError("run identity does not match directory name")
    if expected_project_key and identity["project_key"] != expected_project_key:
        raise AgentTeamReleaseError("run identity project key does not match this project")
    if identity.get("run_kind") == "implementation":
        if set(identity) - (common | {"creation_sequence", "implementation_run_id"}):
            raise AgentTeamReleaseError("implementation run identity has unknown fields")
        sequence = identity.get("creation_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise AgentTeamReleaseError("implementation creation_sequence must be positive")
        if "gate_epoch" in identity:
            raise AgentTeamReleaseError("implementation identity must not contain gate_epoch")
        if "implementation_run_id" in identity:
            _required_slug(identity["implementation_run_id"], "implementation_run_id")
    elif identity.get("run_kind") == "acceptance_evidence":
        if set(identity) - (common | {"implementation_run_id", "gate_epoch"}):
            raise AgentTeamReleaseError("acceptance evidence identity has unknown fields")
        if identity.get("creation_sequence") is not None:
            raise AgentTeamReleaseError("acceptance evidence must not have creation_sequence")
        if not isinstance(identity.get("implementation_run_id"), str) or not identity["implementation_run_id"]:
            raise AgentTeamReleaseError("acceptance evidence requires implementation_run_id")
        gate_epoch = identity.get("gate_epoch")
        if not isinstance(gate_epoch, int) or isinstance(gate_epoch, bool) or gate_epoch <= 0:
            raise AgentTeamReleaseError("acceptance evidence requires a positive gate_epoch")
    else:
        raise AgentTeamReleaseError("invalid run_kind")


def _validate_binding_shape(binding):
    if not isinstance(binding, dict) or binding.get("schema_version") != RUNTIME_RELEASE_BINDING_SCHEMA_VERSION:
        raise AgentTeamReleaseError("invalid runtime release binding schema version")
    required = {
        "schema_version",
        "release_id",
        "release_root",
        "runtime_root",
        "release_manifest_sha256",
        "source_commit",
        "git_object_format",
        "bound_at",
        "approval_bound",
        "expected_release",
    }
    if set(binding) != required:
        raise AgentTeamReleaseError("runtime release binding fields do not match the schema")
    for key in (
        "release_id",
        "release_root",
        "runtime_root",
        "release_manifest_sha256",
        "source_commit",
        "git_object_format",
        "bound_at",
    ):
        if not isinstance(binding.get(key), str) or not binding[key]:
            raise AgentTeamReleaseError(f"runtime release binding {key} must be non-empty")
    if not re.fullmatch(r"[0-9a-f]{64}", binding["release_manifest_sha256"]):
        raise AgentTeamReleaseError("runtime release binding manifest digest is invalid")
    oid_length = 40 if binding["git_object_format"] == "sha1" else 64 if binding["git_object_format"] == "sha256" else 0
    if not oid_length or not re.fullmatch(rf"[0-9a-f]{{{oid_length}}}", binding["source_commit"]):
        raise AgentTeamReleaseError("runtime release binding source commit is invalid")
    if not isinstance(binding.get("approval_bound"), bool):
        raise AgentTeamReleaseError("runtime release binding approval_bound must be boolean")
    if not isinstance(binding.get("expected_release"), dict):
        raise AgentTeamReleaseError("runtime release binding expected_release must be an object")
    if set(binding["expected_release"]) - {
        "release_id",
        "source_commit",
        "git_object_format",
    }:
        raise AgentTeamReleaseError("runtime release binding expected fields are invalid")
    if binding["approval_bound"]:
        missing = {
            "release_id",
            "source_commit",
            "git_object_format",
        } - set(binding["expected_release"])
        if missing:
            raise AgentTeamReleaseError(
                "approval-bound release binding is missing expected fields: "
                + ", ".join(sorted(missing))
            )
    _validate_expected_release(binding, binding["expected_release"])


def _required_slug(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise AgentTeamReleaseError(f"{field} is not a safe identifier")
    return value


def _read_json_strict(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentTeamReleaseError(f"{label} is not readable valid JSON") from exc
    if not isinstance(value, dict):
        raise AgentTeamReleaseError(f"{label} must be a JSON object")
    return value


def _canonical_json_sha256(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json_fsync(path, value):
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path):
    fd = os.open(Path(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_noreplace(source, target):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AgentTeamReleaseError("Linux renameat2 is required for atomic run publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise AgentTeamReleaseError(
                f"atomic publication target already exists: {target}"
            )
        raise OSError(error, os.strerror(error), str(target))


def release_manifest(work_root, release_id):
    release_id = _safe_release_id(release_id)
    project_ref_path = project_release_refs_root(work_root) / f"{release_id}.json"
    if project_ref_path.exists():
        return _read_json_if_exists(project_ref_path)
    manifest_path = releases_root(work_root) / release_id / "manifest.json"
    if manifest_path.exists():
        return _read_json_if_exists(manifest_path)
    return {}


def _copy_release_files(checkout_root, release_root):
    _validate_release_root(checkout_root)
    release_root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(checkout_root / "agentteam", release_root / "agentteam")
    native_root = checkout_root / "experiments" / "native_agentteam_runtime"
    target_native_root = release_root / "experiments" / "native_agentteam_runtime"
    runtime_package = native_root / "m0_runtime" / "agentteam_runtime"
    target_package = target_native_root / "m0_runtime" / "agentteam_runtime"
    target_package.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(runtime_package, target_package)
    shutil.copytree(native_root / "schemas", target_native_root / "schemas")


def _validate_release_root(release_root):
    release_root = Path(release_root)
    launcher = release_root / "agentteam"
    runtime_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    schema_root = release_root / "experiments" / "native_agentteam_runtime" / "schemas"
    readiness_record = (
        runtime_package / "data" / "p0_experiment_readiness.v1.json"
    )
    if not launcher.exists():
        raise AgentTeamReleaseError(f"release source is missing launcher: {launcher}")
    if not runtime_package.exists():
        raise AgentTeamReleaseError(f"release source is missing runtime package: {runtime_package}")
    required_schemas = (
        "taskpack_blueprint.schema.json",
        "p0_experiment_readiness.schema.json",
        "experiment_manifest.schema.json",
    )
    missing_schemas = [
        name for name in required_schemas if not (schema_root / name).is_file()
    ]
    if missing_schemas:
        raise AgentTeamReleaseError(
            "release source is missing runtime schemas: "
            + ", ".join(missing_schemas)
        )
    if not readiness_record.is_file():
        raise AgentTeamReleaseError(
            f"release source is missing P0 readiness data: {readiness_record}"
        )


def _require_git_repository(source_repo):
    completed = subprocess.run(
        ["git", "-C", str(source_repo), "rev-parse", "--git-dir"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(f"source is not a git repository: {source_repo}")


def _resolve_local_git_ref(source_repo, source_ref):
    completed = subprocess.run(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{source_ref}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(
            completed.stderr.strip() or f"git ref not found: {source_ref}"
        )
    return completed.stdout.strip()


def _resolve_remote_git_ref(source_repo, source_ref):
    completed = subprocess.run(
        ["git", "ls-remote", str(source_repo), source_ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(
            completed.stderr.strip() or f"git ls-remote failed for {source_repo}"
        )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AgentTeamReleaseError(f"git ref not found: {source_ref}")
    peeled_tag = f"refs/tags/{source_ref}^{{}}"
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == peeled_tag:
            return parts[0]
    return lines[0].split()[0]


def _checkout_remote_git_commit(source_repo, source_commit, checkout_root):
    completed = subprocess.run(
        ["git", "clone", "--no-checkout", str(source_repo), str(checkout_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(
            completed.stderr.strip() or f"git clone failed for {source_repo}"
        )
    completed = subprocess.run(
        ["git", "-C", str(checkout_root), "checkout", "--detach", source_commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(
            completed.stderr.strip() or f"git checkout failed for {source_commit}"
        )


def _export_git_tree(source_repo, source_commit, release_root):
    completed = subprocess.run(
        ["git", "-C", str(source_repo), "archive", "--format=tar", source_commit],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or f"git archive failed for {source_commit}"
        )
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        archive.extractall(release_root)


def _git_release_manifest(release_id, release_root, source_key, source_repo, source_ref, source_commit):
    release_root = Path(release_root).resolve()
    return {
        "manifest_schema_version": RELEASE_MANIFEST_SCHEMA_VERSION_V2,
        "install_method": "git_ref",
        "release_id": release_id,
        "release_root": str(release_root),
        "source_key": source_key,
        "source_repo": source_repo,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "git_object_format": _git_object_format_for_oid(source_commit),
        "installed_at": _utc_now(),
        "launcher_path": str(release_root / "agentteam"),
        "runtime_root": str(release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime"),
    }


def _write_project_release_ref(work_root, manifest):
    project_ref = {
        **manifest,
        "project_release_ref_schema_version": PROJECT_RELEASE_REF_SCHEMA_VERSION,
        "project_ref_written_at": _utc_now(),
    }
    _write_json(project_release_refs_root(work_root) / f"{manifest['release_id']}.json", project_ref)
    return project_ref


def _source_key(source_repo):
    value = str(source_repo).strip()
    value = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", value)
    value = re.sub(r"\.git$", "", value)
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value or "source"


def _release_id_from_ref(source_ref, source_commit):
    ref_name = re.sub(r"^refs/(heads|tags)/", "", str(source_ref).strip())
    ref_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", ref_name).strip(".-")
    return _safe_release_id(f"{ref_name or 'git'}-{source_commit[:12]}")


def _require_clean_checkout(checkout_root):
    git_dir = checkout_root / ".git"
    if not git_dir.exists():
        return
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=checkout_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AgentTeamReleaseError(completed.stderr.strip() or "git status failed")
    if completed.stdout.strip():
        raise AgentTeamReleaseError("release source checkout must be clean")


def _source_release_id(checkout_root):
    commit = _git_commit(checkout_root)
    if commit:
        return f"git-{commit[:12]}"
    return "release-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def _git_commit(checkout_root):
    if not (Path(checkout_root) / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _git_object_format(checkout_root):
    if not (Path(checkout_root) / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "--show-object-format"],
        cwd=checkout_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_object_format_for_oid(oid):
    if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{40}", oid):
        return "sha1"
    if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{64}", oid):
        return "sha256"
    return None


def _latest_release_ids(releases, count):
    if count <= 0:
        return []
    indexed = [(release, index) for index, release in enumerate(releases) if release.get("installed_at")]
    indexed.sort(key=lambda item: (_release_sort_value(item[0]), item[1]), reverse=True)
    return [release["release_id"] for release, _index in indexed[:count] if release.get("release_id")]


def _release_sort_value(release):
    installed_at = release.get("installed_at")
    if installed_at:
        return (1, installed_at)
    return (0, release.get("release_id") or "")


def _nonterminal_run_release_ids(work_root):
    run_root = Path(work_root).resolve() / "runs"
    if not run_root.exists():
        return []
    release_ids = []
    for run_dir in _iter_run_identity_directories(run_root):
        state = _run_state(run_dir)
        if not isinstance(state, dict):
            continue
        release_id = state.get("runtime_release_id")
        if not release_id:
            continue
        scheduler_status = state.get("scheduler_status")
        if not scheduler_status or scheduler_status not in TERMINAL_RUN_STATUSES:
            release_ids.append(release_id)
    return release_ids


def _bound_run_release_ids(work_root):
    run_root = Path(work_root).resolve() / "runs"
    if not run_root.exists():
        return []
    release_ids = []
    for run_dir in _iter_run_identity_directories(run_root):
        try:
            pair = validate_run_binding(run_dir, expected_project_key=None)
        except AgentTeamReleaseError:
            continue
        release_ids.append(pair["binding"]["release_id"])
    return release_ids


def _frozen_taskpack_release_ids(work_root):
    frozen_root = Path(work_root).resolve() / "frozen"
    if not frozen_root.exists():
        return []
    release_ids = []
    for taskpack_dir in _iter_versioned_artifact_directories(
        frozen_root,
        artifact_kind="frozen taskpack",
    ):
        taskpack_path = taskpack_dir / "taskpack.yaml"
        if not taskpack_path.exists() and not taskpack_path.is_symlink():
            continue
        if taskpack_path.is_symlink() or not taskpack_path.is_file():
            raise AgentTeamReleaseError(
                f"frozen taskpack metadata is unsafe: {taskpack_path}"
            )
        taskpack = _read_json_strict(
            taskpack_path,
            "frozen taskpack metadata",
        )
        context = (
            taskpack.get("context")
            if isinstance(taskpack.get("context"), dict)
            else {}
        )
        release_id = context.get("runtime_release_id")
        if release_id:
            release_ids.append(_safe_release_id(release_id))
    return release_ids


def _discover_known_work_roots(current_work_root, release_store_root):
    roots = []

    def add_root(path):
        if not path:
            return
        path = Path(path).expanduser().resolve()
        if path in roots:
            return
        if path.exists() or path == Path(current_work_root or "").expanduser().resolve():
            roots.append(path)

    if current_work_root:
        add_root(current_work_root)
    agentteam_home = Path(release_store_root).expanduser().resolve().parent
    if agentteam_home.exists():
        for candidate in sorted(path for path in agentteam_home.iterdir() if path.is_dir()):
            if candidate.resolve() == Path(release_store_root).expanduser().resolve():
                continue
            if _looks_like_work_root(candidate):
                add_root(candidate)
    return roots


def _looks_like_work_root(path):
    path = Path(path)
    return any((path / name).exists() for name in ("releases", "runs", "drafts", "frozen"))


def _global_release_candidates(release_store_root):
    release_store_root = Path(release_store_root).expanduser().resolve()
    if not release_store_root.exists():
        return []
    candidates = []
    for source_dir in sorted(path for path in release_store_root.iterdir() if path.is_dir()):
        if source_dir.name.startswith("."):
            continue
        for release_root in sorted(path for path in source_dir.iterdir() if path.is_dir()):
            if release_root.name.startswith("."):
                continue
            manifest = _read_json_if_exists(release_root / "manifest.json")
            release_id = manifest.get("release_id") or release_root.name
            candidates.append(
                {
                    "release_id": release_id,
                    "source_key": manifest.get("source_key") or source_dir.name,
                    "release_root": str(release_root.resolve()),
                    "manifest_release_root": manifest.get("release_root"),
                    "installed_at": manifest.get("installed_at"),
                    "install_method": manifest.get("install_method"),
                    "source_repo": manifest.get("source_repo"),
                    "source_ref": manifest.get("source_ref"),
                    "source_commit": manifest.get("source_commit"),
                }
            )
    return candidates


def _global_release_references(work_roots):
    references = []
    for work_root in work_roots:
        work_root = Path(work_root).expanduser().resolve()
        active = _read_json_if_exists(active_release_path(work_root))
        reference = _global_release_reference(active, work_root, "active_project")
        if reference:
            references.append(reference)

        refs_root = project_release_refs_root(work_root)
        if refs_root.exists():
            for ref_path in sorted(refs_root.glob("*.json")):
                reference = _global_release_reference(
                    _read_json_if_exists(ref_path),
                    work_root,
                    "project_ref",
                    ref_path=ref_path,
                )
                if reference:
                    references.append(reference)

        run_root = work_root / "runs"
        if run_root.exists():
            for run_dir in _iter_run_identity_directories(run_root):
                try:
                    pair = validate_run_binding(
                        run_dir,
                        expected_project_key=None,
                    )
                except AgentTeamReleaseError:
                    pair = None
                if pair:
                    binding = pair["binding"]
                    reference = _global_release_reference(
                        binding,
                        work_root,
                        "bound_run",
                        run_id=run_dir.name,
                    )
                else:
                    state = _run_state(run_dir)
                    if not isinstance(state, dict):
                        continue
                    scheduler_status = state.get("scheduler_status")
                    if (
                        scheduler_status
                        and scheduler_status in TERMINAL_RUN_STATUSES
                    ):
                        continue
                    reference = _global_release_reference(
                        {
                            "release_id": state.get("runtime_release_id"),
                            "release_root": state.get("runtime_release_root"),
                        },
                        work_root,
                        "nonterminal_run",
                        run_id=run_dir.name,
                    )
                if reference:
                    references.append(reference)

        for release_id in _frozen_taskpack_release_ids(work_root):
            manifest = release_manifest(work_root, release_id)
            reference = _global_release_reference(
                manifest,
                work_root,
                "frozen_taskpack",
            )
            if reference:
                references.append(reference)
    return references


def _global_release_reference(record, work_root, reference_type, ref_path=None, run_id=None):
    if not isinstance(record, dict):
        return None
    release_root = record.get("release_root")
    if not release_root:
        return None
    reference = {
        "reference_type": reference_type,
        "work_root": str(Path(work_root).expanduser().resolve()),
        "release_id": record.get("release_id"),
        "release_root": str(Path(release_root).expanduser().resolve()),
    }
    if ref_path:
        reference["ref_path"] = str(Path(ref_path).expanduser().resolve())
    if run_id:
        reference["run_id"] = run_id
    return reference


def _safe_release_id(value):
    release_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", release_id):
        raise AgentTeamReleaseError(f"invalid release id: {value}")
    return release_id


def _run_state(run_dir):
    return _read_json_if_exists(_run_state_path(run_dir))


def _run_state_path(run_dir):
    run_dir = Path(run_dir)
    two_phase = run_dir / "state" / "two_phase_scheduler_state.json"
    if two_phase.exists():
        return two_phase
    return run_dir / "state" / "scheduler_state.json"


def _read_json_if_exists(path):
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl_if_exists(path):
    path = Path(path)
    records = []
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def _append_release_event(work_root, pointer, event_type):
    events_path = release_events_path(work_root)
    existing = _read_jsonl_if_exists(events_path)
    sequence = max(
        [
            int(event.get("sequence", 0))
            for event in existing
            if isinstance(event, dict) and str(event.get("sequence", "")).isdigit()
        ],
        default=0,
    ) + 1
    event = {
        "event_schema_version": "agentteam_release_event.v1",
        "event_id": f"REL-EVT-{sequence:04d}",
        "sequence": sequence,
        "time": pointer.get("activated_at") or _utc_now(),
        "event_type": event_type,
        "release_id": pointer.get("release_id"),
        "release_root": pointer.get("release_root"),
        "update_status": pointer.get("update_status"),
        "activated_at": pointer.get("activated_at"),
    }
    _append_jsonl(events_path, [event])
    return event


def _utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

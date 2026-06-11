import io
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
RUNTIME_RELEASE_STORE_ENV = "AGENTTEAM_RUNTIME_RELEASE_ROOT"
TERMINAL_RUN_STATUSES = {"idle", "completed", "failed", "cancelled", "canceled"}


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
                temp_release_root,
                source_key,
                source_repo_identity,
                source_ref,
                source_commit,
            )
            _write_json(temp_release_root / "manifest.json", manifest)
            temp_release_root.rename(release_root)
            manifest = {**manifest, "release_root": str(release_root)}
            _write_json(release_root / "manifest.json", manifest)
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
    for run_dir in sorted(path for path in run_root.iterdir() if path.is_dir()):
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
        for release_id in [active_release_id, *latest_release_ids, *_nonterminal_run_release_ids(work_root)]
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
    }


def record_active_release_for_run(run_dir, work_root):
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
    runtime_package = checkout_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    target_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    target_package.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(runtime_package, target_package)


def _validate_release_root(release_root):
    release_root = Path(release_root)
    launcher = release_root / "agentteam"
    runtime_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    if not launcher.exists():
        raise AgentTeamReleaseError(f"release source is missing launcher: {launcher}")
    if not runtime_package.exists():
        raise AgentTeamReleaseError(f"release source is missing runtime package: {runtime_package}")


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
    for run_dir in sorted(path for path in run_root.iterdir() if path.is_dir()):
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

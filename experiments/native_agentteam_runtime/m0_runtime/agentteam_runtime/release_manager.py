import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


RELEASE_POINTER_SCHEMA_VERSION = "agentteam_active_release.v1"
RELEASE_MANIFEST_SCHEMA_VERSION = "agentteam_release_manifest.v1"
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


def activate_release(work_root, release_id, update_status="activated"):
    work_root = Path(work_root).resolve()
    release_id = _safe_release_id(release_id)
    manifest_path = releases_root(work_root) / release_id / "manifest.json"
    if not manifest_path.exists():
        raise AgentTeamReleaseError(f"release not found: {release_id}")
    manifest = _read_json_if_exists(manifest_path)
    activated_at = _utc_now()
    pointer = {
        "pointer_schema_version": RELEASE_POINTER_SCHEMA_VERSION,
        "release_id": release_id,
        "release_root": manifest.get("release_root") or str(manifest_path.parent),
        "activated_at": activated_at,
        "update_status": update_status,
    }
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
    }


def known_releases(work_root):
    root = releases_root(work_root)
    if not root.exists():
        return []
    releases = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = _read_json_if_exists(manifest_path)
        if manifest:
            releases.append(manifest)
    return releases


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


def active_release_path(work_root):
    return releases_root(work_root) / "active.json"


def release_events_path(work_root):
    return releases_root(work_root) / "events.jsonl"


def _copy_release_files(checkout_root, release_root):
    launcher = checkout_root / "agentteam"
    runtime_package = checkout_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    if not launcher.exists():
        raise AgentTeamReleaseError(f"release source is missing launcher: {launcher}")
    if not runtime_package.exists():
        raise AgentTeamReleaseError(f"release source is missing runtime package: {runtime_package}")
    release_root.mkdir(parents=True, exist_ok=False)
    shutil.copy2(launcher, release_root / "agentteam")
    target_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
    target_package.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(runtime_package, target_package)


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

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


RELEASE_POINTER_SCHEMA_VERSION = "agentteam_active_release.v1"
RELEASE_MANIFEST_SCHEMA_VERSION = "agentteam_release_manifest.v1"


class AgentTeamReleaseError(RuntimeError):
    pass


def update_status(profile):
    work_root = Path(profile["work_root"]).resolve()
    return {
        "update_status": "status",
        "project": profile.get("project_key") or "unknown",
        "work_root": str(work_root),
        "active_release": read_active_release(work_root),
        "known_releases": known_releases(work_root),
        **run_release_bindings(work_root),
    }


def install_release_from_checkout(checkout_root, work_root, release_id=None, activate=True):
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
    return {
        "update_status": "installed",
        "release": manifest,
        "active_release": active_release,
        "known_releases": known_releases(work_root),
    }


def activate_release(work_root, release_id, update_status="activated"):
    work_root = Path(work_root).resolve()
    release_id = _safe_release_id(release_id)
    manifest_path = releases_root(work_root) / release_id / "manifest.json"
    if not manifest_path.exists():
        raise AgentTeamReleaseError(f"release not found: {release_id}")
    manifest = _read_json_if_exists(manifest_path)
    pointer = {
        "pointer_schema_version": RELEASE_POINTER_SCHEMA_VERSION,
        "release_id": release_id,
        "release_root": manifest.get("release_root") or str(manifest_path.parent),
        "activated_at": _utc_now(),
        "update_status": update_status,
    }
    _write_json(active_release_path(work_root), pointer)
    return pointer


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


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now():
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

import json
import re
from pathlib import Path


PROFILE_SCHEMA_VERSION = "agentteam_profile.v1"
LOCAL_PROFILE_EXCLUDE_PATTERN = ".agentteam/"


class AgentTeamProfileError(RuntimeError):
    pass


def profile_path_for_project(project_root):
    return Path(project_root).resolve() / ".agentteam" / "profile.json"


def default_project_key(project_root):
    name = Path(project_root).resolve().name
    return _safe_project_key(name)


def default_work_root(project_key):
    return Path.home() / ".local" / "share" / "agentteam" / project_key


def build_project_profile(
    project_root,
    project_key=None,
    work_root=None,
    author_runtime="codex",
    default_runtime="auto",
    one_shot=False,
    max_inflight=2,
    max_attempts=1,
    commit_verified_integration=False,
    notification_project=None,
    feishu_enabled=False,
    feishu_webhook_env=None,
    feishu_signing_secret_env=None,
):
    project_key = _safe_project_key(project_key or default_project_key(project_root))
    if author_runtime not in {"fake", "codex"}:
        raise AgentTeamProfileError("author_runtime must be fake or codex")
    if default_runtime not in {"auto", "fake", "codex"}:
        raise AgentTeamProfileError("default_runtime must be auto, fake, or codex")
    if max_inflight < 1:
        raise AgentTeamProfileError("max_inflight must be at least 1")
    if max_attempts < 1:
        raise AgentTeamProfileError("max_attempts must be at least 1")

    work_root_path = Path(work_root).resolve() if work_root else default_work_root(project_key)
    feishu_enabled = bool(feishu_enabled or feishu_webhook_env)
    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "project_key": project_key,
        "work_root": str(work_root_path),
        "author_runtime": author_runtime,
        "default_runtime": default_runtime,
        "one_shot": bool(one_shot),
        "max_inflight": max_inflight,
        "max_attempts": max_attempts,
        "commit_verified_integration": bool(commit_verified_integration),
        "notification_project": notification_project or project_key,
        "feishu": {
            "enabled": feishu_enabled,
            "webhook_env": feishu_webhook_env if feishu_enabled else None,
            "signing_secret_env": feishu_signing_secret_env if feishu_enabled else None,
        },
    }


def write_project_profile(project_root, profile, force=False):
    project_root = Path(project_root).resolve()
    path = profile_path_for_project(project_root)
    if path.exists() and not force:
        raise AgentTeamProfileError(f"AgentTeam profile already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ensure_project_profile_git_excluded(project_root)
    return path


def ensure_project_profile_git_excluded(project_root):
    exclude_path = Path(project_root).resolve() / ".git" / "info" / "exclude"
    if not exclude_path.exists():
        return None
    content = exclude_path.read_text(encoding="utf-8")
    patterns = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if LOCAL_PROFILE_EXCLUDE_PATTERN in patterns:
        return exclude_path
    suffix = "" if content.endswith("\n") or not content else "\n"
    exclude_path.write_text(
        content + suffix + LOCAL_PROFILE_EXCLUDE_PATTERN + "\n",
        encoding="utf-8",
    )
    return exclude_path


def load_project_profile(project_root):
    path = profile_path_for_project(project_root)
    if not path.exists():
        raise AgentTeamProfileError(f"AgentTeam profile is missing: {path}")
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("profile_schema_version") != PROFILE_SCHEMA_VERSION:
        raise AgentTeamProfileError(
            f"unsupported AgentTeam profile schema: {profile.get('profile_schema_version')}"
        )
    return profile


def _safe_project_key(value):
    key = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip().lower())
    key = key.strip("-._")
    return key or "project"

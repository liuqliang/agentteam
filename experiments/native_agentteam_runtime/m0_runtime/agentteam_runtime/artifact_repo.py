import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


ARTIFACT_SNAPSHOT_SCHEMA_VERSION = "agentteam_artifact_snapshot.v1"
DEFAULT_MAX_ARTIFACT_FILE_BYTES = 1_000_000


def snapshot_run_artifacts(
    work_root,
    run_dir,
    taskpack_id=None,
    project=None,
    max_file_bytes=DEFAULT_MAX_ARTIFACT_FILE_BYTES,
):
    work_root = Path(work_root).resolve()
    run_dir = Path(run_dir).resolve()
    taskpack_id = taskpack_id or run_dir.name
    artifacts_root = work_root / "artifacts"
    snapshot_root = artifacts_root / "runs" / taskpack_id
    copied_files = []
    skipped_files = []

    _ensure_artifact_repo(artifacts_root)
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)

    _copy_optional_file(
        run_dir / "events.jsonl",
        snapshot_root / "events.jsonl",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
    )
    _copy_selected_tree(
        run_dir / "reports",
        snapshot_root / "reports",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
        suffixes={".md", ".json"},
    )
    _copy_selected_tree(
        run_dir / "state",
        snapshot_root / "state",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
        suffixes={".json"},
    )
    _copy_selected_tree(
        run_dir / "steps",
        snapshot_root / "steps",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
        suffixes={".json", ".jsonl", ".patch"},
    )
    _copy_selected_tree(
        run_dir / "mailboxes",
        snapshot_root / "mailboxes",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
        suffixes={".jsonl"},
    )
    _copy_selected_tree(
        run_dir / "codex_results",
        snapshot_root / "codex_results",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
        suffixes={".json"},
    )
    frozen_root = work_root / "frozen" / taskpack_id
    _copy_selected_tree(
        frozen_root,
        snapshot_root / "taskpack",
        copied_files,
        skipped_files,
        artifacts_root,
        max_file_bytes,
        suffixes={".json", ".yaml"},
    )

    manifest = {
        "snapshot_schema_version": ARTIFACT_SNAPSHOT_SCHEMA_VERSION,
        "project": project,
        "taskpack_id": taskpack_id,
        "run_dir": str(run_dir),
        "artifacts_root": str(artifacts_root),
        "snapshot_root": str(snapshot_root),
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "copied_file_count": len(copied_files),
        "skipped_file_count": len(skipped_files),
        "copied_files": sorted(copied_files),
        "skipped_files": sorted(skipped_files),
    }
    _write_json(snapshot_root / "artifact_manifest.json", manifest)
    _write_json(snapshot_root / "skipped_files.json", skipped_files)

    commit = _commit_artifact_snapshot(artifacts_root, taskpack_id)
    return {
        "snapshot_status": commit["snapshot_status"],
        "artifacts_root": str(artifacts_root),
        "snapshot_root": str(snapshot_root),
        "commit_sha": commit.get("commit_sha"),
        "commit_message": commit.get("commit_message"),
        "copied_file_count": len(copied_files),
        "skipped_file_count": len(skipped_files),
    }


def snapshot_run_artifacts_safe(*args, **kwargs):
    try:
        return snapshot_run_artifacts(*args, **kwargs)
    except Exception as exc:  # pragma: no cover - defensive boundary for CLI runs
        work_root = Path(args[0]).resolve() if args else Path(".").resolve()
        return {
            "snapshot_status": "failed",
            "artifacts_root": str(work_root / "artifacts"),
            "snapshot_root": None,
            "commit_sha": None,
            "commit_message": None,
            "error": str(exc),
        }


def _ensure_artifact_repo(artifacts_root):
    artifacts_root.mkdir(parents=True, exist_ok=True)
    if not (artifacts_root / ".git").exists():
        subprocess.run(
            ["git", "-C", str(artifacts_root), "init"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    subprocess.run(
        ["git", "-C", str(artifacts_root), "config", "user.email", "agentteam@local"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(artifacts_root), "config", "user.name", "AgentTeam Artifact Bot"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _copy_selected_tree(
    source_root,
    target_root,
    copied_files,
    skipped_files,
    artifacts_root,
    max_file_bytes,
    suffixes,
):
    source_root = Path(source_root)
    if not source_root.exists():
        return
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        if source.suffix not in suffixes:
            continue
        relative = source.relative_to(source_root)
        _copy_optional_file(
            source,
            target_root / relative,
            copied_files,
            skipped_files,
            artifacts_root,
            max_file_bytes,
        )


def _copy_optional_file(
    source,
    target,
    copied_files,
    skipped_files,
    artifacts_root,
    max_file_bytes,
):
    source = Path(source)
    if not source.exists() or not source.is_file():
        return
    size = source.stat().st_size
    target = Path(target)
    if size > max_file_bytes:
        skipped_files.append(
            {
                "source_path": str(source),
                "target_path": str(target),
                "reason": "file_too_large",
                "size_bytes": size,
                "max_file_bytes": max_file_bytes,
            }
        )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    copied_files.append(str(target.relative_to(artifacts_root)))


def _commit_artifact_snapshot(artifacts_root, taskpack_id):
    subprocess.run(
        ["git", "-C", str(artifacts_root), "add", "--all"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status = subprocess.run(
        ["git", "-C", str(artifacts_root), "status", "--porcelain=v1"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    message = f"AgentTeam trace snapshot {taskpack_id}"
    if not status.stdout.strip():
        rev_parse = subprocess.run(
            ["git", "-C", str(artifacts_root), "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return {
            "snapshot_status": "unchanged",
            "commit_sha": rev_parse.stdout.strip() if rev_parse.returncode == 0 else None,
            "commit_message": message,
        }
    completed = subprocess.run(
        ["git", "-C", str(artifacts_root), "commit", "-m", message],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    rev_parse = subprocess.run(
        ["git", "-C", str(artifacts_root), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "snapshot_status": "committed",
        "commit_sha": rev_parse.stdout.strip(),
        "commit_message": message,
        "commit_stdout": completed.stdout,
        "commit_stderr": completed.stderr,
    }


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

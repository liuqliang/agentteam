"""Additive deterministic decision indexes for pre-decision runtime history."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .decision_ledger import DecisionLedger
from .experiment_contract import canonical_json_bytes


LEGACY_INDEX_SCHEMA_VERSION = "legacy_decision_index.v1"
LEGACY_CREATED_AT = "1970-01-01T00:00:00Z"


def build_legacy_decision_index(work_root, *, project_root=None):
    work_root = Path(work_root).resolve()
    project_root = Path(project_root).resolve() if project_root else None
    lineages = _scan_legacy_lineages(work_root, project_root)
    payload = {
        "schema_version": LEGACY_INDEX_SCHEMA_VERSION,
        "rationale_policy": "missing_historical_rationale_is_not_inferred",
        "lineages": lineages,
    }
    content = canonical_json_bytes(payload) + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    index_path = (
        work_root / "decisions" / f"legacy-decision-index-{digest[:24]}.json"
    )
    index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if index_path.exists():
        if index_path.read_bytes() != content:
            raise RuntimeError("legacy decision index conflicts with existing content")
    else:
        index_path.write_bytes(content)

    ledger = DecisionLedger.create(work_root)
    index_relative = index_path.relative_to(work_root).as_posix()
    for lineage in lineages:
        ledger.append_decision(lineage["decision"])
        for artifact in lineage["artifacts"]:
            if artifact["artifact_kind"] == "code_state" and project_root is not None:
                _protect_legacy_commit(
                    project_root,
                    lineage["decision"]["decision_id"],
                    artifact["artifact_id"],
                    artifact["digest"],
                )
            ledger.append_artifact_link(
                _legacy_artifact_link(
                    artifact,
                    decision_id=lineage["decision"]["decision_id"],
                )
            )
        if lineage["validation_summaries"]:
            evidence_id = "ART-legacy-evidence-" + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "decision_id": lineage["decision"]["decision_id"],
                        "index_sha256": digest,
                    }
                )
            ).hexdigest()[:24]
            ledger.append_artifact_link(
                {
                    "schema_version": "decision_artifact_link.v1",
                    "artifact_id": evidence_id,
                    "decision_id": lineage["decision"]["decision_id"],
                    "artifact_kind": "evidence",
                    "locator": "path:" + index_relative,
                    "digest_algorithm": "sha256",
                    "digest": digest,
                    "producer": "agentteam-legacy-indexer",
                    "created_at": LEGACY_CREATED_AT,
                }
            )
    return {
        "index_status": "published",
        "index_path": str(index_path),
        "index_sha256": digest,
        "lineage_count": len(lineages),
        "decision_ids": [item["decision"]["decision_id"] for item in lineages],
    }


def _scan_legacy_lineages(work_root, project_root):
    grouped = {}
    runs_root = work_root / "runs"
    if not runs_root.is_dir():
        return []
    for run_dir in _legacy_run_directories(runs_root):
        if (run_dir / "state" / "decision-binding.v1.json").is_file():
            continue
        identity = _read_json(run_dir / "state" / "run_identity.v1.json")
        taskpack_id = (
            identity.get("taskpack_id")
            if isinstance(identity, dict) and identity.get("taskpack_id")
            else run_dir.name
        )
        lineage = grouped.setdefault(
            str(taskpack_id),
            {
                "taskpack_id": str(taskpack_id),
                "run_ids": [],
                "artifacts": [],
                "validation_summaries": [],
            },
        )
        identity_run_id = identity.get("run_id") if isinstance(identity, dict) else None
        run_id = identity_run_id or run_dir.relative_to(runs_root).as_posix()
        lineage["run_ids"].append(str(run_id))
        lineage["artifacts"].extend(
            _run_artifacts(work_root, run_dir, project_root, run_id=str(run_id))
        )
        lineage["validation_summaries"].extend(
            _validation_summaries(run_dir)
        )

    lineages = []
    for taskpack_id in sorted(grouped):
        lineage = grouped[taskpack_id]
        manifest = work_root / "frozen" / taskpack_id / "manifest.json"
        if manifest.is_file():
            lineage["artifacts"].append(
                _path_artifact(work_root, manifest, "contract")
            )
        lineage["run_ids"] = sorted(set(lineage["run_ids"]))
        lineage["artifacts"] = _unique_artifacts(lineage["artifacts"])
        lineage["validation_summaries"] = sorted(
            lineage["validation_summaries"],
            key=lambda item: (
                item.get("run_id", ""),
                item.get("task_id", ""),
                item.get("attempt_id", ""),
                item.get("event_type", ""),
            ),
        )
        lineage["decision"] = _legacy_decision(taskpack_id)
        lineages.append(lineage)
    return lineages


def _legacy_run_directories(runs_root):
    candidates = set()
    for state_name in ("run_identity.v1.json", "two_phase_scheduler_state.json"):
        candidates.update(path.parent.parent for path in runs_root.rglob(state_name))
    return sorted(
        path
        for path in candidates
        if path.is_dir()
        and path != runs_root
        and not (path / "state" / "decision-binding.v1.json").is_file()
    )


def _legacy_decision(taskpack_id):
    decision_id = "DEC-legacy-" + hashlib.sha256(
        taskpack_id.encode("utf-8")
    ).hexdigest()[:24]
    return {
        "schema_version": "decision_record.v1",
        "decision_id": decision_id,
        "revision": 1,
        "decision_kind": "direction",
        "subject": "legacy_execution_lineage",
        "authority_level": "L1",
        "parent_decision_id": None,
        "supersedes_decision_id": None,
        "statement": "legacy execution lineage",
        "selected_option": "historical_execution",
        "alternatives": [],
        "rationale": "Historical records do not contain decision rationale.",
        "scope": (
            ["legacy"]
            if taskpack_id == "legacy"
            else ["legacy", taskpack_id[:200]]
        ),
        "expected_outcome": "Index existing artifacts without rewriting history.",
        "acceptance_refs": [],
        "status": "completed",
        "created_at": LEGACY_CREATED_AT,
        "created_by": "agentteam-legacy-indexer",
        "previous_revision_sha256": None,
    }


def _run_artifacts(work_root, run_dir, project_root, *, run_id):
    artifacts = []
    for path in sorted((run_dir / "reports").rglob("*")) if (run_dir / "reports").is_dir() else []:
        if path.is_file() and not path.is_symlink():
            artifacts.append(_path_artifact(work_root, path, "report"))
    state = _read_json(run_dir / "state" / "two_phase_scheduler_state.json")
    if isinstance(state, dict) and project_root is not None:
        for step in state.get("steps", []):
            result = step.get("result") if isinstance(step, dict) else None
            if not isinstance(result, dict):
                continue
            for key in (
                "code_state_commit_sha",
                "integration_commit_sha",
                "integration_baseline_commit_sha",
            ):
                commit_sha = result.get(key)
                if _git_commit_exists(project_root, commit_sha):
                    artifacts.append(
                        {
                            "artifact_id": _artifact_id(
                                "code_state", f"{run_id}:{key}:{commit_sha}"
                            ),
                            "artifact_kind": "code_state",
                            "locator": f"git:commit:{commit_sha}",
                            "digest_algorithm": _git_digest_algorithm(project_root),
                            "digest": commit_sha,
                        }
                    )
    return artifacts


def _validation_summaries(run_dir):
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return []
    summaries = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") not in {
            "validation_accepted",
            "validation_rejected",
            "integration_verified",
            "integration_blocked",
        }:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        summaries.append(
            {
                "run_id": run_dir.name,
                "event_type": event.get("event_type"),
                "task_id": payload.get("task_id"),
                "attempt_id": payload.get("attempt_id"),
                "validation_status": payload.get("validation_status"),
                "integration_verification_status": payload.get(
                    "integration_verification_status"
                ),
                "failure_category": payload.get("failure_category")
                or payload.get("block_reason"),
            }
        )
    return summaries


def _path_artifact(work_root, path, kind):
    content = path.read_bytes()
    relative = path.resolve().relative_to(work_root).as_posix()
    digest = hashlib.sha256(content).hexdigest()
    return {
        "artifact_id": _artifact_id(kind, relative + ":" + digest),
        "artifact_kind": kind,
        "locator": "path:" + relative,
        "digest_algorithm": "sha256",
        "digest": digest,
    }


def _legacy_artifact_link(artifact, *, decision_id):
    return {
        "schema_version": "decision_artifact_link.v1",
        "artifact_id": artifact["artifact_id"],
        "decision_id": decision_id,
        "artifact_kind": artifact["artifact_kind"],
        "locator": artifact["locator"],
        "digest_algorithm": artifact["digest_algorithm"],
        "digest": artifact["digest"],
        "producer": "agentteam-legacy-indexer",
        "created_at": LEGACY_CREATED_AT,
    }


def _unique_artifacts(artifacts):
    by_id = {item["artifact_id"]: item for item in artifacts}
    return [by_id[key] for key in sorted(by_id)]


def _artifact_id(kind, identity):
    return "ART-legacy-{}-{}".format(
        kind.replace("_", "-"),
        hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
    )


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _git_commit_exists(repo, commit_sha):
    if not isinstance(commit_sha, str) or not commit_sha:
        return False
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).returncode == 0


def _git_digest_algorithm(repo):
    object_format = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-object-format"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return "git_sha256" if object_format == "sha256" else "git_sha1"


def _protect_legacy_commit(repo, decision_id, artifact_id, commit_sha):
    ref = f"refs/agentteam/legacy/{decision_id}/{artifact_id}"
    completed = subprocess.run(
        ["git", "-C", str(repo), "update-ref", ref, commit_sha, ""],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode == 0:
        return ref
    existing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    if existing != commit_sha:
        raise RuntimeError("legacy code-state ref conflicts with existing history")
    return ref

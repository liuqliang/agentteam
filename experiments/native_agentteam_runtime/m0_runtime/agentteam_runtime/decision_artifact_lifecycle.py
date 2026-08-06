"""Terminal artifact publication and compaction for decision-bound runs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path

from .decision_ledger import DecisionLedger
from .experiment_contract import canonical_json_bytes
from .git_code_state import run_code_state_ref_prefix


REPORT_SCHEMA_VERSION = "decision_operator_report.v1"
DEFAULT_RAW_LOG_RETENTION_BYTES = 64 * 1024
MAX_PROTECTED_RAW_SPOOL_BYTES = 1024 * 1024
PHASE1_ACCEPTANCE_RELATIVE = Path(
    "acceptance/model-invocation-live-smoke.v1.json"
)


class DecisionArtifactLifecycleError(RuntimeError):
    """Raised when a decision artifact cannot be published or resolved."""


def publish_operator_report(binding, output_dir, report, *, created_at):
    if binding is None:
        return None
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": Path(output_dir).name,
        "root_decision_id": binding["root_decision_id"],
        "operator_report": deepcopy(report),
    }
    content = canonical_json_bytes(payload) + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    report_path = Path(output_dir) / "reports" / f"decision-report-{digest[:24]}.json"
    _publish_idempotent_bytes(report_path, content)
    work_root = Path(binding["work_root"]).resolve()
    try:
        relative = report_path.resolve().relative_to(work_root)
    except ValueError as exc:
        raise DecisionArtifactLifecycleError(
            "decision report must be inside the project work root"
        ) from exc
    artifact_id = "ART-report-" + digest[:24]
    link = {
        "schema_version": "decision_artifact_link.v1",
        "artifact_id": artifact_id,
        "decision_id": binding["root_decision_id"],
        "artifact_kind": "report",
        "locator": "path:" + relative.as_posix(),
        "digest_algorithm": "sha256",
        "digest": digest,
        "producer": "agentteam-operator-report",
        "created_at": created_at,
    }
    ledger = DecisionLedger(work_root)
    prior = {
        item["artifact_id"]: item for item in ledger.artifact_links()
    }.get(artifact_id)
    if prior is None:
        ledger.append_artifact_link(link)
    elif any(
        prior.get(key) != link.get(key)
        for key in (
            "decision_id",
            "artifact_kind",
            "locator",
            "digest_algorithm",
            "digest",
            "producer",
        )
    ):
        raise DecisionArtifactLifecycleError(
            "operator report artifact conflicts with decision authority"
        )
    return {
        "report_artifact_id": artifact_id,
        "report_decision_id": binding["root_decision_id"],
        "report_path": str(report_path.resolve()),
        "report_locator": link["locator"],
        "report_sha256": digest,
        "report_size_bytes": len(content),
    }


def load_operator_report(metadata):
    if not isinstance(metadata, dict):
        raise DecisionArtifactLifecycleError("operator report metadata is missing")
    path = Path(metadata.get("report_path") or "")
    try:
        content = path.read_bytes()
        payload = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionArtifactLifecycleError("operator report is unreadable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != metadata.get("report_sha256"):
        raise DecisionArtifactLifecycleError("operator report digest mismatch")
    if payload.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise DecisionArtifactLifecycleError("operator report schema is invalid")
    report = payload.get("operator_report")
    if not isinstance(report, dict):
        raise DecisionArtifactLifecycleError("operator report payload is invalid")
    return report


def publish_attempt_evidence(binding, output_dir, result, *, created_at):
    if binding is None:
        return None
    decision_id = (
        result.get("acceptance_decision_id")
        or result.get("code_state_decision_id")
        or result.get("decision_id")
    )
    if not decision_id:
        raise DecisionArtifactLifecycleError(
            "decision-bound evidence has no owning decision"
        )
    payload = {
        "schema_version": "decision_attempt_evidence.v1",
        "run_id": Path(output_dir).name,
        "task_id": result.get("task_id"),
        "attempt_id": result.get("attempt_id"),
        "decision_id": decision_id,
        "validation_status": result.get("validation_status"),
        "failure_category": result.get("failure_category"),
        "semantic_validation": result.get("semantic_validation"),
        "evidence_summary": result.get("evidence_summary"),
        "evidence_level": result.get("evidence_level"),
        "evidence_status": result.get("evidence_status"),
        "trace_carrier": result.get("trace_carrier", []),
        "missing_evidence": result.get("missing_evidence", []),
        "runtime_artifacts": result.get("runtime_artifacts", []),
        "integration_status": result.get("integration_status"),
        "integration_verification_status": result.get(
            "integration_verification_status"
        ),
        "integration_verification_exit_code": result.get(
            "integration_verification_exit_code"
        ),
        "integration_verification_additions_status": result.get(
            "integration_verification_additions_status"
        ),
        "token_usage": result.get("token_usage"),
        "code_state_artifact_id": result.get("code_state_artifact_id"),
        "code_state_commit_sha": result.get("code_state_commit_sha"),
    }
    content = canonical_json_bytes(payload) + b"\n"
    digest = hashlib.sha256(content).hexdigest()
    evidence_path = (
        Path(output_dir) / "evidence" / f"decision-evidence-{digest[:24]}.json"
    )
    _publish_idempotent_bytes(evidence_path, content)
    work_root = Path(binding["work_root"]).resolve()
    relative = evidence_path.resolve().relative_to(work_root)
    artifact_id = "ART-evidence-" + digest[:24]
    link = {
        "schema_version": "decision_artifact_link.v1",
        "artifact_id": artifact_id,
        "decision_id": decision_id,
        "artifact_kind": "evidence",
        "locator": "path:" + relative.as_posix(),
        "digest_algorithm": "sha256",
        "digest": digest,
        "producer": "agentteam-verification-controller",
        "created_at": created_at,
    }
    ledger = DecisionLedger(work_root)
    prior = {
        item["artifact_id"]: item for item in ledger.artifact_links()
    }.get(artifact_id)
    if prior is None:
        ledger.append_artifact_link(link)
    elif any(
        prior.get(key) != link.get(key)
        for key in (
            "decision_id",
            "artifact_kind",
            "locator",
            "digest_algorithm",
            "digest",
            "producer",
        )
    ):
        raise DecisionArtifactLifecycleError(
            "attempt evidence conflicts with decision authority"
        )
    return {
        "evidence_artifact_id": artifact_id,
        "evidence_decision_id": decision_id,
        "evidence_path": str(evidence_path.resolve()),
        "evidence_sha256": digest,
        "evidence_size_bytes": len(content),
    }


def compact_scheduler_state(state, report_metadata):
    state["operator_report_artifact"] = deepcopy(report_metadata)
    for step in state.get("steps", []):
        result = step.get("result") if isinstance(step, dict) else None
        if not isinstance(result, dict):
            continue
        _compact_result(result, report_metadata["report_artifact_id"])
    state["artifact_compaction"] = {
        "schema_version": "decision_artifact_compaction.v1",
        "status": "compacted",
        "report_artifact_id": report_metadata["report_artifact_id"],
        "compacted_step_count": len(state.get("steps", [])),
    }
    return state


def compact_event(event, artifact_context):
    compact = deepcopy(event)
    payload = compact.get("payload")
    if not isinstance(payload, dict):
        return compact
    changed_files = payload.pop("changed_files", None)
    if isinstance(changed_files, list):
        payload["changed_file_count"] = len(changed_files)
        payload["changed_files_sha256"] = _value_sha256(sorted(changed_files))
    output = payload.pop("output", None)
    if isinstance(output, dict):
        payload["runtime_output_sha256"] = _value_sha256(output)
    diff_audit = payload.get("diff_audit")
    if isinstance(diff_audit, dict):
        payload["diff_audit"] = _compact_diff_audit(diff_audit)
    payload.pop("patch_path", None)
    code_state_event_types = {
        "runtime_output_received",
        "validation_accepted",
        "validation_rejected",
        "integration_queued",
        "integration_deferred_by_experiment_budget",
        "patch_integrated",
        "integration_blocked",
    }
    if (
        isinstance(artifact_context, dict)
        and (
            compact.get("event_type") in code_state_event_types
            or (
                compact.get("event_type") == "code_state_published"
                and payload.get("integration_code_state_artifact_id") is None
            )
        )
    ):
        for key in (
            "code_state_artifact_id",
            "code_state_base_sha",
            "code_state_commit_sha",
            "code_state_ref",
        ):
            if artifact_context.get(key) is not None:
                payload.setdefault(key, artifact_context[key])
    if (
        isinstance(artifact_context, dict)
        and compact.get("event_type")
        in {
            "validation_accepted",
            "validation_rejected",
            "integration_queued",
            "integration_verified",
            "integration_blocked",
        }
    ):
        for key in (
            "evidence_artifact_id",
            "evidence_decision_id",
            "evidence_sha256",
        ):
            if artifact_context.get(key) is not None:
                payload.setdefault(key, artifact_context[key])
    return compact


def apply_terminal_retention(
    binding,
    output_dir,
    state,
    *,
    project_root=None,
    raw_log_limit=DEFAULT_RAW_LOG_RETENTION_BYTES,
):
    if binding is None:
        return None
    existing = state.get("artifact_retention")
    if isinstance(existing, dict) and existing.get("status") == "applied":
        return existing
    output_dir = Path(output_dir).resolve()
    protected_spools, compatibility_evidence = (
        _phase1_compatibility_evidence(binding, output_dir)
    )
    raw_logs = []
    for terminal_path in sorted(output_dir.rglob("model_invocations/*/terminal.json")):
        terminal = _read_json(terminal_path)
        if not isinstance(terminal, dict):
            continue
        invocation_dir = terminal_path.parent
        for name in ("stdout.jsonl", "stderr.log"):
            path = invocation_dir / name
            if not path.is_file() or path.is_symlink():
                continue
            original = path.read_bytes()
            action = "retained"
            retained = original
            if path.resolve() in protected_spools:
                if len(original) > MAX_PROTECTED_RAW_SPOOL_BYTES:
                    raise DecisionArtifactLifecycleError(
                        "protected Phase 1 provider spool exceeds 1 MiB"
                    )
                action = "retained_evidence"
            elif name == "stderr.log" and not original:
                path.unlink()
                action = "removed_empty"
                retained = b""
            elif len(original) > raw_log_limit:
                retained = _bounded_log_tail(
                    original,
                    raw_log_limit,
                    jsonl=name.endswith(".jsonl"),
                )
                path.write_bytes(retained)
                action = "truncated"
            raw_logs.append(
                {
                    "path": str(path.relative_to(output_dir)),
                    "terminal_status": terminal.get("terminal_status"),
                    "action": action,
                    "original_size_bytes": len(original),
                    "original_sha256": hashlib.sha256(original).hexdigest(),
                    "retained_size_bytes": len(retained),
                    "retained_sha256": hashlib.sha256(retained).hexdigest(),
                }
            )
    removed_refs = []
    if project_root is not None:
        project_root = Path(project_root).resolve()
        linked_commits = {
            item["digest"]
            for item in DecisionLedger(binding["work_root"]).artifact_links()
            if item["artifact_kind"] == "code_state"
        }
        prefixes = ("refs/agentteam/export/", run_code_state_ref_prefix(output_dir.name))
        refs = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                *prefixes,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.splitlines()
        for line in refs:
            ref, _, commit_sha = line.partition(" ")
            is_export_ref = ref.startswith("refs/agentteam/export/")
            if (is_export_ref and commit_sha in linked_commits) or (
                not is_export_ref and commit_sha not in linked_commits
            ):
                subprocess.run(
                    ["git", "-C", str(project_root), "update-ref", "-d", ref],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                removed_refs.append(ref)
    removed_rebuildable = _remove_rebuildable_terminal_files(output_dir)
    retention = {
        "schema_version": "decision_artifact_retention.v1",
        "status": "applied",
        "raw_log_limit_bytes": raw_log_limit,
        "raw_logs": raw_logs,
        "compatibility_evidence": compatibility_evidence,
        "removed_internal_refs": sorted(removed_refs),
        "removed_rebuildable_files": removed_rebuildable,
    }
    state["artifact_retention"] = retention
    return retention


def _phase1_compatibility_evidence(binding, output_dir):
    artifact_path = output_dir / PHASE1_ACCEPTANCE_RELATIVE
    if not artifact_path.exists():
        return set(), []
    artifact = _read_json(artifact_path)
    if (
        not isinstance(artifact, dict)
        or artifact.get("schema_version") != "model_invocation_live_smoke.v1"
        or artifact.get("controller_validation_status") != "passed"
    ):
        raise DecisionArtifactLifecycleError(
            "Phase 1 acceptance authority is not a passed v1 artifact"
        )
    relative_spool = Path(str(artifact.get("bounded_raw_spool_path") or ""))
    if (
        not relative_spool.parts
        or relative_spool.is_absolute()
        or ".." in relative_spool.parts
    ):
        raise DecisionArtifactLifecycleError(
            "Phase 1 acceptance raw spool path is unsafe"
        )
    spool_candidate = output_dir / relative_spool
    if spool_candidate.is_symlink():
        raise DecisionArtifactLifecycleError(
            "Phase 1 acceptance raw spool is a symlink"
        )
    spool_path = spool_candidate.resolve()
    try:
        spool_path.relative_to(output_dir)
    except ValueError as exc:
        raise DecisionArtifactLifecycleError(
            "Phase 1 acceptance raw spool escapes its run"
        ) from exc
    if (
        not spool_path.is_file()
        or spool_path.stat().st_size > MAX_PROTECTED_RAW_SPOOL_BYTES
    ):
        raise DecisionArtifactLifecycleError(
            "Phase 1 acceptance raw spool is unavailable or oversized"
        )

    content = artifact_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    work_root = Path(binding["work_root"]).resolve()
    try:
        locator_path = artifact_path.resolve().relative_to(work_root)
    except ValueError as exc:
        raise DecisionArtifactLifecycleError(
            "Phase 1 acceptance evidence must be inside the project work root"
        ) from exc
    artifact_id = "ART-evidence-phase1-" + digest[:24]
    link = {
        "schema_version": "decision_artifact_link.v1",
        "artifact_id": artifact_id,
        "decision_id": binding["root_decision_id"],
        "artifact_kind": "evidence",
        "locator": "path:" + locator_path.as_posix(),
        "digest_algorithm": "sha256",
        "digest": digest,
        "producer": "phase1-usage-acceptance-controller",
        "created_at": artifact.get("finished_at"),
    }
    DecisionLedger(work_root).append_artifact_link(link)
    return {spool_path}, [
        {
            "artifact_id": artifact_id,
            "decision_id": binding["root_decision_id"],
            "artifact_path": str(artifact_path.resolve()),
            "artifact_sha256": digest,
            "raw_spool_path": str(spool_path),
            "raw_spool_size_bytes": spool_path.stat().st_size,
            "retention_status": "retained_evidence",
        }
    ]


def artifact_cost_snapshot(run_dir):
    run_dir = Path(run_dir)
    files = [
        path
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and _is_retained_cost_file(run_dir, path)
    ]
    duplicate_units = []
    for path in files:
        if path.suffix in {".patch", ".diff"}:
            duplicate_units.append(("patch", path.stat().st_size))
        if path.name in {"stdout.jsonl", "stderr.log"}:
            duplicate_units.append(("raw_log", path.stat().st_size))
    for path in files:
        if path.suffix not in {".json", ".jsonl"}:
            continue
        if path.name.startswith("decision-report-"):
            continue
        for value in _json_values(path):
            for item_dict in _walk_dicts(value):
                for key in (
                    "changed_files",
                    "operator_report",
                    "runtime_output",
                    "patch_path",
                ):
                    item = item_dict.get(key)
                    if item not in (None, [], {}):
                        duplicate_units.append(
                            (key, len(canonical_json_bytes(item)))
                        )
    return {
        "measurement_scope": "durable_run_files_excluding_disposable_worktrees",
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "redundant_unit_count": len(duplicate_units),
        "redundant_bytes": sum(item[1] for item in duplicate_units),
        "redundant_by_kind": _cost_by_kind(duplicate_units),
    }


def retire_patch(path):
    if not path:
        return {"patch_retention_status": "not_created"}
    path = Path(path)
    size = path.stat().st_size if path.is_file() else 0
    path.unlink(missing_ok=True)
    parent = path.parent
    try:
        parent.rmdir()
    except OSError:
        pass
    return {
        "patch_path": None,
        "patch_retention_status": "replaced_by_git_code_state",
        "retired_patch_bytes": size,
    }


def retire_transport(path):
    if not path:
        return {"transport_retention_status": "not_present"}
    path = Path(path)
    size = path.stat().st_size if path.is_file() else 0
    path.unlink(missing_ok=True)
    return {
        "transport_retention_status": "retired_after_canonicalization",
        "retired_transport_bytes": size,
    }


def _compact_result(result, report_artifact_id):
    changed_files = result.pop("changed_files", None)
    if isinstance(changed_files, list):
        result["changed_file_count"] = len(changed_files)
        result["changed_files_sha256"] = _value_sha256(sorted(changed_files))
    runtime_output = result.pop("runtime_output", None)
    if isinstance(runtime_output, dict):
        result["runtime_output_sha256"] = _value_sha256(runtime_output)
    diff_audit = result.get("diff_audit")
    if isinstance(diff_audit, dict):
        result["diff_audit"] = _compact_diff_audit(diff_audit)
    result["patch_path"] = None
    result["prose_report_artifact_id"] = report_artifact_id


def _compact_diff_audit(diff_audit):
    compact = {
        "diff_status": diff_audit.get("diff_status"),
    }
    for key, value in diff_audit.items():
        if not isinstance(value, list):
            continue
        compact[key.replace("_files", "_file_count")] = len(value)
        compact[key + "_sha256"] = _value_sha256(sorted(value))
    runtime_digests = diff_audit.get("runtime_artifact_digests")
    if isinstance(runtime_digests, dict):
        compact["runtime_artifact_count"] = len(runtime_digests)
        compact["runtime_artifact_digests_sha256"] = _value_sha256(runtime_digests)
    return compact


def _value_sha256(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _publish_idempotent_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise DecisionArtifactLifecycleError(
                "operator report path conflicts with existing content"
            )
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _bounded_log_tail(content, limit, *, jsonl):
    retained = content[-limit:]
    if jsonl and len(content) > limit:
        search = retained[:-1] if retained.endswith(b"\n") else retained
        newline = search.find(b"\n")
        if newline >= 0:
            retained = retained[newline + 1 :]
    return retained


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _json_values(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    lines = text.splitlines()
    values = []
    for line in lines:
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            if len(lines) == 1:
                return []
    return values


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _cost_by_kind(units):
    result = {}
    for kind, size in units:
        item = result.setdefault(kind, {"count": 0, "bytes": 0})
        item["count"] += 1
        item["bytes"] += size
    return result


def _is_retained_cost_file(run_dir, path):
    relative = path.relative_to(run_dir)
    parts = relative.parts
    if ".git" in parts or "worktrees" in parts:
        return False
    if parts and parts[0] in {"integration", "integration-baseline"}:
        return False
    if parts and parts[0] == "integration_batches" and "worktree" in parts:
        return False
    return True


def _remove_rebuildable_terminal_files(output_dir):
    candidates = []
    for directory in ("role_contexts", "repo_contexts", "codex_results"):
        root = output_dir / directory
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*") if path.is_file())
    steps_root = output_dir / "steps"
    if steps_root.is_dir():
        candidates.extend(
            path
            for path in steps_root.rglob("*")
            if path.is_file()
            and (
                path.name in {"backlog.json", "inbox.jsonl", "outbox.jsonl"}
                or path.suffix in {".patch", ".diff"}
            )
        )
    removed = []
    for path in sorted(set(candidates)):
        if path.is_symlink():
            continue
        path.unlink(missing_ok=True)
        removed.append(str(path.relative_to(output_dir)))
    for path in sorted(
        (candidate for candidate in output_dir.rglob("*") if candidate.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
    return removed

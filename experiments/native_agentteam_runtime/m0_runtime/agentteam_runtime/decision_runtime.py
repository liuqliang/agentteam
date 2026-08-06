"""Runtime binding between frozen taskpacks and decision authority."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

from .decision_ledger import (
    DecisionLedger,
    DecisionLedgerError,
    validate_decision_record,
)
from .experiment_contract import canonical_json_bytes


DECISION_CONTRACT_SCHEMA_VERSION = "taskpack_decision_contract.v1"
RUN_DECISION_BINDING_SCHEMA_VERSION = "run_decision_binding.v1"
RUN_DECISION_BINDING_PATH = Path("state/decision-binding.v1.json")


class DecisionRuntimeError(RuntimeError):
    """Raised when runtime decision inheritance cannot be proven."""


def validate_taskpack_decision_contract(contract, *, task_ids=None):
    if not isinstance(contract, dict):
        raise DecisionRuntimeError("decision_contract must be an object")
    required = {
        "schema_version",
        "root_decision_id",
        "decisions",
        "task_bindings",
    }
    if set(contract) != required:
        raise DecisionRuntimeError(
            "decision_contract fields must be exactly: "
            + ", ".join(sorted(required))
        )
    if contract["schema_version"] != DECISION_CONTRACT_SCHEMA_VERSION:
        raise DecisionRuntimeError(
            "decision_contract.schema_version must be "
            + DECISION_CONTRACT_SCHEMA_VERSION
        )
    decisions = contract["decisions"]
    if not isinstance(decisions, list) or not decisions:
        raise DecisionRuntimeError("decision_contract.decisions must be non-empty")
    if len(decisions) > 256:
        raise DecisionRuntimeError("decision_contract.decisions is too large")
    by_id = {}
    for record in decisions:
        try:
            validate_decision_record(record)
        except DecisionLedgerError as exc:
            raise DecisionRuntimeError(str(exc)) from exc
        decision_id = record["decision_id"]
        if decision_id in by_id:
            raise DecisionRuntimeError(
                f"decision_contract contains duplicate decision: {decision_id}"
            )
        if record["revision"] != 1 or record["status"] != "active":
            raise DecisionRuntimeError(
                "taskpack decisions must contain active first revisions"
            )
        if record["decision_kind"] == "acceptance":
            raise DecisionRuntimeError(
                "acceptance decisions must be created by the runtime controller"
            )
        if record["created_by"] == "agent-scheduler":
            raise DecisionRuntimeError(
                "scheduler cannot author taskpack direction or execution decisions"
            )
        by_id[decision_id] = record
    root_id = contract["root_decision_id"]
    root = by_id.get(root_id)
    if root is None:
        raise DecisionRuntimeError("root_decision_id is not declared")
    if root["decision_kind"] != "direction" or root["parent_decision_id"] is not None:
        raise DecisionRuntimeError(
            "root decision must be a parentless direction decision"
        )
    for record in decisions:
        parent = record["parent_decision_id"]
        if record["decision_id"] != root_id and parent not in by_id:
            raise DecisionRuntimeError(
                f"decision parent is outside the taskpack contract: {parent}"
            )
        current = record
        seen = set()
        while current["decision_id"] != root_id:
            current_id = current["decision_id"]
            if current_id in seen:
                raise DecisionRuntimeError("decision contract parent graph is cyclic")
            seen.add(current_id)
            parent_id = current["parent_decision_id"]
            if parent_id is None:
                raise DecisionRuntimeError(
                    "every taskpack decision must descend from root_decision_id"
                )
            current = by_id[parent_id]
    bindings = contract["task_bindings"]
    if not isinstance(bindings, dict):
        raise DecisionRuntimeError("decision_contract.task_bindings must be an object")
    known_tasks = set(task_ids or [])
    if known_tasks and set(bindings).difference(known_tasks):
        raise DecisionRuntimeError(
            "decision_contract.task_bindings contains an unknown task"
        )
    for task_id, decision_id in bindings.items():
        if not isinstance(task_id, str) or not task_id:
            raise DecisionRuntimeError("task binding key must be a non-empty task id")
        if decision_id not in by_id:
            raise DecisionRuntimeError(
                f"task binding references an unknown decision: {decision_id}"
            )
    return deepcopy(contract)


def publish_run_decision_binding(
    work_root,
    frozen_taskpack_dir,
    run_dir,
    taskpack,
    *,
    task_ids,
):
    contract = taskpack.get("decision_contract")
    if contract is None:
        return None
    contract = validate_taskpack_decision_contract(contract, task_ids=task_ids)
    work_root = Path(work_root).resolve()
    frozen_taskpack_dir = Path(frozen_taskpack_dir).resolve()
    run_dir = Path(run_dir).resolve()
    try:
        manifest_relative = (
            frozen_taskpack_dir / "manifest.json"
        ).relative_to(work_root)
    except ValueError as exc:
        raise DecisionRuntimeError(
            "decision-bound frozen taskpack must be inside the project work root"
        ) from exc
    manifest_path = frozen_taskpack_dir / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionRuntimeError("frozen taskpack manifest is unreadable") from exc
    taskpack_digest = manifest.get("digest_sha256")
    if not isinstance(taskpack_digest, str) or len(taskpack_digest) != 64:
        raise DecisionRuntimeError("frozen taskpack manifest digest is invalid")

    ledger = DecisionLedger.create(work_root)
    pending = {record["decision_id"]: record for record in contract["decisions"]}
    while pending:
        published = False
        known = {item["decision_id"] for item in ledger.latest_decisions()}
        for decision_id, record in list(pending.items()):
            parent = record["parent_decision_id"]
            if parent is None or parent in known:
                ledger.append_decision(record)
                del pending[decision_id]
                published = True
        if not published:
            raise DecisionRuntimeError("decision contract parent graph is cyclic")
    root = contract["root_decision_id"]
    artifact_id = "ART-taskpack-" + taskpack_digest[:24]
    ledger.append_artifact_link(
        {
            "schema_version": "decision_artifact_link.v1",
            "artifact_id": artifact_id,
            "decision_id": root,
            "artifact_kind": "contract",
            "locator": "path:" + manifest_relative.as_posix(),
            "digest_algorithm": "sha256",
            "digest": hashlib.sha256(manifest_bytes).hexdigest(),
            "producer": "taskpack-freeze",
            "created_at": _root_created_at(contract),
        }
    )
    binding = {
        "schema_version": RUN_DECISION_BINDING_SCHEMA_VERSION,
        "taskpack_id": taskpack["taskpack_id"],
        "taskpack_digest_sha256": taskpack_digest,
        "work_root": str(work_root),
        "root_decision_id": root,
        "task_bindings": deepcopy(contract["task_bindings"]),
        "contract_artifact_id": artifact_id,
        "contract_sha256": hashlib.sha256(
            canonical_json_bytes(contract)
        ).hexdigest(),
    }
    path = run_dir / RUN_DECISION_BINDING_PATH
    _publish_idempotent_json(path, binding)
    return binding


def load_run_decision_binding(run_dir):
    path = Path(run_dir).resolve() / RUN_DECISION_BINDING_PATH
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise DecisionRuntimeError("run decision binding is unsafe")
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionRuntimeError("run decision binding is unreadable") from exc
    required = {
        "schema_version",
        "taskpack_id",
        "taskpack_digest_sha256",
        "work_root",
        "root_decision_id",
        "task_bindings",
        "contract_artifact_id",
        "contract_sha256",
    }
    if not isinstance(binding, dict) or set(binding) != required:
        raise DecisionRuntimeError("run decision binding shape is invalid")
    if binding["schema_version"] != RUN_DECISION_BINDING_SCHEMA_VERSION:
        raise DecisionRuntimeError("run decision binding schema is invalid")
    for field in ("taskpack_digest_sha256", "contract_sha256"):
        value = binding[field]
        if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise DecisionRuntimeError(f"run decision binding {field} is invalid")
    if not isinstance(binding["task_bindings"], dict):
        raise DecisionRuntimeError("run decision task bindings are invalid")
    work_root = Path(binding["work_root"]).resolve()
    try:
        Path(run_dir).resolve().relative_to(work_root)
    except ValueError as exc:
        raise DecisionRuntimeError(
            "run decision binding work root does not contain the run"
        ) from exc
    ledger = DecisionLedger(work_root)
    active = {
        item["decision_id"]: item
        for item in ledger.latest_decisions(statuses={"active"})
    }
    inherited = {binding["root_decision_id"], *binding["task_bindings"].values()}
    missing = sorted(inherited.difference(active))
    if missing:
        raise DecisionRuntimeError(
            "run decision binding references inactive decisions: "
            + ", ".join(missing)
        )
    return binding


def inherited_decision_id(binding, task_id=None):
    if binding is None:
        return None
    if task_id is not None:
        return binding["task_bindings"].get(task_id, binding["root_decision_id"])
    return binding["root_decision_id"]


def require_active_inherited_decision(binding, task_id=None):
    decision_id = inherited_decision_id(binding, task_id)
    if decision_id is None:
        return None
    active = {
        item["decision_id"]
        for item in DecisionLedger(binding["work_root"]).latest_decisions(
            statuses={"active"}
        )
    }
    if decision_id not in active:
        raise DecisionRuntimeError(
            f"inherited decision is no longer active: {decision_id}"
        )
    return decision_id


def record_integration_acceptance(
    binding,
    *,
    run_id,
    task_id,
    attempt_id,
    created_at,
    evidence_refs,
):
    if binding is None:
        return None
    parent = require_active_inherited_decision(binding, task_id)
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "run_id": run_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "parent_decision_id": parent,
            }
        )
    ).hexdigest()[:24]
    decision_id = "DEC-accept-" + identity
    ledger = DecisionLedger(binding["work_root"])
    existing = {
        item["decision_id"]: item for item in ledger.latest_decisions()
    }.get(decision_id)
    if existing is not None:
        if (
            existing["decision_kind"] != "acceptance"
            or existing["parent_decision_id"] != parent
            or existing["subject"] != "integration"
        ):
            raise DecisionRuntimeError(
                "existing acceptance decision conflicts with attempt identity"
            )
        return existing
    refs = sorted(
        {
            str(item)[:500]
            for item in evidence_refs
            if isinstance(item, str) and item
        }
    )[:64]
    record = {
        "schema_version": "decision_record.v1",
        "decision_id": decision_id,
        "revision": 1,
        "decision_kind": "acceptance",
        "subject": "integration",
        "authority_level": "L2",
        "parent_decision_id": parent,
        "supersedes_decision_id": None,
        "statement": f"Admit validated attempt {attempt_id} to integration.",
        "selected_option": "admit_to_integration",
        "alternatives": [
            {
                "option": "reject_before_integration",
                "rejected_reason": (
                    "Runtime validation accepted the result and required "
                    "evidence was complete."
                ),
            }
        ],
        "rationale": (
            "The integration controller observed accepted validation and no "
            "blocking evidence gap."
        ),
        "scope": [f"run:{run_id}", f"task:{task_id}", f"attempt:{attempt_id}"],
        "expected_outcome": (
            "The attempt enters integration application and verification."
        ),
        "acceptance_refs": refs or [f"attempt:{attempt_id}:validation_accepted"],
        "status": "completed",
        "created_at": created_at,
        "created_by": "verification-integration-controller",
        "previous_revision_sha256": None,
    }
    ledger.append_decision(record)
    return record


def _root_created_at(contract):
    root_id = contract["root_decision_id"]
    return next(
        record["created_at"]
        for record in contract["decisions"]
        if record["decision_id"] == root_id
    )


def _publish_idempotent_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    if path.exists():
        if path.is_symlink() or path.read_text(encoding="utf-8") != encoded:
            raise DecisionRuntimeError("run decision binding conflicts with existing state")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())

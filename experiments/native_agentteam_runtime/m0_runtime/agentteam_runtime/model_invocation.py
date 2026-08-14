"""Durable lifecycle authority for one model-provider invocation.

The supported Linux path deliberately separates creating the execution group
from permitting the provider launch:

1. systemd creates a transient service containing a gated supervisor;
2. the parent verifies the supervisor and service identities;
3. ``started.json`` is durably and exclusively published; and only then
4. the parent sends the nonce-bearing launch permit.

Test provider commands use the same lifecycle and terminalization primitives,
but are classified as ``not_applicable_adapter`` and do not claim systemd
execution-group authority.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .model_context_budget import (
        CONTEXT_BUDGET_POLICY_FIELDS,
        HOOK_TRUST_BYPASS_OPTION,
        LEGACY_CONTEXT_POLICY_FIELDS,
        codex_context_policy_arguments,
        normalize_context_budget_policy,
    )
else:  # The systemd supervisor executes this module by its file path.
    from model_context_budget import (
        CONTEXT_BUDGET_POLICY_FIELDS,
        HOOK_TRUST_BYPASS_OPTION,
        LEGACY_CONTEXT_POLICY_FIELDS,
        codex_context_policy_arguments,
        normalize_context_budget_policy,
    )


MAX_PROVIDER_STREAM_BYTES = 4 * 1024 * 1024
HANDSHAKE_TIMEOUT_SECONDS = 30.0
SYSTEMD_IDENTITY_TIMEOUT_SECONDS = 10.0
PRELAUNCH_SOURCE_REVALIDATION_TIMEOUT_SECONDS = 120.0
PROVIDER_LANE_ADMISSION_RETRY_SECONDS = 0.25
PROVIDER_LANE_ADMISSION_RETRY_INTERVAL_SECONDS = 0.01
PARENT_DEATH_SIGNAL = signal.SIGKILL
CANONICAL_LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "model_invocation_started",
        "model_invocation_writer_revoked",
        "model_invocation_usage_recorded",
    }
)
_CANONICAL_SOURCE_METADATA_FIELDS = frozenset(
    {
        "_source_artifact_path",
        "_source_record_sha256",
    }
)
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
_CONTROLLER_USAGE_STAGES = frozenset(
    {
        "runtime_diagnostic",
        "development_smoke",
    }
)


class ModelInvocationError(RuntimeError):
    """Base error for lifecycle integrity and launch failures."""


class ModelInvocationUnavailable(ModelInvocationError):
    """The supported live execution authority is unavailable."""


class ModelInvocationIntegrityError(ModelInvocationError):
    """Immutable lifecycle evidence conflicts with an existing record."""


class ModelInvocationWriterRevoked(ModelInvocationError):
    """The lifecycle owner no longer has terminal-write authority."""


def append_canonical_events(
    events_path,
    events,
    *,
    run_id=None,
    step_id=None,
    detect_conflicts=False,
):
    """Append canonical events under one file lock without rewriting history.

    Ordinary scheduler events retain the historical "first idempotency key
    wins" behavior.  Lifecycle import opts into content comparison so a
    duplicate immutable source ID is accepted only when its source record is
    byte-equivalent.
    """
    events_path = Path(events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            existing = _read_canonical_events(events_path)
            existing_by_key = {}
            for event in existing:
                key = event.get("idempotency_key")
                if not isinstance(key, str) or not key:
                    continue
                prior = existing_by_key.get(key)
                if (
                    detect_conflicts
                    and prior is not None
                    and not _canonical_events_are_equivalent(prior, event)
                ):
                    raise ModelInvocationIntegrityError(
                        f"conflicting canonical idempotency key: {key}"
                    )
                existing_by_key.setdefault(key, event)

            sequence = max(
                (
                    event.get("sequence", 0)
                    for event in existing
                    if isinstance(event.get("sequence"), int)
                ),
                default=0,
            ) + 1
            appended = []
            for event in events:
                candidate = dict(event)
                key = candidate.get("idempotency_key")
                if not isinstance(key, str) or not key:
                    raise ModelInvocationIntegrityError(
                        "canonical event requires a nonempty idempotency key"
                    )
                prior = existing_by_key.get(key)
                if prior is not None:
                    if (
                        detect_conflicts
                        and not _canonical_events_are_equivalent(
                            prior,
                            candidate,
                        )
                    ):
                        raise ModelInvocationIntegrityError(
                            f"conflicting canonical idempotency key: {key}"
                        )
                    continue
                canonical = {
                    **candidate,
                    "event_id": f"EVT-{sequence:04d}",
                    "sequence": sequence,
                    "run_id": candidate.get("run_id", run_id),
                    "step_id": candidate.get("step_id", step_id),
                }
                appended.append(canonical)
                existing_by_key[key] = canonical
                sequence += 1
            _append_canonical_event_bytes(events_path, appended)
            return appended
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def import_model_invocation_lifecycle(
    events_path,
    started_path,
    *,
    revoked_path=None,
    terminal_path=None,
    actor="agent-scheduler",
    run_id=None,
    step_id=None,
    source_root=None,
    require_terminal=False,
):
    """Import one immutable lifecycle into a canonical append-only event log."""
    started_path = Path(started_path)
    invocation_dir = started_path.parent
    revoked_path = Path(revoked_path or invocation_dir / "revoked.json")
    terminal_path = Path(terminal_path or invocation_dir / "terminal.json")
    start, start_digest = _read_lifecycle_record(started_path, "start")
    invocation_id = _required_record_text(start, "invocation_id", "start")
    if start.get("start_schema_version") != "model_invocation_started.v1":
        raise ModelInvocationIntegrityError(
            f"unsupported start record schema for {invocation_id}"
        )

    records = [
        _canonical_lifecycle_event(
            "model_invocation_started",
            start,
            start_digest,
            started_path,
            actor=actor,
            run_id=run_id,
            step_id=step_id,
            source_root=source_root,
        )
    ]
    if revoked_path.is_file():
        revocation, revocation_digest = _read_lifecycle_record(
            revoked_path,
            "writer revocation",
        )
        if revocation.get("invocation_id") != invocation_id:
            raise ModelInvocationIntegrityError(
                f"writer revocation invocation mismatch for {invocation_id}"
            )
        if (
            revocation.get("revocation_schema_version")
            != "model_invocation_writer_revoked.v1"
        ):
            raise ModelInvocationIntegrityError(
                f"unsupported writer revocation schema for {invocation_id}"
            )
        _required_record_text(
            revocation,
            "lifecycle_owner_token",
            "writer revocation",
        )
        records.append(
            _canonical_lifecycle_event(
                "model_invocation_writer_revoked",
                revocation,
                revocation_digest,
                revoked_path,
                actor=actor,
                run_id=run_id,
                step_id=step_id,
                source_root=source_root,
            )
        )
    if terminal_path.is_file():
        terminal, terminal_digest = _read_lifecycle_record(
            terminal_path,
            "terminal usage",
        )
        if terminal.get("invocation_id") != invocation_id:
            raise ModelInvocationIntegrityError(
                f"terminal invocation mismatch for {invocation_id}"
            )
        if terminal.get("usage_schema_version") != "model_invocation_usage.v1":
            raise ModelInvocationIntegrityError(
                f"unsupported terminal usage schema for {invocation_id}"
            )
        _required_record_text(terminal, "usage_event_id", "terminal usage")
        records.append(
            _canonical_lifecycle_event(
                "model_invocation_usage_recorded",
                terminal,
                terminal_digest,
                terminal_path,
                actor=actor,
                run_id=run_id,
                step_id=step_id,
                source_root=source_root,
            )
        )
    elif require_terminal:
        raise ModelInvocationIntegrityError(
            f"required terminal usage record is missing for {invocation_id}"
        )

    return append_canonical_events(
        events_path,
        records,
        run_id=run_id or start.get("run_id"),
        step_id=step_id,
        detect_conflicts=True,
    )


def import_author_lifecycle_bootstrap(run_dir):
    """Validate and import the successful author lifecycle bootstrap."""
    run_dir = Path(run_dir).resolve()
    bootstrap_path = (
        run_dir / "state" / "author_lifecycle_bootstrap.v1.json"
    )
    if not bootstrap_path.is_file():
        return []
    bootstrap = _read_json_record(bootstrap_path, "author bootstrap")
    if (
        bootstrap.get("bootstrap_schema_version")
        != "author_lifecycle_bootstrap.v1"
    ):
        raise ModelInvocationIntegrityError(
            "unsupported author lifecycle bootstrap schema"
        )
    work_root = _work_root_for_run(run_dir)
    started_path = _contained_bootstrap_path(
        work_root,
        bootstrap.get("started_path"),
        "started_path",
    )
    terminal_path = _contained_bootstrap_path(
        work_root,
        bootstrap.get("terminal_path"),
        "terminal_path",
    )
    _validate_file_digest(
        started_path,
        bootstrap.get("started_sha256"),
        "author start",
    )
    _validate_file_digest(
        terminal_path,
        bootstrap.get("terminal_sha256"),
        "author terminal",
    )
    start = _read_json_record(started_path, "author start")
    terminal = _read_json_record(terminal_path, "author terminal")
    invocation_id = _required_record_text(
        bootstrap,
        "invocation_id",
        "author bootstrap",
    )
    usage_event_id = _required_record_text(
        bootstrap,
        "usage_event_id",
        "author bootstrap",
    )
    if (
        start.get("invocation_id") != invocation_id
        or terminal.get("invocation_id") != invocation_id
        or terminal.get("usage_event_id") != usage_event_id
    ):
        raise ModelInvocationIntegrityError(
            "author bootstrap source IDs do not match immutable records"
        )
    if terminal.get("terminal_status") != "completed":
        raise ModelInvocationIntegrityError(
            "author bootstrap must reference a successful terminal record"
        )
    author_context_path = _contained_bootstrap_path(
        work_root,
        bootstrap.get("author_context_path"),
        "author_context_path",
        require_file=False,
    )
    if started_path.parents[2] != author_context_path:
        raise ModelInvocationIntegrityError(
            "author bootstrap context does not contain the start record"
        )
    return import_model_invocation_lifecycle(
        run_dir / "events.jsonl",
        started_path,
        terminal_path=terminal_path,
        actor="taskpack-author-importer",
        run_id=start.get("run_id"),
        step_id="STEP-AUTHOR-BOOTSTRAP",
        source_root=work_root,
        require_terminal=True,
    )


def import_registered_controller_lifecycles(run_dir):
    """Import fixed-root diagnostic and development-smoke controller records."""
    run_dir = Path(run_dir).resolve()
    controller_root = (
        run_dir / "state" / "controller_invocations"
    ).resolve()
    if not controller_root.is_dir():
        return []
    imported = []
    for claim_path in sorted(
        controller_root.glob("*/*/controller_claim.json")
    ):
        claim_dir = claim_path.parent.resolve()
        if not _is_relative_to(claim_dir, controller_root):
            raise ModelInvocationIntegrityError(
                f"controller claim escapes registered root: {claim_path}"
            )
        claim = _read_json_record(claim_path, "controller claim")
        if (
            claim.get("claim_schema_version")
            != "model_invocation_controller_claim.v1"
        ):
            raise ModelInvocationIntegrityError(
                f"unsupported controller claim schema: {claim_path}"
            )
        stage = _required_record_text(
            claim,
            "usage_stage",
            "controller claim",
        )
        if stage not in _CONTROLLER_USAGE_STAGES:
            raise ModelInvocationIntegrityError(
                f"unregistered controller usage stage: {stage}"
            )
        if claim_path.parents[1].name != stage:
            raise ModelInvocationIntegrityError(
                f"controller claim stage path mismatch: {claim_path}"
            )
        claimed_root = Path(
            _required_record_text(
                claim,
                "authority_root",
                "controller claim",
            )
        ).resolve()
        if claimed_root != claim_dir:
            raise ModelInvocationIntegrityError(
                f"controller claim authority root mismatch: {claim_path}"
            )
        for started_path in sorted(
            claim_dir.glob("model_invocations/*/started.json")
        ):
            start = _read_json_record(started_path, "controller start")
            for field in (
                "project",
                "run_id",
                "taskpack_id",
                "usage_stage",
                "runtime_execution_session_id",
                "lifecycle_owner_token",
            ):
                if start.get(field) != claim.get(field):
                    raise ModelInvocationIntegrityError(
                        f"controller claim {field} mismatch: {started_path}"
                    )
            imported.extend(
                import_model_invocation_lifecycle(
                    run_dir / "events.jsonl",
                    started_path,
                    actor=f"{stage}-controller-importer",
                    run_id=start.get("run_id"),
                    step_id="STEP-CONTROLLER",
                    source_root=run_dir,
                )
            )
    return imported


def replay_model_invocation_events(events):
    """Project unique lifecycle authority and deterministic usage accounting."""
    records = _coerce_canonical_events(events)
    seen_event_ids = {}
    starts = {}
    revocations = {}
    usages = {}
    for event in records:
        event_type = event.get("event_type")
        if event_type not in CANONICAL_LIFECYCLE_EVENT_TYPES:
            continue
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            prior_event = seen_event_ids.get(event_id)
            if (
                prior_event is not None
                and not _canonical_events_are_equivalent(prior_event, event)
            ):
                raise ModelInvocationIntegrityError(
                    f"conflicting canonical event ID: {event_id}"
                )
            if prior_event is not None:
                continue
            seen_event_ids[event_id] = event
        record = _canonical_source_record(event.get("payload"))
        if not isinstance(record, dict):
            raise ModelInvocationIntegrityError(
                f"{event_type} payload is not an immutable record"
            )
        invocation_id = _required_record_text(
            record,
            "invocation_id",
            event_type,
        )
        if event_type == "model_invocation_started":
            _insert_replayed_record(
                starts,
                invocation_id,
                record,
                "invocation start",
            )
        elif event_type == "model_invocation_writer_revoked":
            owner_token = _required_record_text(
                record,
                "lifecycle_owner_token",
                "writer revocation",
            )
            _insert_replayed_record(
                revocations,
                (invocation_id, owner_token),
                record,
                "writer revocation",
            )
        else:
            usage_event_id = _required_record_text(
                record,
                "usage_event_id",
                "terminal usage",
            )
            _insert_replayed_record(
                usages,
                usage_event_id,
                record,
                "terminal usage",
            )

    usage_by_invocation = {}
    for usage_event_id, usage in usages.items():
        invocation_id = usage["invocation_id"]
        if invocation_id not in starts:
            raise ModelInvocationIntegrityError(
                f"terminal usage has no canonical start: {usage_event_id}"
            )
        prior = usage_by_invocation.get(invocation_id)
        if prior is not None and prior != usage:
            raise ModelInvocationIntegrityError(
                f"multiple terminal usage records for {invocation_id}"
            )
        usage_by_invocation[invocation_id] = usage

    reported_totals = {field: 0 for field in _TOKEN_FIELDS}
    reported_invocation_count = 0
    for usage in usage_by_invocation.values():
        if usage.get("usage_status") != "reported":
            continue
        reported_invocation_count += 1
        for field in _TOKEN_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                reported_totals[field] += value

    open_invocation_ids = sorted(set(starts) - set(usage_by_invocation))
    return {
        "invocation_count": len(starts),
        "terminal_count": len(usage_by_invocation),
        "open_invocation_ids": open_invocation_ids,
        "reported_invocation_count": reported_invocation_count,
        "reported_token_totals": reported_totals,
        "start_records": starts,
        "usage_records": usages,
        "usage_by_invocation": usage_by_invocation,
        "writer_revocations": revocations,
    }


def authoritative_provider_snapshot(events, provider_session_id):
    """Return a graph-derived unique provider predecessor, never event order."""
    if not isinstance(provider_session_id, str) or not provider_session_id:
        return None
    projection = replay_model_invocation_events(events)
    nodes = {
        record["invocation_id"]: record
        for record in projection["usage_records"].values()
        if record.get("provider_session_id") == provider_session_id
    }
    if not nodes:
        return None

    referenced = set()
    child_counts = {}
    for invocation_id, record in nodes.items():
        predecessor = record.get("provider_predecessor_invocation_id")
        predecessor_turn = record.get("provider_predecessor_turn_id")
        if predecessor is None:
            if predecessor_turn is not None:
                return _unavailable_provider_lineage(
                    "provider_predecessor_invocation_missing",
                )
            continue
        if predecessor not in nodes:
            return _unavailable_provider_lineage(
                "provider_predecessor_record_missing",
            )
        if not predecessor_turn:
            return _unavailable_provider_lineage(
                "provider_predecessor_turn_missing",
            )
        if nodes[predecessor].get("provider_turn_id") != predecessor_turn:
            return _unavailable_provider_lineage(
                "provider_predecessor_turn_mismatch",
            )
        referenced.add(predecessor)
        child_counts[predecessor] = child_counts.get(predecessor, 0) + 1
        if child_counts[predecessor] > 1:
            return _unavailable_provider_lineage(
                "provider_session_lineage_forked",
            )

    tips = sorted(set(nodes) - referenced)
    if len(tips) != 1:
        return _unavailable_provider_lineage(
            "provider_session_predecessor_ambiguous",
        )
    tip_id = tips[0]
    visited = set()
    cursor = tip_id
    while cursor is not None:
        if cursor in visited:
            return _unavailable_provider_lineage(
                "provider_session_lineage_cycle",
            )
        visited.add(cursor)
        cursor = nodes[cursor].get("provider_predecessor_invocation_id")
    if visited != set(nodes):
        return _unavailable_provider_lineage(
            "provider_session_lineage_disconnected",
        )

    tip = nodes[tip_id]
    provider_turn_id = tip.get("provider_turn_id")
    snapshot = tip.get("provider_usage_snapshot")
    if not provider_turn_id:
        return _unavailable_provider_lineage(
            "provider_turn_id_missing",
        )
    if not isinstance(snapshot, dict):
        return _unavailable_provider_lineage(
            "provider_usage_snapshot_missing",
        )
    compact_snapshot = {
        field: snapshot.get(field)
        for field in _TOKEN_FIELDS
        if isinstance(snapshot.get(field), int)
        and not isinstance(snapshot.get(field), bool)
    }
    if not compact_snapshot:
        return _unavailable_provider_lineage(
            "provider_usage_snapshot_empty",
        )
    return {
        "provider_lineage_status": "authoritative",
        "provider_predecessor_invocation_id": tip_id,
        "provider_predecessor_turn_id": provider_turn_id,
        "provider_predecessor_usage_snapshot": compact_snapshot,
        "previous_provider_session_id": provider_session_id,
        "previous_invocation_id": tip_id,
        "previous_provider_turn_id": provider_turn_id,
    }


def hydrate_provider_predecessor_context(context, events):
    """Supply a canonical predecessor for one explicit provider resume."""
    hydrated = dict(context)
    if (
        hydrated.get("provider_resume_mode") != "explicit"
        or not hydrated.get("requested_provider_session_id")
    ):
        return hydrated
    predecessor = authoritative_provider_snapshot(
        events,
        hydrated["requested_provider_session_id"],
    )
    if predecessor is None:
        return hydrated
    if predecessor.get("provider_lineage_status") != "authoritative":
        for field in (
            "provider_predecessor_invocation_id",
            "provider_predecessor_turn_id",
            "provider_predecessor_usage_snapshot",
            "previous_provider_session_id",
            "previous_invocation_id",
            "previous_provider_turn_id",
        ):
            hydrated[field] = None
        hydrated["provider_lineage_status"] = predecessor[
            "provider_lineage_status"
        ]
        hydrated["provider_lineage_error"] = predecessor.get(
            "provider_lineage_error"
        )
        return hydrated
    for field in (
        "provider_predecessor_invocation_id",
        "provider_predecessor_turn_id",
        "provider_predecessor_usage_snapshot",
    ):
        supplied = hydrated.get(field)
        if supplied is not None and supplied != predecessor[field]:
            raise ModelInvocationIntegrityError(
                f"supplied {field} conflicts with canonical provider lineage"
            )
    hydrated.update(predecessor)
    return hydrated


@dataclass(frozen=True)
class ExecutionGroupIdentity:
    gated_supervisor_pid: int | None
    gated_supervisor_pgid: int | None
    host_boot_id: str | None
    gated_supervisor_start_ticks: int | None
    launch_nonce_sha256: str | None
    systemd_linger_enabled: bool | None
    systemd_transient_unit: str | None
    systemd_transient_invocation_id: str | None
    systemd_transient_kill_mode: str | None
    systemd_user_manager_identity: str | None
    systemd_transient_control_group: str | None
    systemd_user_service_invocation_id: str | None
    systemd_user_service_control_group: str | None
    systemd_user_service_kill_mode: str | None

    @classmethod
    def not_applicable(cls):
        return cls(**{name: None for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ProviderExecution:
    command: list[str]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    launch_failed: bool = False
    launch_error: str | None = None
    resource_evidence: dict | None = None

    def completed_process(self):
        return subprocess.CompletedProcess(
            self.command,
            self.returncode if self.returncode is not None else 1,
            self.stdout,
            self.stderr,
        )


class InvocationLifecycle:
    """Own immutable start/revocation/terminal files for one invocation."""

    def __init__(
        self,
        authority_root,
        context,
        *,
        invocation_id=None,
        started_at=None,
    ):
        self.authority_root = Path(authority_root)
        self.invocation_id = invocation_id or f"INV-{uuid.uuid4().hex}"
        self.context = dict(context)
        self.started_at = started_at or _utc_now()
        self.authority_root.mkdir(parents=True, exist_ok=True)
        invocation_root = self.authority_root / "model_invocations"
        invocation_root.mkdir(parents=True, exist_ok=True)
        self.invocation_dir = (
            invocation_root / self.invocation_id
        )
        lock_path = self.authority_root / "model_invocations.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if (self.authority_root / "model_invocations.sealed.json").exists():
                    raise ModelInvocationIntegrityError(
                        "model invocation set is sealed"
                    )
                self.invocation_dir.mkdir(parents=False, exist_ok=False)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        _fsync_directory(self.invocation_dir.parent)
        self.started_path = self.invocation_dir / "started.json"
        self.revoked_path = self.invocation_dir / "revoked.json"
        self.terminal_path = self.invocation_dir / "terminal.json"
        self.terminal_lock_path = self.invocation_dir / "terminal.lock"
        self.stdout_path = self.invocation_dir / "stdout.jsonl"
        self.stderr_path = self.invocation_dir / "stderr.log"
        self.resource_path = self.invocation_dir / "resource.json"

    @property
    def is_started(self):
        return self.started_path.exists()

    def discard_before_start(self):
        """Remove a failed prelaunch allocation that never became authority."""

        lock_path = self.authority_root / "model_invocations.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if self.is_started:
                    return False
                if self.terminal_path.exists():
                    raise ModelInvocationIntegrityError(
                        "unstarted invocation unexpectedly has terminal authority"
                    )
                if self.invocation_dir.exists():
                    shutil.rmtree(self.invocation_dir)
                    _fsync_directory(self.invocation_dir.parent)
                return True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def publish_start(self, identity):
        record = {
            "start_schema_version": "model_invocation_started.v1",
            "invocation_id": self.invocation_id,
            **_start_context(self.context),
            **{
                name: getattr(identity, name)
                for name in ExecutionGroupIdentity.__dataclass_fields__
            },
            "started_at": self.started_at,
        }
        lock_path = self.authority_root / "model_invocations.lock"
        lock_path.touch(mode=0o600, exist_ok=True)
        with lock_path.open("r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if (self.authority_root / "model_invocations.sealed.json").exists():
                    raise ModelInvocationIntegrityError(
                        "model invocation set is sealed"
                    )
                _exclusive_publish_json(self.started_path, record)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return record

    def write_bounded_spools(self, stdout, stderr):
        _exclusive_or_idempotent_bytes(
            self.stdout_path,
            str(stdout or "").encode("utf-8")[:MAX_PROVIDER_STREAM_BYTES],
        )
        _exclusive_or_idempotent_bytes(
            self.stderr_path,
            str(stderr or "").encode("utf-8")[:MAX_PROVIDER_STREAM_BYTES],
        )

    def write_resource_evidence(self, evidence):
        if evidence is None:
            return None
        if not isinstance(evidence, dict):
            raise ModelInvocationIntegrityError(
                "model invocation resource evidence is invalid"
            )
        payload = dict(evidence)
        payload["invocation_id"] = self.invocation_id
        _exclusive_publish_json(self.resource_path, payload)
        return payload

    @contextmanager
    def terminal_authority(self):
        self.terminal_lock_path.touch(mode=0o600, exist_ok=True)
        with self.terminal_lock_path.open("r+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def revoke_writer(self, owner_token, *, revoked_by, reason, revoked_at=None):
        with self.terminal_authority():
            existing = _read_json_if_exists(self.revoked_path)
            if existing is not None:
                if (
                    existing.get("lifecycle_owner_token") == owner_token
                    and existing.get("revoked_by") == revoked_by
                    and existing.get("reason") == reason
                ):
                    return existing
                raise ModelInvocationIntegrityError(
                    f"conflicting writer revocation for {self.invocation_id}"
                )
            record = {
                "revocation_schema_version": (
                    "model_invocation_writer_revoked.v1"
                ),
                "invocation_id": self.invocation_id,
                "lifecycle_owner_token": owner_token,
                "revoked_by": revoked_by,
                "revoked_at": revoked_at or _utc_now(),
                "reason": reason,
            }
            _exclusive_publish_json(self.revoked_path, record)
            return record

    def finalize(
        self,
        terminal_status,
        *,
        stdout="",
        stderr="",
        terminal_writer="worker",
        finished_at=None,
    ):
        if terminal_status not in {
            "completed",
            "failed",
            "blocked",
            "cancelled",
            "timed_out",
            "launch_failed",
            "missing_result",
            "invalid_result",
            "recovered_orphan",
        }:
            raise ValueError(f"unsupported terminal_status: {terminal_status}")
        if not self.is_started:
            raise ModelInvocationIntegrityError(
                "terminal record requires a durable start record"
            )
        finished_at = finished_at or _utc_now()
        with self.terminal_authority():
            usage = _terminal_usage(
                stdout,
                self.context,
                terminal_status=terminal_status,
            )
            lineage = _provider_lineage(stdout, self.context)
            terminal_context = _terminal_context(self.context)
            if terminal_writer == "recovery_controller":
                started = _read_json_if_exists(self.started_path)
                if not isinstance(started, dict):
                    raise ModelInvocationIntegrityError(
                        "recovery terminal requires its durable start"
                    )
                terminal_context = {
                    field: started.get(field)
                    for field in terminal_context
                }
            record = {
                "usage_schema_version": "model_invocation_usage.v1",
                "usage_event_id": _usage_event_id(self.invocation_id),
                "invocation_id": self.invocation_id,
                "start_sha256": _bounded_file_sha256(self.started_path),
                **terminal_context,
                "provider_session_id": lineage["provider_session_id"],
                "provider_predecessor_invocation_id": self.context.get(
                    "provider_predecessor_invocation_id"
                ),
                "provider_turn_id": lineage["provider_turn_id"],
                "provider_predecessor_turn_id": self.context.get(
                    "provider_predecessor_turn_id"
                ),
                "terminal_writer": terminal_writer,
                "terminal_status": terminal_status,
                **usage,
                "started_at": self.started_at,
                "finished_at": finished_at,
                "wall_time_seconds": max(
                    _timestamp_seconds(finished_at)
                    - _timestamp_seconds(self.started_at),
                    0.0,
                ),
                "source_artifact_path": (
                    f"model_invocations/{self.invocation_id}/stdout.jsonl"
                ),
            }
            existing = _read_json_if_exists(self.terminal_path)
            if existing is not None:
                if existing != record:
                    raise ModelInvocationIntegrityError(
                        f"conflicting terminal record for {self.invocation_id}"
                    )
                return existing
            revoked = _read_json_if_exists(self.revoked_path)
            if (
                revoked is not None
                and revoked.get("lifecycle_owner_token")
                == self.context["lifecycle_owner_token"]
            ):
                raise ModelInvocationWriterRevoked(
                    f"terminal writer revoked for {self.invocation_id}"
                )
            self.write_bounded_spools(stdout, stderr)
            _exclusive_publish_json(self.terminal_path, record)
            return record

    def summary(self):
        return {
            "invocation_id": self.invocation_id,
            "started_path": str(self.started_path),
            "terminal_path": str(self.terminal_path),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "resource_path": str(self.resource_path),
        }


class ModelInvocationCall:
    """Shared start-before-launch and terminalization primitive."""

    def __init__(
        self,
        authority_root,
        context,
        *,
        supported,
        systemd_runner_factory=None,
    ):
        context, launch_registration = (
            _bind_registered_experiment_launch(
                authority_root,
                context,
            )
        )
        _validate_call_context(context, supported=bool(supported))
        self.lifecycle = InvocationLifecycle(authority_root, context)
        self.experiment_launch_registration = launch_registration
        self.supported = bool(supported)
        self.systemd_runner_factory = (
            systemd_runner_factory or SystemdGatedExecution
        )
        self.execution_group = None
        self.experiment_controller = None
        self.provider_admission = None

    def execute(
        self,
        command,
        *,
        cwd,
        input_text,
        timeout_seconds,
        progress_callback=None,
        progress_interval_seconds=30.0,
    ):
        try:
            return self._execute(
                command,
                cwd=cwd,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                progress_callback=progress_callback,
                progress_interval_seconds=progress_interval_seconds,
            )
        except Exception:
            if not self.lifecycle.is_started:
                self.lifecycle.discard_before_start()
            raise

    def _execute(
        self,
        command,
        *,
        cwd,
        input_text,
        timeout_seconds,
        progress_callback=None,
        progress_interval_seconds=30.0,
    ):
        command = list(command)
        if self.experiment_launch_registration is not None:
            if self.supported:
                _validate_registered_codex_command(
                    command,
                    self.experiment_launch_registration[
                        "model_policy"
                    ],
                )
            registered_workspace = Path(
                self.experiment_launch_registration["workspace_root"]
            )
            resolved_cwd = Path(cwd).resolve(strict=True)
            if (
                resolved_cwd != registered_workspace
                and not resolved_cwd.is_relative_to(
                    registered_workspace
                )
            ):
                raise ModelInvocationIntegrityError(
                    "provider cwd differs from experiment launch registration"
                )
        environment = None
        prepared = None
        sandbox_reference = self.lifecycle.context.get(
            "experiment_sandbox_reference"
        )
        sandbox_required = (
            self.lifecycle.context.get("experiment_sandbox_required") is True
        )
        if sandbox_required and sandbox_reference is None:
            raise ModelInvocationIntegrityError(
                "experiment provider sandbox is required but unavailable"
            )
        if sandbox_reference is not None:
            try:
                from .experiment_sandbox import (
                    ExperimentSandboxError,
                    ExperimentSandboxUnavailable,
                    load_provider_sandbox_reference,
                    prepare_provider_launch,
                    validate_experiment_lifecycle_authority,
                    validate_provider_authority_separation,
                )

                experiment_authority_root = self.lifecycle.context.get(
                    "experiment_authority_root"
                )
                if experiment_authority_root is None:
                    raise ExperimentSandboxError(
                        "experiment authority root is required"
                    )
                sandbox_descriptor = load_provider_sandbox_reference(
                    sandbox_reference,
                    experiment_authority_root,
                )
                validate_experiment_lifecycle_authority(
                    experiment_authority_root,
                    self.lifecycle.authority_root,
                )
                validate_provider_authority_separation(
                    sandbox_descriptor,
                    experiment_authority_root,
                    self.lifecycle.authority_root,
                )
                prepared = prepare_provider_launch(
                    sandbox_descriptor,
                    command,
                    cwd=cwd,
                )
            except ExperimentSandboxUnavailable as exc:
                raise ModelInvocationUnavailable(
                    f"experiment provider namespace unavailable: {exc}"
                ) from exc
            except ExperimentSandboxError as exc:
                raise ModelInvocationIntegrityError(
                    f"invalid experiment provider namespace: {exc}"
                ) from exc
            command = list(prepared.command)
            cwd = prepared.cwd
            environment = dict(prepared.environment)
            self.lifecycle.context["experiment_sandbox_policy_sha256"] = (
                prepared.policy_sha256
            )
            self.lifecycle.context[
                "experiment_sandbox_reference_sha256"
            ] = sandbox_reference["sha256"]
        try:
            if self.supported:
                supervisor_revalidation = False
                runner_arguments = {
                    "cwd": cwd,
                    "input_text": input_text,
                    "timeout_seconds": timeout_seconds,
                }
                if environment is not None:
                    runner_arguments["environment"] = environment
                resource_binding = self.lifecycle.context.get(
                    "resource_envelope_binding"
                )
                if resource_binding is not None:
                    runner_arguments.update(
                        {
                            "resource_envelope_binding": resource_binding,
                            "resource_mode": self.lifecycle.context.get(
                                "experiment_mode"
                            ),
                            "resource_run_id": self.lifecycle.context["run_id"],
                            "resource_hierarchy_reference": (
                                self.lifecycle.context.get(
                                    "resource_hierarchy_reference"
                                )
                            ),
                        }
                    )
                    runner_arguments["resource_run_id"] = (
                        self.lifecycle.context.get("resource_project_id")
                        or self.lifecycle.context["run_id"]
                    )
                if resource_binding is None:
                    self._acquire_experiment_provider_admission()
                    runner = self.systemd_runner_factory(
                        self.lifecycle,
                        command,
                        **runner_arguments,
                    )
                else:
                    runner = self.systemd_runner_factory(
                        self.lifecycle,
                        command,
                        **runner_arguments,
                    )
                    prepare_resources = getattr(
                        runner,
                        "prepare_resources_before_admission",
                        None,
                    )
                    if not callable(prepare_resources):
                        raise ModelInvocationUnavailable(
                            "model runner cannot enforce the required "
                            "resource envelope before admission"
                        )
                    prepare_resources()
                    if prepared is not None:
                        configure_source_authority = getattr(
                            runner,
                            "set_prelaunch_source_authority",
                            None,
                        )
                        if callable(configure_source_authority):
                            configure_source_authority(
                                prepared.source_authority()
                            )
                            supervisor_revalidation = True
                    try:
                        prepared_identity = runner.prepare()
                        self._acquire_experiment_provider_admission()
                    except Exception:
                        runner.abort_before_permit()
                        raise
                self.execution_group = runner
                if prepared is not None:
                    configure_source_authority = getattr(
                        runner,
                        "set_prelaunch_source_authority",
                        None,
                    )
                    if (
                        callable(configure_source_authority)
                        and not supervisor_revalidation
                    ):
                        configure_source_authority(
                            prepared.source_authority()
                        )
                        supervisor_revalidation = True
                try:
                    identity = (
                        prepared_identity
                        if resource_binding is not None
                        else runner.prepare()
                    )
                except Exception:
                    runner.abort_before_permit()
                    raise
                try:
                    self.lifecycle.publish_start(identity)
                except Exception:
                    runner.abort_before_permit()
                    raise
                if prepared is not None and not supervisor_revalidation:
                    try:
                        prepared.revalidate_mutable_sources()
                    except (
                        ExperimentSandboxError,
                        ExperimentSandboxUnavailable,
                    ) as exc:
                        runner.abort_before_permit()
                        return ProviderExecution(
                            command,
                            None,
                            "",
                            "",
                            launch_failed=True,
                            launch_error=(
                                "launch_identity_revalidation_failed:"
                                f"{type(exc).__name__}"
                            ),
                        )
                return runner.permit_and_wait(
                    progress_callback=progress_callback,
                    progress_interval_seconds=progress_interval_seconds,
                )

            # Test provider commands are intentionally outside the supported
            # live denominator, but still prove that start publication
            # precedes Popen.
            self._acquire_experiment_provider_admission()
            self.lifecycle.publish_start(
                ExecutionGroupIdentity.not_applicable()
            )
            return _run_bounded_process(
                command,
                cwd=cwd,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                environment=environment,
                progress_callback=progress_callback,
                progress_interval_seconds=progress_interval_seconds,
            )
        except Exception:
            if (
                self.provider_admission is not None
                and not self.lifecycle.is_started
            ):
                self.provider_admission.abandon_before_start()
                self.provider_admission = None
            raise

    def finalize(self, terminal_status, execution, *, terminal_writer="worker"):
        terminal = None
        try:
            terminal = self.lifecycle.finalize(
                terminal_status,
                stdout=execution.stdout,
                stderr=execution.stderr,
                terminal_writer=terminal_writer,
            )
            if self.provider_admission is not None:
                self.experiment_controller.post_terminal_accounting(
                    self.provider_admission,
                    started_path=self.lifecycle.started_path,
                    terminal_path=self.lifecycle.terminal_path,
                )
                self.provider_admission = None
            return terminal
        finally:
            if self.execution_group is not None:
                cleanup = self.execution_group.cleanup_after_terminal()
                evidence = execution.resource_evidence
                if evidence is not None:
                    evidence["cleanup"] = cleanup
                    self.lifecycle.write_resource_evidence(evidence)

    def _acquire_experiment_provider_admission(self):
        reference = self.lifecycle.context.get(
            "experiment_controller_reference"
        )
        controller_required = (
            self.lifecycle.context.get("experiment_controller_required")
            is True
            or reference is not None
        )
        authority_root = self.lifecycle.context.get(
            "experiment_authority_root"
        )
        try:
            from .experiment_controller import (
                ExperimentControllerError,
                ExperimentControllerIntegrityError,
                ExperimentProviderAdmissionDenied,
                ExperimentProviderLaneBusy,
                discover_experiment_controller_reference,
                load_experiment_controller,
                validate_experiment_controller_reference,
            )

            if reference is not None:
                validate_experiment_controller_reference(
                    reference,
                    expected_authority_root=(
                        None
                        if self.experiment_launch_registration
                        is not None
                        else authority_root
                    ),
                )
            elif authority_root is not None:
                reference = discover_experiment_controller_reference(
                    authority_root
                )
            if reference is None:
                if controller_required:
                    raise ExperimentControllerIntegrityError(
                        "required experiment budget controller is unavailable"
                    )
                return
            controller = load_experiment_controller(reference)
            invocation = {
                "invocation_id": self.lifecycle.invocation_id,
                "lifecycle_root": str(self.lifecycle.authority_root),
                "run_id": self.lifecycle.context["run_id"],
            }
            retry_deadline = (
                time.monotonic() + PROVIDER_LANE_ADMISSION_RETRY_SECONDS
            )
            while True:
                try:
                    admission = controller.prelaunch_admission(invocation)
                    break
                except ExperimentProviderLaneBusy:
                    if time.monotonic() >= retry_deadline:
                        raise
                    time.sleep(
                        PROVIDER_LANE_ADMISSION_RETRY_INTERVAL_SECONDS
                    )
        except ExperimentProviderAdmissionDenied as exc:
            raise ModelInvocationUnavailable(
                f"experiment provider admission denied: {exc}"
            ) from exc
        except ExperimentControllerIntegrityError as exc:
            raise ModelInvocationIntegrityError(
                f"invalid experiment budget controller authority: {exc}"
            ) from exc
        except ExperimentControllerError as exc:
            raise ModelInvocationIntegrityError(
                f"experiment budget controller failed: {exc}"
            ) from exc
        self.experiment_controller = controller
        self.provider_admission = admission


def _systemd_gated_supervisor_command(
    unit,
    module_path,
    spec_path,
    *,
    resource_arguments=(),
):
    return [
        "systemd-run",
        "--user",
        "--quiet",
        "--no-block",
        f"--unit={unit}",
        "--property=Type=oneshot",
        "--property=RemainAfterExit=yes",
        "--property=KillMode=control-group",
        *resource_arguments,
        "--",
        sys.executable,
        "-B",
        str(module_path),
        "_supervisor",
        str(spec_path),
    ]


class SystemdGatedExecution:
    """One exact lingering systemd execution group with a gated supervisor."""

    def __init__(
        self,
        lifecycle,
        command,
        *,
        cwd,
        input_text,
        timeout_seconds,
        environment=None,
        command_runner=None,
        resource_envelope_binding=None,
        resource_mode=None,
        resource_run_id=None,
        resource_hierarchy_reference=None,
        resource_hierarchy_factory=None,
    ):
        if not sys.platform.startswith("linux"):
            raise ModelInvocationUnavailable(
                "supported model invocation requires Linux"
            )
        self.lifecycle = lifecycle
        self.command = list(command)
        if self.command and not Path(self.command[0]).is_absolute():
            resolved_executable = shutil.which(self.command[0])
            if resolved_executable:
                self.command[0] = resolved_executable
        self.cwd = str(cwd)
        self.input_text = str(input_text)
        self.timeout_seconds = timeout_seconds
        self.environment = _validated_process_environment(environment)
        self.command_runner = command_runner or subprocess.run
        self.resource_envelope_binding = resource_envelope_binding
        self.resource_mode = resource_mode
        self.resource_run_id = resource_run_id
        self.resource_hierarchy_reference = resource_hierarchy_reference
        self.resource_hierarchy_factory = resource_hierarchy_factory
        self.resource_hierarchy = None
        self.resource_evidence = None
        digest = hashlib.sha256(
            lifecycle.invocation_id.encode("utf-8")
        ).hexdigest()
        self.unit = f"agentteam-inv-{digest[:24]}.service"
        socket_root = Path("/tmp") / f"agentteam-invocation-{os.getuid()}"
        socket_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(socket_root, 0o700)
        self.socket_path = socket_root / f"{digest[:40]}.sock"
        self.ready_path = lifecycle.invocation_dir / "supervisor-ready.json"
        self.result_path = lifecycle.invocation_dir / "supervisor-result.json"
        self.resource_evidence_ack_path = (
            lifecycle.invocation_dir / "resource-evidence.ack"
        )
        self.spec_path = lifecycle.invocation_dir / "supervisor-spec.json"
        self.nonce = secrets.token_hex(32)
        self._prepared = False
        self.identity = None
        self.prelaunch_source_authority = None

    def set_prelaunch_source_authority(self, authority):
        if self._prepared or self.spec_path.exists():
            raise ModelInvocationIntegrityError(
                "prelaunch source authority must precede supervisor start"
            )
        if not isinstance(authority, dict):
            raise ModelInvocationIntegrityError(
                "prelaunch source authority is invalid"
            )
        self.prelaunch_source_authority = json.loads(
            json.dumps(authority, sort_keys=True)
        )

    def prepare(self):
        user_service = self._preflight_user_service()
        self.prepare_resources_before_admission()
        resource_arguments = (
            self.resource_hierarchy.leaf_arguments()
            if self.resource_hierarchy is not None
            else ()
        )
        spec = {
            "command": self.command,
            "cwd": self.cwd,
            "input_text": self.input_text,
            "timeout_seconds": self.timeout_seconds,
            "socket_path": str(self.socket_path),
            "ready_path": str(self.ready_path),
            "result_path": str(self.result_path),
            "nonce_sha256": hashlib.sha256(
                self.nonce.encode("ascii")
            ).hexdigest(),
            "max_stream_bytes": MAX_PROVIDER_STREAM_BYTES,
        }
        if self.environment is not None:
            spec["environment"] = self.environment
        if self.prelaunch_source_authority is not None:
            spec["prelaunch_source_authority"] = (
                self.prelaunch_source_authority
            )
        if self.resource_envelope_binding is not None:
            self.resource_evidence_ack_path.unlink(missing_ok=True)
            spec["resource_evidence_ack_path"] = str(
                self.resource_evidence_ack_path
            )
        _exclusive_publish_json(self.spec_path, spec)
        module_path = str(Path(__file__).resolve())
        self._checked_command(
            _systemd_gated_supervisor_command(
                self.unit,
                module_path,
                self.spec_path,
                resource_arguments=resource_arguments,
            )
        )
        try:
            ready = _wait_for_json(
                self.ready_path,
                timeout_seconds=SYSTEMD_IDENTITY_TIMEOUT_SECONDS,
            )
            transient = self._transient_identity()
            pid = _positive_int(ready.get("pid"))
            pgid = _positive_int(ready.get("pgid"))
            if pid is None or pgid != pid:
                raise ModelInvocationUnavailable(
                    "gated supervisor must remain its process-group leader"
                )
            if transient["MainPID"] != str(pid):
                raise ModelInvocationUnavailable(
                    "transient service MainPID does not match gated supervisor"
                )
            identity = ExecutionGroupIdentity(
                gated_supervisor_pid=pid,
                gated_supervisor_pgid=pgid,
                host_boot_id=_read_boot_id(),
                gated_supervisor_start_ticks=_proc_start_ticks(pid),
                launch_nonce_sha256=spec["nonce_sha256"],
                systemd_linger_enabled=True,
                systemd_transient_unit=self.unit,
                systemd_transient_invocation_id=_systemd_invocation_id(
                    transient["InvocationID"]
                ),
                systemd_transient_kill_mode=_require_value(
                    transient["KillMode"],
                    {"control-group"},
                    "transient KillMode",
                ),
                systemd_user_manager_identity=self._manager_identity(),
                systemd_transient_control_group=_control_group(
                    transient["ControlGroup"]
                ),
                systemd_user_service_invocation_id=_systemd_invocation_id(
                    user_service["InvocationID"]
                ),
                systemd_user_service_control_group=_control_group(
                    user_service["ControlGroup"]
                ),
                systemd_user_service_kill_mode=_require_value(
                    user_service["KillMode"],
                    {"control-group", "mixed"},
                    "enclosing user service KillMode",
                ),
            )
            self.identity = identity
            if self.resource_hierarchy is not None:
                self.resource_hierarchy.verify_leaf(self.unit)
            self._prepared = True
            return identity
        except Exception:
            self.abort_before_permit()
            raise

    def prepare_resources_before_admission(self):
        """Enforce and read back parent limits before budget admission."""

        if (
            self.resource_envelope_binding is None
            or self.resource_hierarchy is not None
        ):
            return
        from .resource_envelope import SystemdResourceHierarchy

        hierarchy_factory = (
            self.resource_hierarchy_factory or SystemdResourceHierarchy
        )
        self.resource_hierarchy = hierarchy_factory(
            self.resource_envelope_binding,
            run_id=self.resource_run_id,
            mode=self.resource_mode,
            command_runner=self.command_runner,
            owner_reference=self.resource_hierarchy_reference,
        )
        self.resource_hierarchy.prepare()

    def permit_and_wait(
        self,
        *,
        progress_callback=None,
        progress_interval_seconds=30.0,
    ):
        if not self._prepared:
            raise ModelInvocationIntegrityError(
                "launch permit requires prepared execution identity"
            )
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
                channel.connect(str(self.socket_path))
                channel.sendall(self.nonce.encode("ascii") + b"\n")
        except OSError as exc:
            return ProviderExecution(
                self.command,
                None,
                "",
                "",
                launch_failed=True,
                launch_error=f"launch_permit_failed:{exc.__class__.__name__}",
            )

        revalidation_budget = (
            PRELAUNCH_SOURCE_REVALIDATION_TIMEOUT_SECONDS
            if self.prelaunch_source_authority is not None
            else 0.0
        )
        deadline = (
            time.monotonic()
            + float(self.timeout_seconds)
            + revalidation_budget
            + 10.0
        )
        interval = max(float(progress_interval_seconds or 0), 0.05)
        next_progress = time.monotonic() + interval
        while time.monotonic() < deadline:
            result = _read_json_if_exists(self.result_path)
            if result is not None:
                try:
                    resource_evidence = self._resource_evidence(
                        timed_out=bool(result.get("timed_out")),
                    )
                finally:
                    if self.resource_envelope_binding is not None:
                        self.resource_evidence_ack_path.touch(
                            mode=0o600,
                            exist_ok=True,
                        )
                return ProviderExecution(
                    self.command,
                    result.get("returncode"),
                    result.get("stdout", ""),
                    result.get("stderr", ""),
                    timed_out=bool(result.get("timed_out")),
                    launch_failed=not bool(result.get("launch_permitted")),
                    launch_error=result.get("launch_error"),
                    resource_evidence=resource_evidence,
                )
            if progress_callback is not None and time.monotonic() >= next_progress:
                progress_callback()
                next_progress = time.monotonic() + interval
            time.sleep(min(0.05, interval))
        empty_observed = self._execution_group_is_empty()
        resource_evidence = self._resource_evidence(timed_out=True)
        self._stop_exact_unit()
        if not empty_observed and not self._wait_execution_group_empty():
            raise ModelInvocationUnavailable(
                "exact transient service stopped without observable empty cgroup"
            )
        return ProviderExecution(
            self.command,
            None,
            "",
            "",
            timed_out=True,
            launch_error="supervisor_result_timeout",
            resource_evidence=resource_evidence,
        )

    def abort_before_permit(self):
        # Without the full verified tuple even a unique-looking unit name is
        # insufficient signaling authority.  The unopened launch channel
        # fails closed and its bounded supervisor handshake will expire.
        if self.identity is not None:
            self._stop_exact_unit(ignore_errors=True)
        if self.resource_hierarchy is not None:
            self.resource_hierarchy.cleanup()
        self.resource_evidence_ack_path.unlink(missing_ok=True)

    def cleanup_after_terminal(self):
        empty_observed = self._execution_group_is_empty()
        stopped = self._stop_exact_unit(ignore_errors=True)
        leaf_empty = empty_observed
        if stopped and not leaf_empty:
            leaf_empty = self._wait_execution_group_empty()
        if stopped and leaf_empty:
            self._checked_command(
                ["systemctl", "--user", "reset-failed", self.unit],
                ignore_errors=True,
            )
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.resource_evidence_ack_path.unlink(missing_ok=True)
        return {
            "leaf_unit_stopped": bool(stopped),
            "leaf_cgroup_empty": bool(leaf_empty),
        }

    def _resource_evidence(self, *, timed_out):
        if self.resource_hierarchy is None or self.identity is None:
            return None
        from .resource_envelope import (
            build_resource_evidence,
            read_resource_counters,
        )

        counters = read_resource_counters(
            self.identity.systemd_transient_control_group
        )
        self.resource_evidence = build_resource_evidence(
            binding=self.resource_envelope_binding,
            scope="workload",
            identity={
                "systemd_unit": self.unit,
                "control_group": (
                    self.identity.systemd_transient_control_group
                ),
                "transient_invocation_id": (
                    self.identity.systemd_transient_invocation_id
                ),
                "hierarchy": self.resource_hierarchy.identity(),
            },
            counters=counters,
            timed_out=timed_out,
        )
        return self.resource_evidence

    def _preflight_user_service(self):
        linger = self._checked_command(
            [
                "loginctl",
                "show-user",
                str(os.getuid()),
                "--property=Linger",
                "--value",
            ]
        ).stdout.strip()
        if linger.lower() != "yes":
            raise ModelInvocationUnavailable(
                "systemd user linger must be enabled before provider launch"
            )
        return self._systemctl_show(
            ["systemctl", "show", f"user@{os.getuid()}.service"],
            ("InvocationID", "ControlGroup", "KillMode"),
        )

    def _transient_identity(self):
        return self._systemctl_show(
            ["systemctl", "--user", "show", self.unit],
            ("InvocationID", "ControlGroup", "KillMode", "MainPID"),
        )

    def _manager_identity(self):
        completed = self._checked_command(
            [
                "systemctl",
                "--user",
                "show",
                "--property=InvocationID",
                "--property=UserspaceTimestampMonotonic",
                "--property=ManagerTimestampMonotonic",
            ]
        )
        properties = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and value:
                properties[key] = value
        for key in (
            "InvocationID",
            "UserspaceTimestampMonotonic",
            "ManagerTimestampMonotonic",
        ):
            if properties.get(key):
                return f"{key}:{properties[key]}"
        if not properties:
            raise ModelInvocationUnavailable(
                "systemd user manager identity is unavailable"
            )
        raise ModelInvocationUnavailable(
            "systemd user manager identity is empty"
        )

    def _systemctl_show(self, prefix, properties):
        command = list(prefix)
        command.extend(f"--property={name}" for name in properties)
        completed = self._checked_command(command)
        values = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        missing = [name for name in properties if not values.get(name)]
        if missing:
            raise ModelInvocationUnavailable(
                f"missing systemd identity properties: {','.join(missing)}"
            )
        return values

    def _stop_exact_unit(self, ignore_errors=False):
        if self.identity is not None:
            try:
                current = self._transient_identity()
                matches = (
                    current["InvocationID"]
                    == self.identity.systemd_transient_invocation_id
                    and current["ControlGroup"]
                    == self.identity.systemd_transient_control_group
                    and current["KillMode"] == "control-group"
                )
            except ModelInvocationUnavailable:
                matches = False
            if not matches:
                if ignore_errors:
                    return False
                raise ModelInvocationUnavailable(
                    "transient service identity changed before exact-unit stop"
                )
        completed = self._checked_command(
            ["systemctl", "--user", "stop", self.unit],
            ignore_errors=ignore_errors,
        )
        return completed.returncode == 0

    def _execution_group_is_empty(self):
        if self.identity is None:
            return False
        return _cgroup_populated(
            self.identity.systemd_transient_control_group
        ) is False

    def _wait_execution_group_empty(self, timeout_seconds=5.0):
        if self.identity is None:
            return False
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._execution_group_is_empty():
                return True
            time.sleep(0.05)
        return False

    def _checked_command(self, command, ignore_errors=False):
        try:
            completed = self.command_runner(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if ignore_errors:
                return subprocess.CompletedProcess(command, 1, "", str(exc))
            raise ModelInvocationUnavailable(
                f"systemd command unavailable: {command[0]}"
            ) from exc
        if completed.returncode != 0 and not ignore_errors:
            reason = (completed.stderr or completed.stdout or "").strip()[:500]
            raise ModelInvocationUnavailable(
                f"systemd command failed: {command[0]}: {reason}"
            )
        return completed


def is_supported_codex_command(command):
    """Recognize the real Codex executable without treating Python fakes as live."""
    if not command:
        return False
    executable = Path(str(command[0])).name.lower()
    return executable in {"codex", "codex.exe"}


def assess_execution_group_fence(
    start_record,
    *,
    current_boot_id,
    current_user_service,
    current_manager_identity,
    current_transient_service,
    pidfd_open=None,
    process_start_ticks=None,
    cgroup_populated=None,
):
    """Assess a persisted execution identity without signaling a numeric PID.

    A ``live_pinned`` result transfers the returned pidfd to the caller, which
    must close it.  Every ambiguous or recycled identity stays open.  ESRCH is
    useful only in combination with the exact persisted transient service and
    an observed ``cgroup.events`` value of ``populated 0``.
    """
    if current_boot_id != start_record.get("host_boot_id"):
        return _fence_assessment(
            "death_proven",
            "host_boot_changed",
            signal_allowed=False,
        )

    persisted_user_invocation = start_record.get(
        "systemd_user_service_invocation_id"
    )
    current_user_invocation = (
        current_user_service.get("InvocationID")
        if isinstance(current_user_service, dict)
        else None
    )
    if current_user_invocation != persisted_user_invocation:
        completed_restart = (
            isinstance(current_user_service, dict)
            and current_user_service.get("ActiveState") == "active"
            and bool(current_user_service.get("ControlGroup"))
            and bool(current_user_invocation)
            and start_record.get("systemd_user_service_kill_mode")
            in {"control-group", "mixed"}
        )
        if completed_restart:
            return _fence_assessment(
                "death_proven",
                "enclosing_user_service_restarted",
                signal_allowed=False,
            )
        return _fence_assessment(
            "open_ambiguous",
            "enclosing_user_service_identity_ambiguous",
        )
    if (
        not isinstance(current_user_service, dict)
        or current_user_service.get("ControlGroup")
        != start_record.get("systemd_user_service_control_group")
        or current_user_service.get("KillMode")
        != start_record.get("systemd_user_service_kill_mode")
    ):
        return _fence_assessment(
            "open_ambiguous",
            "enclosing_user_service_properties_mismatch",
        )

    if current_manager_identity != start_record.get(
        "systemd_user_manager_identity"
    ):
        return _fence_assessment(
            "open_ambiguous",
            "user_manager_identity_changed_without_enclosing_restart",
        )

    if not _transient_identity_matches(start_record, current_transient_service):
        return _fence_assessment(
            "open_ambiguous",
            "transient_service_identity_mismatch",
        )

    pidfd_open = pidfd_open or getattr(os, "pidfd_open", None)
    process_start_ticks = process_start_ticks or _proc_start_ticks
    cgroup_populated = cgroup_populated or _cgroup_populated
    if pidfd_open is None:
        return _fence_assessment(
            "open_ambiguous",
            "pidfd_unavailable",
        )
    pid = start_record.get("gated_supervisor_pid")
    try:
        pidfd = pidfd_open(pid, 0)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            return _fence_assessment(
                "open_ambiguous",
                f"pidfd_open_failed_errno_{exc.errno}",
            )
        populated = cgroup_populated(
            start_record.get("systemd_transient_control_group")
        )
        if populated is False:
            return _fence_assessment(
                "death_proven",
                "exact_transient_cgroup_empty",
                signal_allowed=False,
            )
        if populated is True:
            return _fence_assessment(
                "exact_service_stop_required",
                "exact_transient_cgroup_populated",
                signal_allowed=False,
            )
        return _fence_assessment(
            "open_ambiguous",
            "transient_cgroup_population_unknown",
        )

    try:
        ticks = process_start_ticks(pid)
    except (OSError, ModelInvocationUnavailable):
        os.close(pidfd)
        return _fence_assessment(
            "open_ambiguous",
            "pinned_process_start_identity_unreadable",
        )
    if ticks != start_record.get("gated_supervisor_start_ticks"):
        os.close(pidfd)
        return _fence_assessment(
            "open_ambiguous",
            "pinned_process_start_identity_mismatch",
        )
    return _fence_assessment(
        "live_pinned",
        "pidfd_and_start_ticks_match",
        signal_allowed=True,
        pidfd=pidfd,
    )


def invocation_context_from_message(message, *, model=None, backend="codex"):
    payload = dict(message.get("payload") or {})
    nested = payload.get("model_invocation_context")
    if isinstance(nested, dict):
        payload = {**payload, **nested}
    attempt_id = _nonempty(payload.get("attempt_id")) or "ATTEMPT-UNKNOWN"
    task_id = _nullable_text(payload.get("task_id"))
    role = _nullable_text(payload.get("agent_role") or payload.get("required_role"))
    resume_mode = payload.get("provider_resume_mode")
    requested_session = _nullable_text(
        payload.get("requested_provider_session_id")
    )
    if resume_mode not in {"new", "explicit", "resume_last"}:
        resume_mode = "explicit" if requested_session else "new"
    context = {
        "project": _nonempty(payload.get("project")) or "agentteam",
        "run_id": _nonempty(payload.get("run_id")) or f"RUN-{attempt_id}",
        "pursue_id": _nullable_text(payload.get("pursue_id")),
        "round_index": payload.get("round_index"),
        "taskpack_id": (
            _nonempty(payload.get("taskpack_id"))
            or _nonempty(payload.get("backlog_id"))
            or task_id
            or "TASKPACK-UNKNOWN"
        ),
        "implementation_run_id": _nullable_text(
            payload.get("implementation_run_id")
        ),
        "gate_epoch": payload.get("gate_epoch"),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "runtime_execution_session_id": (
            _nonempty(payload.get("runtime_execution_session_id"))
            or f"SESSION-{attempt_id}"
        ),
        "requested_provider_session_id": requested_session,
        "provider_resume_mode": resume_mode,
        "provider_predecessor_invocation_id": _nullable_text(
            payload.get("provider_predecessor_invocation_id")
        ),
        "provider_predecessor_turn_id": _nullable_text(
            payload.get("provider_predecessor_turn_id")
        ),
        "provider_predecessor_usage_snapshot": payload.get(
            "provider_predecessor_usage_snapshot"
        ),
        "lifecycle_owner_token": (
            _nonempty(payload.get("lifecycle_owner_token"))
            or _nonempty(payload.get("lease_id"))
            or f"OWNER-{attempt_id}"
        ),
        "agent_id": _nullable_text(
            payload.get("agent_id") or message.get("to_agent")
        ),
        "role": role,
        "usage_stage": (
            payload.get("usage_stage")
            if payload.get("usage_stage") in _USAGE_STAGES
            else _usage_stage_for_role(role, payload.get("task_kind"))
        ),
        "backend": backend,
        "model": _nullable_text(payload.get("model") or model),
        "reasoning_profile": _nullable_text(
            payload.get("reasoning_profile")
        ),
        "model_routing": (
            dict(payload.get("model_routing"))
            if isinstance(payload.get("model_routing"), dict)
            else None
        ),
        "coverage_class": payload.get("coverage_class"),
        "provider_usage_scope": payload.get("provider_usage_scope"),
        "provider_session_lock_held": payload.get(
            "provider_session_lock_held", False
        ),
        "provider_project_binding_valid": payload.get(
            "provider_project_binding_valid", False
        ),
        "provider_lineage_status": payload.get("provider_lineage_status"),
        "previous_provider_session_id": payload.get(
            "previous_provider_session_id"
        ),
        "previous_provider_turn_id": payload.get("previous_provider_turn_id"),
        "previous_invocation_id": payload.get("previous_invocation_id"),
        "experiment_sandbox_reference": payload.get(
            "experiment_sandbox_reference"
        ),
        "experiment_sandbox_required": (
            payload.get("experiment_sandbox_required") is True
        ),
        "experiment_authority_root": payload.get(
            "experiment_authority_root"
        ),
        "experiment_controller_reference": payload.get(
            "experiment_controller_reference"
        ),
        "experiment_controller_required": (
            payload.get("experiment_controller_required") is True
        ),
        "resource_envelope_binding": payload.get(
            "resource_envelope_binding"
        ),
        "resource_envelope_required": (
            payload.get("resource_envelope_required") is True
        ),
        "resource_hierarchy_reference": payload.get(
            "resource_hierarchy_reference"
        ),
        "resource_project_id": payload.get("resource_project_id"),
    }
    context["_explicit_context_fields"] = {
        "project": _nonempty(payload.get("project")) is not None,
        "run_id": _nonempty(payload.get("run_id")) is not None,
        "taskpack_id": _nonempty(payload.get("taskpack_id")) is not None,
        "runtime_execution_session_id": (
            _nonempty(payload.get("runtime_execution_session_id")) is not None
        ),
        "lifecycle_owner_token": (
            _nonempty(payload.get("lifecycle_owner_token"))
            or _nonempty(payload.get("lease_id"))
        )
        is not None,
        "agent_id": (
            _nonempty(payload.get("agent_id"))
            or _nonempty(message.get("to_agent"))
        )
        is not None,
        "role": (
            _nonempty(payload.get("agent_role"))
            or _nonempty(payload.get("required_role"))
        )
        is not None,
        "usage_stage": _nonempty(payload.get("usage_stage")) is not None,
    }
    if payload.get("experiment_mode") is not None:
        context["experiment_mode"] = payload["experiment_mode"]
    return context


def _run_bounded_process(
    command,
    *,
    cwd,
    input_text,
    timeout_seconds,
    environment=None,
    progress_callback=None,
    progress_interval_seconds=30.0,
    max_stream_bytes=MAX_PROVIDER_STREAM_BYTES,
):
    popen_arguments = {
        "cwd": cwd,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    validated_environment = _validated_process_environment(environment)
    if validated_environment is not None:
        popen_arguments["env"] = validated_environment
    try:
        process = subprocess.Popen(
            command,
            **popen_arguments,
        )
    except OSError as exc:
        return ProviderExecution(
            list(command),
            None,
            "",
            str(exc),
            launch_failed=True,
            launch_error=f"provider_popen_failed:{exc.__class__.__name__}",
        )

    stdout = bytearray()
    stderr = bytearray()

    def drain(stream, target):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            remaining = max_stream_bytes - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])

    def feed():
        try:
            process.stdin.write(str(input_text).encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    deadline = (
        time.monotonic() + float(timeout_seconds)
        if timeout_seconds is not None
        else None
    )
    interval = max(float(progress_interval_seconds or 0), 0.05)
    next_progress = time.monotonic() + interval
    while process.poll() is None:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            timed_out = True
            process.kill()
            break
        if progress_callback is not None and now >= next_progress:
            try:
                progress_callback()
            except Exception:
                process.kill()
                process.wait()
                raise
            next_progress = now + interval
        time.sleep(min(interval, 0.05))
    process.wait()
    for thread in threads:
        thread.join(timeout=2)
    process.stdout.close()
    process.stderr.close()
    return ProviderExecution(
        list(command),
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
    )


def _supervisor_main(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    try:
        os.setpgid(0, 0)
    except PermissionError:
        if os.getpgrp() != os.getpid():
            raise
    pid = os.getpid()
    pgid = os.getpgrp()
    socket_path = Path(spec["socket_path"])
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    launch_permitted = False
    launch_error = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        channel.listen(1)
        _exclusive_publish_json(
            Path(spec["ready_path"]),
            {"pid": pid, "pgid": pgid, "ready_at": _utc_now()},
        )
        channel.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        try:
            connection, _ = channel.accept()
            with connection:
                permit = connection.recv(256).strip()
            digest = hashlib.sha256(permit).hexdigest()
            if digest != spec["nonce_sha256"]:
                launch_error = "launch_nonce_mismatch"
            else:
                launch_permitted = True
        except socket.timeout:
            launch_error = "launch_permit_timeout"
        except OSError as exc:
            launch_error = f"launch_channel_failed:{exc.__class__.__name__}"
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass

    if launch_permitted and spec.get("prelaunch_source_authority") is not None:
        try:
            revalidate = _load_release_source_revalidator()
            _call_with_wall_alarm(
                revalidate,
                spec["prelaunch_source_authority"],
                PRELAUNCH_SOURCE_REVALIDATION_TIMEOUT_SECONDS,
            )
        except (
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            launch_permitted = False
            launch_error = (
                "launch_identity_revalidation_failed:"
                f"{type(exc).__name__}"
            )

    if launch_permitted:
        execution = _run_bounded_process_with_parent_death_safeguard(spec)
        result = {
            "launch_permitted": True,
            "launch_error": execution.launch_error,
            "returncode": execution.returncode,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "timed_out": execution.timed_out,
        }
    else:
        result = {
            "launch_permitted": False,
            "launch_error": launch_error,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }
    _exclusive_publish_json(Path(spec["result_path"]), result)
    ack_path = spec.get("resource_evidence_ack_path")
    if ack_path is not None:
        deadline = time.monotonic() + 10.0
        ack_path = Path(ack_path)
        while time.monotonic() < deadline:
            if ack_path.is_file() and not ack_path.is_symlink():
                break
            time.sleep(0.05)
    return 0


def _load_release_source_revalidator():
    """Load the source guard from the same runtime release as this helper."""

    package_root = Path(__file__).resolve().parent.parent
    package_root_text = str(package_root)
    if package_root_text not in sys.path:
        sys.path.insert(0, package_root_text)
    from agentteam_runtime import experiment_sandbox

    expected_package = Path(__file__).resolve().parent
    loaded_package = Path(experiment_sandbox.__file__).resolve().parent
    if loaded_package != expected_package:
        raise ImportError(
            "experiment sandbox was not loaded from the active runtime release"
        )
    return experiment_sandbox._revalidate_prepared_source_authority


def _call_with_wall_alarm(callback, argument, timeout_seconds):
    """Bound a pre-exec check in the real helper's main thread."""

    if threading.current_thread() is not threading.main_thread():
        return callback(argument)

    def timeout_handler(_signum, _frame):
        raise TimeoutError("prelaunch source revalidation timed out")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds))
    try:
        return callback(argument)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                previous_timer[0],
                previous_timer[1],
            )


def _run_source_guard_command(command, command_timeout):
    try:
        process = subprocess.Popen(command, env=dict(os.environ))
    except (OSError, subprocess.SubprocessError):
        return 126
    try:
        return process.wait(timeout=command_timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return 124
        return 124


def _source_guard_main(
    authority_path,
    authority_sha256,
    command_timeout_seconds,
    command,
):
    """Revalidate mutable source and supervise the guarded command."""

    try:
        command_timeout = float(command_timeout_seconds)
    except (TypeError, ValueError):
        return 64
    if (
        not command
        or not math.isfinite(command_timeout)
        or not 0 < command_timeout <= 3600
        or len(authority_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in authority_sha256
        )
        or not all(
            isinstance(argument, str) and "\x00" not in argument
            for argument in command
        )
    ):
        return 64
    try:
        authority_bytes = Path(authority_path).read_bytes()
        if (
            len(authority_bytes) > 1024 * 1024
            or hashlib.sha256(authority_bytes).hexdigest()
            != authority_sha256
        ):
            return 125
        authority = json.loads(authority_bytes)
        _call_with_wall_alarm(
            _load_release_source_revalidator(),
            authority,
            PRELAUNCH_SOURCE_REVALIDATION_TIMEOUT_SECONDS,
        )
    except (
        ImportError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return 125
    return _run_source_guard_command(command, command_timeout)


def _run_bounded_process_with_parent_death_safeguard(spec):
    command = list(spec["command"])
    popen_arguments = {
        "cwd": spec["cwd"],
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "preexec_fn": _provider_child_setup,
    }
    environment = _validated_process_environment(spec.get("environment"))
    if environment is not None:
        popen_arguments["env"] = environment
    try:
        process = subprocess.Popen(
            command,
            **popen_arguments,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProviderExecution(
            command,
            None,
            "",
            str(exc),
            launch_failed=True,
            launch_error=f"provider_popen_failed:{exc.__class__.__name__}",
        )

    stdout = bytearray()
    stderr = bytearray()
    limit = int(spec.get("max_stream_bytes", MAX_PROVIDER_STREAM_BYTES))

    def drain(stream, target):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            remaining = limit - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])

    def feed():
        try:
            process.stdin.write(spec["input_text"].encode("utf-8"))
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=float(spec["timeout_seconds"]))
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_owned_process_group(process)
    for thread in threads:
        thread.join(timeout=2)
    process.stdout.close()
    process.stderr.close()
    return ProviderExecution(
        command,
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
    )


def _provider_child_setup():
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(PARENT_DEATH_SIGNAL), 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), PARENT_DEATH_SIGNAL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _terminate_owned_process_group(process):
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    finally:
        signal.signal(signal.SIGTERM, previous)


def _terminal_usage(stdout, context, *, terminal_status):
    coverage_class = context["coverage_class"]
    if coverage_class == "not_applicable_adapter":
        return {
            "usage_status": "not_applicable",
            "usage_source": None,
            "provider_usage_scope": "unknown",
            "accounting_method": "not_applicable",
            "provider_usage_snapshot": None,
            "unavailable_reason": None,
            **_null_token_fields(),
        }
    try:
        from .token_usage import parse_terminal_usage_from_jsonl
    except ImportError:  # pragma: no cover - only the standalone helper path
        return _unavailable_usage(terminal_status)

    session_context = {
        "resume_mode": context.get("provider_resume_mode"),
        "provider_session_id": context.get("requested_provider_session_id"),
        "provider_predecessor_invocation_id": context.get(
            "provider_predecessor_invocation_id"
        ),
        "provider_predecessor_turn_id": context.get(
            "provider_predecessor_turn_id"
        ),
        "previous_provider_snapshot": context.get(
            "provider_predecessor_usage_snapshot"
        ),
        "previous_provider_session_id": context.get(
            "previous_provider_session_id"
        )
        or context.get("requested_provider_session_id"),
        "previous_invocation_id": context.get("previous_invocation_id")
        or context.get("provider_predecessor_invocation_id"),
        "previous_provider_turn_id": context.get("previous_provider_turn_id")
        or context.get("provider_predecessor_turn_id"),
        "provider_session_lock_held": context.get(
            "provider_session_lock_held"
        )
        is True,
        "provider_project_binding_valid": context.get(
            "provider_project_binding_valid"
        )
        is True,
        "lineage_status": context.get("provider_lineage_status"),
    }
    usage = parse_terminal_usage_from_jsonl(
        stdout,
        provider_usage_scope=context.get("provider_usage_scope"),
        session_context=session_context,
    )
    return usage if isinstance(usage, dict) else _unavailable_usage(terminal_status)


def _unavailable_usage(terminal_status):
    reasons = {
        "timed_out": "provider_usage_unavailable_before_timeout",
        "launch_failed": "provider_launch_failed",
        "missing_result": "provider_result_missing",
        "invalid_result": "provider_result_invalid",
    }
    return {
        "usage_status": "unavailable",
        "usage_source": "codex_jsonl",
        "provider_usage_scope": "invocation",
        "accounting_method": "unavailable",
        "provider_usage_snapshot": None,
        "unavailable_reason": reasons.get(
            terminal_status,
            "missing_provider_terminal_usage",
        ),
        **_null_token_fields(),
    }


def _provider_lineage(stdout, context):
    session_id = context.get("requested_provider_session_id")
    turn_id = None
    for line in str(stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        for source in (event, event.get("usage"), event.get("token_usage")):
            if not isinstance(source, dict):
                continue
            session_id = (
                source.get("provider_session_id")
                or source.get("session_id")
                or session_id
            )
            turn_id = source.get("provider_turn_id") or source.get("turn_id") or turn_id
    return {
        "provider_session_id": _nullable_text(session_id),
        "provider_turn_id": _nullable_text(turn_id),
    }


def _start_context(context):
    fields = (
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
        "requested_provider_session_id",
        "provider_resume_mode",
        "provider_predecessor_invocation_id",
        "provider_predecessor_turn_id",
        "provider_predecessor_usage_snapshot",
        "lifecycle_owner_token",
        "agent_id",
        "role",
        "usage_stage",
        "backend",
        "model",
        "coverage_class",
    )
    result = {field: context.get(field) for field in fields}
    for field in (
        "experiment_run_id",
        "experiment_mode",
        "experiment_protocol_sha256",
        "experiment_run_manifest_sha256",
        "reasoning_profile",
        "model_routing",
        "decision_id",
    ):
        if context.get(field) is not None:
            result[field] = context[field]
    policy_digest = context.get("experiment_sandbox_policy_sha256")
    if policy_digest is not None:
        result["experiment_sandbox_policy_sha256"] = policy_digest
    reference_digest = context.get("experiment_sandbox_reference_sha256")
    if reference_digest is not None:
        result["experiment_sandbox_reference_sha256"] = reference_digest
    return result


def _terminal_context(context):
    fields = (
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
    )
    result = {field: context.get(field) for field in fields}
    for field in (
        "experiment_run_id",
        "experiment_mode",
        "experiment_protocol_sha256",
        "experiment_run_manifest_sha256",
        "reasoning_profile",
        "model_routing",
        "decision_id",
    ):
        if context.get(field) is not None:
            result[field] = context[field]
    policy_digest = context.get("experiment_sandbox_policy_sha256")
    if policy_digest is not None:
        result["experiment_sandbox_policy_sha256"] = policy_digest
    reference_digest = context.get("experiment_sandbox_reference_sha256")
    if reference_digest is not None:
        result["experiment_sandbox_reference_sha256"] = reference_digest
    return result


def _exclusive_publish_json(path, record):
    data = (
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    _exclusive_or_idempotent_bytes(Path(path), data)


def _exclusive_or_idempotent_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ModelInvocationIntegrityError(
                    f"conflicting immutable record: {path}"
                )
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_canonical_events(path):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ModelInvocationIntegrityError(
                f"invalid canonical event JSON at line {line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise ModelInvocationIntegrityError(
                f"canonical event at line {line_number} is not an object"
            )
        records.append(record)
    return records


def _append_canonical_event_bytes(path, records):
    if not records:
        return
    path = Path(path)
    with path.open("ab") as stream:
        for record in records:
            stream.write(
                (
                    json.dumps(
                        record,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _canonical_events_are_equivalent(left, right):
    if left.get("event_type") != right.get("event_type"):
        return False
    if left.get("event_type") in CANONICAL_LIFECYCLE_EVENT_TYPES:
        left_payload = left.get("payload")
        right_payload = right.get("payload")
        left_digest = (
            left_payload.get("_source_record_sha256")
            if isinstance(left_payload, dict)
            else None
        )
        right_digest = (
            right_payload.get("_source_record_sha256")
            if isinstance(right_payload, dict)
            else None
        )
        if (
            left_digest is not None
            and right_digest is not None
            and left_digest != right_digest
        ):
            return False
        return (
            left.get("source_event_id") == right.get("source_event_id")
            and _canonical_source_record(left_payload)
            == _canonical_source_record(right_payload)
        )
    return True


def _canonical_source_record(payload):
    if not isinstance(payload, dict):
        return payload
    return {
        key: value
        for key, value in payload.items()
        if key not in _CANONICAL_SOURCE_METADATA_FIELDS
    }


def _read_lifecycle_record(path, label):
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ModelInvocationIntegrityError(
            f"missing immutable {label} record: {path}"
        ) from exc
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelInvocationIntegrityError(
            f"invalid immutable {label} record: {path}"
        ) from exc
    if not isinstance(record, dict):
        raise ModelInvocationIntegrityError(
            f"immutable {label} record is not an object: {path}"
        )
    return record, hashlib.sha256(data).hexdigest()


def _required_record_text(record, field, label):
    value = record.get(field) if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        raise ModelInvocationIntegrityError(
            f"{label} requires nonempty {field}"
        )
    return value


def _canonical_lifecycle_event(
    event_type,
    record,
    record_digest,
    source_path,
    *,
    actor,
    run_id,
    step_id,
    source_root,
):
    invocation_id = _required_record_text(
        record,
        "invocation_id",
        event_type,
    )
    if event_type == "model_invocation_started":
        source_event_id = invocation_id
        idempotency_key = f"model-invocation-started:{invocation_id}"
        event_time = record.get("started_at")
    elif event_type == "model_invocation_writer_revoked":
        owner_token = _required_record_text(
            record,
            "lifecycle_owner_token",
            "writer revocation",
        )
        source_event_id = (
            f"{invocation_id}:{owner_token}"
        )
        idempotency_key = (
            "model-invocation-writer-revoked:"
            f"{invocation_id}:{owner_token}"
        )
        event_time = record.get("revoked_at")
    elif event_type == "model_invocation_usage_recorded":
        source_event_id = _required_record_text(
            record,
            "usage_event_id",
            "terminal usage",
        )
        idempotency_key = source_event_id
        event_time = record.get("finished_at")
    else:  # pragma: no cover - guarded by internal callers
        raise ModelInvocationIntegrityError(
            f"unsupported canonical lifecycle event type: {event_type}"
        )
    if not isinstance(event_time, str) or not event_time:
        raise ModelInvocationIntegrityError(
            f"{event_type} requires its immutable source timestamp"
        )
    payload = {
        **record,
        "_source_artifact_path": _source_artifact_label(
            source_path,
            source_root,
        ),
        "_source_record_sha256": record_digest,
    }
    return {
        "event_id": None,
        "sequence": 0,
        "time": event_time,
        "event_type": event_type,
        "actor": actor,
        "target_agent_id": record.get("agent_id"),
        "idempotency_key": idempotency_key,
        "correlation_id": f"model-invocation:{invocation_id}",
        "payload": payload,
        "run_id": run_id or record.get("run_id"),
        "step_id": step_id,
        "source_event_id": source_event_id,
        "source_event_sequence": None,
    }


def _source_artifact_label(path, source_root):
    path = Path(path).resolve()
    if source_root is not None:
        source_root = Path(source_root).resolve()
        if _is_relative_to(path, source_root):
            return path.relative_to(source_root).as_posix()
    return str(path)


def _read_json_record(path, label):
    record, _digest = _read_lifecycle_record(path, label)
    return record


def _work_root_for_run(run_dir):
    run_dir = Path(run_dir).resolve()
    for parent in (run_dir, *run_dir.parents):
        if parent.name == "runs":
            return parent.parent.resolve()
    raise ModelInvocationIntegrityError(
        f"author bootstrap run is not below a work-root runs directory: {run_dir}"
    )


def _contained_bootstrap_path(
    root,
    relative_path,
    label,
    *,
    require_file=True,
):
    if not isinstance(relative_path, str) or not relative_path:
        raise ModelInvocationIntegrityError(
            f"author bootstrap requires nonempty {label}"
        )
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ModelInvocationIntegrityError(
            f"author bootstrap {label} must be a contained relative path"
        )
    candidate = (Path(root) / relative).resolve()
    if not _is_relative_to(candidate, Path(root).resolve()):
        raise ModelInvocationIntegrityError(
            f"author bootstrap {label} escapes the work root"
        )
    if require_file and not candidate.is_file():
        raise ModelInvocationIntegrityError(
            f"author bootstrap {label} is not a file"
        )
    if not require_file and not candidate.is_dir():
        raise ModelInvocationIntegrityError(
            f"author bootstrap {label} is not a directory"
        )
    return candidate


def _validate_file_digest(path, expected_digest, label):
    if (
        not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_digest)
    ):
        raise ModelInvocationIntegrityError(
            f"author bootstrap has invalid {label} SHA-256"
        )
    actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if not secrets.compare_digest(actual, expected_digest):
        raise ModelInvocationIntegrityError(
            f"author bootstrap {label} digest mismatch"
        )


def _is_relative_to(path, root):
    try:
        Path(path).relative_to(Path(root))
    except ValueError:
        return False
    return True


def _coerce_canonical_events(events):
    if isinstance(events, (str, os.PathLike, Path)):
        return _read_canonical_events(events)
    if not isinstance(events, (list, tuple)):
        events = list(events)
    if not all(isinstance(event, dict) for event in events):
        raise ModelInvocationIntegrityError(
            "canonical event replay requires event objects"
        )
    return list(events)


def _insert_replayed_record(target, key, record, label):
    prior = target.get(key)
    if prior is not None and prior != record:
        raise ModelInvocationIntegrityError(
            f"conflicting duplicate {label} ID: {key}"
        )
    target.setdefault(key, record)


def _unavailable_provider_lineage(reason):
    return {
        "provider_lineage_status": "unavailable",
        "provider_lineage_error": reason,
    }


def _read_json_if_exists(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _wait_for_json(path, *, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = _read_json_if_exists(path)
        if record is not None:
            return record
        time.sleep(0.02)
    raise ModelInvocationUnavailable(f"timed out waiting for {Path(path).name}")


def _fsync_directory(path):
    descriptor = os.open(Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="ascii"
    ).strip()
    if not value:
        raise ModelInvocationUnavailable("host boot ID is unavailable")
    return value


def _cgroup_populated(control_group, *, cgroup_root="/sys/fs/cgroup"):
    if not isinstance(control_group, str) or not control_group.startswith("/"):
        return None
    events_path = Path(cgroup_root) / control_group.lstrip("/") / "cgroup.events"
    try:
        lines = events_path.read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    for line in lines:
        key, separator, value = line.partition(" ")
        if key == "populated" and separator and value in {"0", "1"}:
            return value == "1"
    return None


def _proc_start_ticks(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError as exc:
        raise ModelInvocationUnavailable(
            "gated supervisor disappeared before durable start"
        ) from exc
    after_comm = stat.rsplit(")", 1)[1].strip().split()
    try:
        return int(after_comm[19])
    except (IndexError, ValueError) as exc:
        raise ModelInvocationUnavailable(
            "gated supervisor start ticks are unavailable"
        ) from exc


def _systemd_invocation_id(value):
    value = str(value or "").lower()
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise ModelInvocationUnavailable("invalid systemd InvocationID")
    return value


def _control_group(value):
    value = str(value or "")
    if not value.startswith("/"):
        raise ModelInvocationUnavailable("invalid systemd ControlGroup")
    return value


def _transient_identity_matches(start_record, current):
    return (
        isinstance(current, dict)
        and current.get("InvocationID")
        == start_record.get("systemd_transient_invocation_id")
        and current.get("ControlGroup")
        == start_record.get("systemd_transient_control_group")
        and current.get("KillMode") == "control-group"
    )


def _fence_assessment(status, proof, *, signal_allowed=False, pidfd=None):
    return {
        "fence_status": status,
        "proof": proof,
        "signal_allowed": signal_allowed,
        "pidfd": pidfd,
    }


def _require_value(value, allowed, label):
    if value not in allowed:
        raise ModelInvocationUnavailable(f"invalid {label}: {value}")
    return value


def _positive_int(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _timestamp_seconds(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _utc_now():
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _usage_event_id(invocation_id):
    namespace = b"agentteam:model_invocation_usage.v1\x00"
    digest = hashlib.sha256(namespace + invocation_id.encode("utf-8")).hexdigest()
    return f"USAGE-{digest}"


def _null_token_fields():
    return {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


def _nonempty(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _nullable_text(value):
    return _nonempty(value)


def _validated_process_environment(environment):
    if environment is None:
        return None
    if (
        not isinstance(environment, dict)
        or len(environment) > 64
        or not all(
            isinstance(name, str)
            and name
            and "=" not in name
            and "\x00" not in name
            and isinstance(value, str)
            and "\x00" not in value
            and len(value) <= 4096
            for name, value in environment.items()
        )
    ):
        raise ModelInvocationIntegrityError(
            "provider process environment is not bounded"
        )
    return dict(environment)


def _bounded_file_sha256(path, *, max_bytes=4 * 1024 * 1024):
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ModelInvocationIntegrityError(
            f"model invocation artifact is unavailable: {path}"
        ) from exc
    if size < 0 or size > max_bytes:
        raise ModelInvocationIntegrityError(
            f"model invocation artifact exceeds bound: {path}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = max_bytes + 1
        while remaining:
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining == 0:
        raise ModelInvocationIntegrityError(
            f"model invocation artifact exceeds bound: {path}"
        )
    return digest.hexdigest()


_USAGE_STAGES = {
    "single_codex",
    "taskpack_author",
    "planner_or_task_slicer",
    "repo_map",
    "implementation_worker",
    "review_or_repair",
    "follow_up_author",
    "semantic_architecture",
    "runtime_diagnostic",
    "development_smoke",
    "acceptance_live_smoke",
}


def _usage_stage_for_role(role, task_kind):
    if task_kind == "decompose_backlog":
        return "planner_or_task_slicer"
    role_map = {
        "taskpack_author": "taskpack_author",
        "planner": "planner_or_task_slicer",
        "task_slicer": "planner_or_task_slicer",
        "repo_map_agent": "repo_map",
        "repo_map": "repo_map",
        "implementation_worker": "implementation_worker",
        "reviewer": "review_or_repair",
        "repair_worker": "review_or_repair",
        "follow_up_author": "follow_up_author",
        "semantic_architecture": "semantic_architecture",
    }
    return role_map.get(role, "implementation_worker")


def _bind_registered_experiment_launch(authority_root, context):
    try:
        from .experiment_sandbox import (
            ExperimentSandboxError,
            load_experiment_launch_registration,
        )

        registration = load_experiment_launch_registration(
            authority_root
        )
    except ExperimentSandboxError as exc:
        raise ModelInvocationIntegrityError(
            f"invalid experiment launch registration: {exc}"
        ) from exc
    if registration is None:
        return dict(context), None
    context = dict(context)
    model_policy = registration["model_policy"]
    expected = {
        "run_id": registration["experiment_run_id"],
        "taskpack_id": registration["taskpack_id"],
        "usage_stage": registration["usage_stage"],
        "backend": model_policy["backend"],
        "model": model_policy["model"],
        "reasoning_profile": model_policy["reasoning_profile"],
        "experiment_run_id": registration["experiment_run_id"],
        "experiment_mode": registration["mode"],
        "experiment_protocol_sha256": registration[
            "protocol_sha256"
        ],
        "experiment_run_manifest_sha256": registration[
            "run_manifest_sha256"
        ],
        "experiment_sandbox_reference": registration[
            "sandbox_reference"
        ],
        "experiment_sandbox_required": True,
        "experiment_authority_root": registration["authority_root"],
        "experiment_controller_reference": registration[
            "controller_reference"
        ],
        "experiment_controller_required": True,
        "model_invocation_authority_root": registration[
            "lifecycle_authority_root"
        ],
        "provider_project_identity": registration["workspace_root"],
    }
    for field, value in expected.items():
        if field in context and context[field] != value:
            raise ModelInvocationIntegrityError(
                "caller context differs from experiment launch "
                f"registration: {field}"
            )
        context[field] = value
    if (
        registration["mode"] == "single_codex"
        and context.get("provider_resume_mode", "new") != "new"
    ):
        raise ModelInvocationIntegrityError(
            "single Codex experiment invocation must be fresh"
        )
    return context, registration


def _validate_registered_codex_command(command, model_policy):
    if not is_supported_codex_command(command):
        raise ModelInvocationIntegrityError(
            "registered live experiment launch is not a Codex command"
        )
    agentteam_only_options = {
        "--notification-project",
        "--feishu-webhook-env",
        "--feishu-signing-secret-env",
    }
    leaked_options = sorted(agentteam_only_options.intersection(command))
    if leaked_options:
        raise ModelInvocationIntegrityError(
            "registered Codex command contains AgentTeam runtime options: "
            + ", ".join(leaked_options)
        )
    models = []
    reasoning_profiles = []
    for index, value in enumerate(command):
        if value in {"-m", "--model"} and index + 1 < len(command):
            models.append(command[index + 1])
        elif isinstance(value, str) and value.startswith("--model="):
            models.append(value.partition("=")[2])
        if value == "-c" and index + 1 < len(command):
            configuration = command[index + 1]
            prefix = "model_reasoning_effort="
            if configuration.startswith(prefix):
                reasoning_profiles.append(
                    configuration[len(prefix):]
                )
    if models != [model_policy["model"]]:
        raise ModelInvocationIntegrityError(
            "Codex command model differs from experiment launch policy"
        )
    if reasoning_profiles != [model_policy["reasoning_profile"]]:
        raise ModelInvocationIntegrityError(
            "Codex command reasoning differs from experiment launch policy"
        )
    present = CONTEXT_BUDGET_POLICY_FIELDS.intersection(model_policy)
    if not present:
        return
    if present not in {
        LEGACY_CONTEXT_POLICY_FIELDS,
        CONTEXT_BUDGET_POLICY_FIELDS,
    }:
        raise ModelInvocationIntegrityError(
            "experiment context policy fields are incomplete"
        )
    try:
        policy = normalize_context_budget_policy(model_policy)
        expected_arguments = codex_context_policy_arguments(policy)
    except ValueError as exc:
        raise ModelInvocationIntegrityError(
            f"experiment context policy is invalid: {exc}"
        ) from exc
    configurations = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "-c" and index + 1 < len(command)
    ]
    expected_configurations = [
        expected_arguments[index + 1]
        for index, value in enumerate(expected_arguments)
        if value == "-c" and index + 1 < len(expected_arguments)
    ]
    configuration_prefixes = [
        "tool_output_token_limit=",
        "web_search=",
        "model_auto_compact_token_limit=",
        "hooks.PreToolUse=",
        "hooks.PostToolUse=",
    ]
    controlled_configurations = [
        value
        for value in configurations
        if any(value.startswith(prefix) for prefix in configuration_prefixes)
    ]
    if controlled_configurations != expected_configurations:
        raise ModelInvocationIntegrityError(
            "Codex command context policy differs from experiment launch policy"
        )
    expected_bypass_count = (
        1 if set(policy) == CONTEXT_BUDGET_POLICY_FIELDS else 0
    )
    if command.count(HOOK_TRUST_BYPASS_OPTION) != expected_bypass_count:
        raise ModelInvocationIntegrityError(
            "Codex command hook trust policy differs from experiment launch policy"
        )


def _validate_call_context(context, *, supported):
    if context.get("coverage_class") not in {
        "supported_model_invocation",
        "not_applicable_adapter",
    }:
        raise ModelInvocationIntegrityError(
            "coverage_class must be fixed before invocation start"
        )
    if supported and context.get("coverage_class") != "supported_model_invocation":
        raise ModelInvocationIntegrityError(
            "supported provider call requires supported coverage"
        )
    if not supported and context.get("coverage_class") != "not_applicable_adapter":
        raise ModelInvocationIntegrityError(
            "test provider call cannot claim supported coverage"
        )
    resource_binding = context.get("resource_envelope_binding")
    if resource_binding is None and context.get("resource_envelope_required") is True:
        raise ModelInvocationUnavailable(
            "required model-worker resource envelope is unavailable"
        )
    if resource_binding is not None:
        try:
            from .resource_envelope import validate_resource_envelope_binding

            validate_resource_envelope_binding(resource_binding)
        except (ImportError, RuntimeError, TypeError, ValueError) as exc:
            raise ModelInvocationIntegrityError(
                f"invalid model-worker resource envelope: {exc}"
            ) from exc
        if context.get("experiment_mode") not in {
            "single_codex",
            "agentteam_direct",
            "agentteam_full",
        }:
            raise ModelInvocationIntegrityError(
                "resource-bound model invocation requires experiment_mode"
            )
        if not isinstance(context.get("resource_hierarchy_reference"), dict):
            raise ModelInvocationIntegrityError(
                "resource-bound model invocation requires an owner reference"
            )
    required = (
        "project",
        "run_id",
        "taskpack_id",
        "runtime_execution_session_id",
        "lifecycle_owner_token",
        "usage_stage",
        "backend",
    )
    missing = [field for field in required if not _nonempty(context.get(field))]
    if missing:
        raise ModelInvocationIntegrityError(
            f"missing model invocation context: {','.join(missing)}"
        )
    if context["usage_stage"] not in _USAGE_STAGES:
        raise ModelInvocationIntegrityError("unsupported usage_stage")
    if supported:
        explicit = context.get("_explicit_context_fields")
        if not isinstance(explicit, dict):
            explicit = {
                field: _nonempty(context.get(field)) is not None
                for field in (
                    "project",
                    "run_id",
                    "taskpack_id",
                    "runtime_execution_session_id",
                    "lifecycle_owner_token",
                    "agent_id",
                    "role",
                    "usage_stage",
                )
            }
        missing_explicit = [
            field
            for field in (
                "project",
                "run_id",
                "taskpack_id",
                "runtime_execution_session_id",
                "lifecycle_owner_token",
                "agent_id",
                "role",
                "usage_stage",
            )
            if explicit.get(field) is not True
        ]
        if missing_explicit:
            raise ModelInvocationUnavailable(
                "supported invocation requires explicit context: "
                + ",".join(missing_explicit)
            )
    if context["usage_stage"] == "acceptance_live_smoke":
        if (
            not _nonempty(context.get("implementation_run_id"))
            or not isinstance(context.get("gate_epoch"), int)
            or context["gate_epoch"] < 1
        ):
            raise ModelInvocationIntegrityError(
                "acceptance invocation requires implementation_run_id and gate_epoch"
            )
    elif (
        context.get("implementation_run_id") is not None
        or context.get("gate_epoch") is not None
    ):
        raise ModelInvocationIntegrityError(
            "non-acceptance invocation cannot bind acceptance gate fields"
        )


if __name__ == "__main__":  # pragma: no cover - exercised by live systemd path
    if len(sys.argv) == 3 and sys.argv[1] == "_supervisor":
        raise SystemExit(_supervisor_main(sys.argv[2]))
    if (
        len(sys.argv) >= 7
        and sys.argv[1] == "_source_guard"
        and sys.argv[5] == "--"
    ):
        raise SystemExit(
            _source_guard_main(
                sys.argv[2],
                sys.argv[3],
                sys.argv[4],
                sys.argv[6:],
            )
        )
    raise SystemExit("model_invocation.py is an internal runtime helper")

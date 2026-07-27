"""Append-only, secret-safe operator action accounting for experiments."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .experiment_contract import (
    canonical_json_bytes,
    canonical_json_sha256,
    derive_experiment_run_id,
    schema_path,
)


LEDGER_SCHEMA_VERSION = "experiment_operator_action.v1"
POLICY_SCHEMA_VERSION = "experiment_operator_action_policy.v1"
ACTION_CLASSES = (
    "expected_operator_action",
    "corrective_intervention",
    "decision_escalation",
)
EXPERIMENT_MODES = (
    "single_codex",
    "agentteam_direct",
    "agentteam_full",
)

_POLICY_FILE = "operator-action-policy.json"
_LEDGER_FILE = "operator-actions.jsonl"
_CHECKPOINT_FILE = "operator-action-checkpoints.jsonl"
_SOURCE_EVENTS_FILE = "operator-input-events.jsonl"
_RUN_BINDINGS_FILE = "operator-run-bindings.jsonl"
_LOCK_FILE = "operator-actions.lock"
_MAX_LEDGER_BYTES = 16 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SOURCE_POLICY = {
    "manual_gate": (
        "decision_escalation",
        "operator answered an unresolved decision gate",
    ),
    "permission_request": (
        "expected_operator_action",
        "operator resolved a preregistered permission request",
    ),
    "run_stop": (
        "corrective_intervention",
        "operator interrupted active experiment work",
    ),
    "run_resume": (
        "corrective_intervention",
        "operator resumed interrupted experiment work",
    ),
    "corrective_guidance": (
        "corrective_intervention",
        "operator supplied corrective runtime guidance",
    ),
}
_SOURCE_EVENT_POLICY = {
    "manual_gate": (
        "manual_gate_required",
        "operator_answer_received",
        True,
    ),
    "permission_request": (
        "permission_request_required",
        "permission_request_resolved",
        True,
    ),
    "run_stop": ("run_stop_requested", None, False),
    "run_resume": ("run_resume_requested", None, False),
    "corrective_guidance": (
        "corrective_guidance_received",
        None,
        False,
    ),
}
_SOURCE_ID_FIELDS = (
    "question_id",
    "request_id",
    "gate_id",
    "decision_id",
    "task_id",
    "attempt_id",
)
_SOURCE_CORRELATION_FIELDS = {
    "manual_gate": "question_id",
    "permission_request": "request_id",
}


class ExperimentLedgerError(RuntimeError):
    """Base error for experiment operator-action accounting."""


class ExperimentLedgerIntegrityError(ExperimentLedgerError):
    """Raised when immutable policy or append-only ledger authority changed."""


class ExperimentOperatorActionLimitExceeded(ExperimentLedgerError):
    """Raised before an operator action would exceed its frozen class limit."""


class ExperimentOperatorActionLedger:
    """One protocol-global ledger with per-run, equal mode limits."""

    def __init__(self, ledger_root):
        self.root = _ledger_root(ledger_root, create=False)
        self._policy = _load_policy(self.root)
        self._validate_authority()

    @classmethod
    def create(
        cls,
        ledger_root,
        *,
        protocol_id,
        protocol_sha256,
        operator_limits,
    ):
        root = _ledger_root(ledger_root, create=True)
        protocol_id = _safe_id(protocol_id, "protocol_id")
        protocol_sha256 = _sha256(protocol_sha256, "protocol_sha256")
        operator_limits = _normalize_operator_limits(operator_limits)
        lock_path = root / _LOCK_FILE
        ledger_path = root / _LEDGER_FILE
        checkpoint_path = root / _CHECKPOINT_FILE
        source_events_path = root / _SOURCE_EVENTS_FILE
        run_bindings_path = root / _RUN_BINDINGS_FILE
        _touch_regular_file(lock_path)
        _touch_regular_file(ledger_path)
        _touch_regular_file(checkpoint_path)
        _touch_regular_file(source_events_path)
        _touch_regular_file(run_bindings_path)
        with _locked_file(lock_path):
            policy = {
                "schema_version": POLICY_SCHEMA_VERSION,
                "protocol_id": protocol_id,
                "protocol_sha256": protocol_sha256,
                "ledger_root": str(root),
                "operator_limits": operator_limits,
                "operator_limits_sha256": canonical_json_sha256(
                    operator_limits
                ),
                **_identity_fields("lock", _regular_file_identity(lock_path)),
                **_identity_fields(
                    "ledger",
                    _regular_file_identity(ledger_path),
                ),
                **_identity_fields(
                    "checkpoint",
                    _regular_file_identity(checkpoint_path),
                ),
                **_identity_fields(
                    "source_events",
                    _regular_file_identity(source_events_path),
                ),
                **_identity_fields(
                    "run_bindings",
                    _regular_file_identity(run_bindings_path),
                ),
            }
            policy_path = root / _POLICY_FILE
            if policy_path.exists():
                if _load_policy(root) != policy:
                    raise ExperimentLedgerIntegrityError(
                        "operator action policy differs from frozen authority"
                    )
            else:
                _exclusive_json(policy_path, policy)
                _append_checkpoint(
                    checkpoint_path,
                    ledger_bytes=b"",
                    entry_count=0,
                    previous_checkpoint_sha256=None,
                    expected_identity=_regular_file_identity(
                        checkpoint_path
                    ),
                )
        return cls(root)

    @property
    def policy(self):
        return copy.deepcopy(self._policy)

    def publish_source_event(self, event):
        event = _normalize_source_event(event)
        with self._lock():
            events = self._read_source_events()
            prior = next(
                (
                    item
                    for item in events
                    if item["event_id"] == event["event_id"]
                ),
                None,
            )
            if prior is not None:
                if (
                    prior["event_type"] != event["event_type"]
                    or prior["payload"] != event["payload"]
                ):
                    raise ExperimentLedgerIntegrityError(
                        "operator input event replay conflicts with authority"
                    )
                return copy.deepcopy(prior)
            _append_entry(
                self.root / _SOURCE_EVENTS_FILE,
                event,
                expected_identity=_policy_identity(
                    self._policy,
                    "source_events",
                ),
            )
            return copy.deepcopy(event)

    def bind_run(self, run_manifest):
        binding = _operator_run_binding(
            run_manifest,
            expected_protocol_sha256=self._policy["protocol_sha256"],
        )
        with self._lock():
            bindings = self._read_run_bindings()
            prior_slot = next(
                (
                    item
                    for item in bindings
                    if item["experiment_run_id"]
                    == binding["experiment_run_id"]
                ),
                None,
            )
            if prior_slot is not None:
                if prior_slot != binding:
                    raise ExperimentLedgerIntegrityError(
                        "operator run identity conflicts with frozen binding"
                    )
                return copy.deepcopy(prior_slot)
            _append_entry(
                self.root / _RUN_BINDINGS_FILE,
                binding,
                expected_identity=_policy_identity(
                    self._policy,
                    "run_bindings",
                ),
            )
            return copy.deepcopy(binding)

    def record(
        self,
        *,
        experiment_run_id,
        mode,
        request_source,
        request,
        response=None,
        requested_at,
        answered_at=None,
        related_task_id=None,
        related_attempt_id=None,
    ):
        experiment_run_id = _safe_id(
            experiment_run_id,
            "experiment_run_id",
        )
        if mode not in EXPERIMENT_MODES:
            raise ExperimentLedgerError(f"unsupported experiment mode: {mode!r}")
        try:
            action_class, reason = _SOURCE_POLICY[request_source]
        except (KeyError, TypeError) as exc:
            raise ExperimentLedgerError(
                f"unsupported operator request source: {request_source!r}"
            ) from exc
        _validate_source_events(request_source, request, response)
        _expected_request, _expected_response, response_required = (
            _SOURCE_EVENT_POLICY[request_source]
        )
        request = self.publish_source_event(request)
        requested_at = request["time"]
        if response_required:
            response = self.publish_source_event(response)
            answered_at = response["time"]
        else:
            answered_at = None
        requested_at = _timestamp(requested_at, "requested_at")
        answered_at = (
            _timestamp(answered_at, "answered_at")
            if answered_at is not None
            else None
        )
        if (
            answered_at is not None
            and _parse_timestamp(answered_at) < _parse_timestamp(requested_at)
        ):
            raise ExperimentLedgerError(
                "answered_at cannot precede requested_at"
            )
        request_digest = _payload_digest(request, "request")
        response_digest = (
            _payload_digest(response, "response")
            if response is not None
            else None
        )
        action_id = _action_id(
            experiment_run_id,
            mode,
            request_source,
            request_digest,
        )
        entry = {
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "action_id": action_id,
            "experiment_run_id": experiment_run_id,
            "mode": mode,
            "action_class": action_class,
            "requested_at": requested_at,
            "answered_at": answered_at,
            "request_source": request_source,
            "request_digest": request_digest,
            "response_digest": response_digest,
            "reason": reason,
            "related_task_id": _optional_safe_id(
                related_task_id,
                "related_task_id",
            ),
            "related_attempt_id": _optional_safe_id(
                related_attempt_id,
                "related_attempt_id",
            ),
            "counts_as_intervention": (
                action_class == "corrective_intervention"
            ),
        }
        validate_experiment_operator_action(entry)

        with self._lock():
            entries = self._read_entries()
            self._require_run_binding(
                experiment_run_id,
                mode,
            )
            prior_modes = {
                item["mode"]
                for item in entries
                if item["experiment_run_id"] == experiment_run_id
            }
            if prior_modes and prior_modes != {mode}:
                raise ExperimentLedgerIntegrityError(
                    "experiment run mode conflicts with prior ledger entries"
                )
            prior = next(
                (
                    item
                    for item in entries
                    if item["action_id"] == action_id
                ),
                None,
            )
            if prior is not None:
                if prior != entry:
                    raise ExperimentLedgerIntegrityError(
                        "operator action replay conflicts with prior entry"
                    )
                return {
                    "record_status": "already_recorded",
                    "entry": copy.deepcopy(prior),
                    "projection": _project(entries, experiment_run_id),
                }

            used = sum(
                1
                for item in entries
                if item["experiment_run_id"] == experiment_run_id
                and item["action_class"] == action_class
            )
            limit = self._policy["operator_limits"][action_class]
            if used >= limit:
                raise ExperimentOperatorActionLimitExceeded(
                    f"{action_class} limit exhausted for {experiment_run_id}"
                )
            _append_entry(
                self.root / _LEDGER_FILE,
                entry,
                expected_identity=_policy_identity(
                    self._policy,
                    "ledger",
                ),
            )
            entries.append(entry)
            checkpoint = self._latest_checkpoint()
            _append_checkpoint(
                self.root / _CHECKPOINT_FILE,
                ledger_bytes=_canonical_ledger_bytes(entries),
                entry_count=len(entries),
                previous_checkpoint_sha256=checkpoint[
                    "checkpoint_sha256"
                ],
                expected_identity=_policy_identity(
                    self._policy,
                    "checkpoint",
                ),
            )
            return {
                "record_status": "recorded",
                "entry": copy.deepcopy(entry),
                "projection": _project(entries, experiment_run_id),
            }

    def replay(self, *, experiment_run_id=None):
        if experiment_run_id is not None:
            experiment_run_id = _safe_id(
                experiment_run_id,
                "experiment_run_id",
            )
        with self._lock():
            entries = self._read_entries()
        return _project(entries, experiment_run_id)

    def _validate_authority(self):
        if self._policy["ledger_root"] != str(self.root):
            raise ExperimentLedgerIntegrityError(
                "operator action ledger root changed"
            )
        for prefix, filename in (
            ("lock", _LOCK_FILE),
            ("ledger", _LEDGER_FILE),
            ("checkpoint", _CHECKPOINT_FILE),
            ("source_events", _SOURCE_EVENTS_FILE),
            ("run_bindings", _RUN_BINDINGS_FILE),
        ):
            if _regular_file_identity(self.root / filename) != (
                _policy_identity(self._policy, prefix)
            ):
                raise ExperimentLedgerIntegrityError(
                    f"operator action {prefix} authority changed"
                )
        with self._lock():
            self._read_source_events()
            self._read_run_bindings()
            self._read_entries()

    def _lock(self):
        return _locked_file(
            self.root / _LOCK_FILE,
            expected_identity=_policy_identity(self._policy, "lock"),
        )

    def _read_entries(self):
        path = self.root / _LEDGER_FILE
        fd = _open_regular_file(
            path,
            os.O_RDWR,
            expected_identity=_policy_identity(self._policy, "ledger"),
        )
        try:
            size = os.fstat(fd).st_size
            if size > _MAX_LEDGER_BYTES:
                raise ExperimentLedgerIntegrityError(
                    "operator action ledger exceeds bounded size"
                )
            content = _read_recoverable_log_fd(
                fd,
                size,
                "operator action ledger",
            )
        finally:
            os.close(fd)
        entries = []
        action_ids = set()
        run_modes = {}
        run_counts = {}
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if len(raw_line) > _MAX_LINE_BYTES:
                raise ExperimentLedgerIntegrityError(
                    f"operator action ledger line {line_number} is oversized"
                )
            try:
                entry = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExperimentLedgerIntegrityError(
                    f"operator action ledger line {line_number} is invalid"
                ) from exc
            validate_experiment_operator_action(entry)
            if entry["action_id"] in action_ids:
                raise ExperimentLedgerIntegrityError(
                    "operator action ledger contains a duplicate action_id"
                )
            prior_mode = run_modes.setdefault(
                entry["experiment_run_id"],
                entry["mode"],
            )
            if prior_mode != entry["mode"]:
                raise ExperimentLedgerIntegrityError(
                    "operator action ledger contains run mode drift"
                )
            count_key = (
                entry["experiment_run_id"],
                entry["action_class"],
            )
            run_counts[count_key] = run_counts.get(count_key, 0) + 1
            if run_counts[count_key] > self._policy["operator_limits"][
                entry["action_class"]
            ]:
                raise ExperimentLedgerIntegrityError(
                    "operator action ledger exceeds frozen action limits"
                )
            action_ids.add(entry["action_id"])
            entries.append(entry)
        bindings = {
            (item["experiment_run_id"], item["mode"])
            for item in self._read_run_bindings()
        }
        for entry in entries:
            if (entry["experiment_run_id"], entry["mode"]) not in bindings:
                raise ExperimentLedgerIntegrityError(
                    "operator action lacks a frozen run binding"
                )
        self._validate_source_correlations(entries)
        self._validate_checkpoint(content, entries)
        return entries

    def _latest_checkpoint(self):
        return self._read_checkpoints()[-1]

    def _validate_checkpoint(self, ledger_bytes, entries):
        checkpoint = self._latest_checkpoint()
        checkpoint_count = checkpoint["entry_count"]
        if checkpoint_count > len(entries):
            raise ExperimentLedgerIntegrityError(
                "operator action checkpoint is ahead of its ledger"
            )
        checkpoint_bytes = _canonical_ledger_bytes(
            entries[:checkpoint_count]
        )
        if (
            checkpoint["ledger_size"] != len(checkpoint_bytes)
            or checkpoint["ledger_sha256"]
            != hashlib.sha256(checkpoint_bytes).hexdigest()
        ):
            raise ExperimentLedgerIntegrityError(
                "operator action ledger conflicts with checkpoint authority"
            )
        previous = checkpoint["checkpoint_sha256"]
        for entry_count in range(checkpoint_count + 1, len(entries) + 1):
            prefix = _canonical_ledger_bytes(entries[:entry_count])
            _append_checkpoint(
                self.root / _CHECKPOINT_FILE,
                ledger_bytes=prefix,
                entry_count=entry_count,
                previous_checkpoint_sha256=previous,
                expected_identity=_policy_identity(
                    self._policy,
                    "checkpoint",
                ),
            )
            previous = self._latest_checkpoint()[
                "checkpoint_sha256"
            ]

    def _read_checkpoints(self):
        path = self.root / _CHECKPOINT_FILE
        fd = _open_regular_file(
            path,
            os.O_RDWR,
            expected_identity=_policy_identity(
                self._policy,
                "checkpoint",
            ),
        )
        try:
            size = os.fstat(fd).st_size
            if size > _MAX_LEDGER_BYTES:
                raise ExperimentLedgerIntegrityError(
                    "operator action checkpoints exceed bounded size"
                )
            content = _read_recoverable_log_fd(
                fd,
                size,
                "operator action checkpoint",
            )
        finally:
            os.close(fd)
        checkpoints = []
        previous = None
        for line_number, raw_line in enumerate(
            content.splitlines(),
            start=1,
        ):
            if not raw_line.strip():
                continue
            try:
                checkpoint = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ExperimentLedgerIntegrityError(
                    f"operator action checkpoint {line_number} is invalid"
                ) from exc
            _validate_checkpoint(checkpoint, previous, len(checkpoints))
            previous = checkpoint["checkpoint_sha256"]
            checkpoints.append(checkpoint)
        if not checkpoints:
            _append_checkpoint(
                self.root / _CHECKPOINT_FILE,
                ledger_bytes=b"",
                entry_count=0,
                previous_checkpoint_sha256=None,
                expected_identity=_policy_identity(
                    self._policy,
                    "checkpoint",
                ),
            )
            return self._read_checkpoints()
        return checkpoints

    def _read_source_events(self):
        path = self.root / _SOURCE_EVENTS_FILE
        fd = _open_regular_file(
            path,
            os.O_RDWR,
            expected_identity=_policy_identity(
                self._policy,
                "source_events",
            ),
        )
        try:
            size = os.fstat(fd).st_size
            if size > _MAX_LEDGER_BYTES:
                raise ExperimentLedgerIntegrityError(
                    "operator input events exceed bounded size"
                )
            content = _read_recoverable_log_fd(
                fd,
                size,
                "operator input event",
            )
        finally:
            os.close(fd)
        events = []
        event_ids = set()
        for line_number, raw_line in enumerate(
            content.splitlines(),
            start=1,
        ):
            if not raw_line.strip():
                continue
            try:
                event = _normalize_source_event(json.loads(raw_line))
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ExperimentLedgerError,
            ) as exc:
                raise ExperimentLedgerIntegrityError(
                    f"operator input event {line_number} is invalid"
                ) from exc
            if event["event_id"] in event_ids:
                raise ExperimentLedgerIntegrityError(
                    "operator input authority contains a duplicate event_id"
                )
            event_ids.add(event["event_id"])
            events.append(event)
        return events

    def _read_run_bindings(self):
        path = self.root / _RUN_BINDINGS_FILE
        fd = _open_regular_file(
            path,
            os.O_RDWR,
            expected_identity=_policy_identity(
                self._policy,
                "run_bindings",
            ),
        )
        try:
            size = os.fstat(fd).st_size
            if size > _MAX_LEDGER_BYTES:
                raise ExperimentLedgerIntegrityError(
                    "operator run bindings exceed bounded size"
                )
            content = _read_recoverable_log_fd(
                fd,
                size,
                "operator run binding",
            )
        finally:
            os.close(fd)
        bindings = []
        run_ids = set()
        for line_number, raw_line in enumerate(
            content.splitlines(),
            start=1,
        ):
            if not raw_line.strip():
                continue
            try:
                binding = json.loads(raw_line)
                binding = _operator_run_binding(
                    binding,
                    expected_protocol_sha256=self._policy[
                        "protocol_sha256"
                    ],
                    stored=True,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ExperimentLedgerError,
            ) as exc:
                raise ExperimentLedgerIntegrityError(
                    f"operator run binding {line_number} is invalid"
                ) from exc
            if binding["experiment_run_id"] in run_ids:
                raise ExperimentLedgerIntegrityError(
                    "operator run binding authority contains duplicates"
                )
            run_ids.add(binding["experiment_run_id"])
            bindings.append(binding)
        return bindings

    def _require_run_binding(self, experiment_run_id, mode):
        if not any(
            item["experiment_run_id"] == experiment_run_id
            and item["mode"] == mode
            for item in self._read_run_bindings()
        ):
            raise ExperimentLedgerIntegrityError(
                "operator action run is not registered"
            )

    def _validate_source_correlations(self, entries):
        source_events = self._read_source_events()
        source_digests = {
            hashlib.sha256(canonical_json_bytes(event)).hexdigest()
            for event in source_events
        }
        for entry in entries:
            _request_type, _response_type, response_required = (
                _SOURCE_EVENT_POLICY[entry["request_source"]]
            )
            correlated_digests = [entry["request_digest"]]
            if response_required:
                correlated_digests.append(entry["response_digest"])
            if any(
                digest not in source_digests
                for digest in correlated_digests
            ):
                raise ExperimentLedgerIntegrityError(
                    "operator action lacks retained source event authority"
                )


def create_experiment_operator_action_ledger(ledger_root, **kwargs):
    return ExperimentOperatorActionLedger.create(ledger_root, **kwargs)


def load_experiment_operator_action_ledger(ledger_root):
    return ExperimentOperatorActionLedger(ledger_root)


def validate_experiment_operator_action(entry):
    validator = Draft202012Validator(
        json.loads(
            schema_path("experiment_operator_action.schema.json").read_text(
                encoding="utf-8"
            )
        ),
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(entry), key=lambda item: list(item.path))
    if errors:
        raise ExperimentLedgerIntegrityError(
            f"invalid experiment operator action: {errors[0].message}"
        )
    expected_class, expected_reason = _SOURCE_POLICY[
        entry["request_source"]
    ]
    if (
        entry["action_class"] != expected_class
        or entry["reason"] != expected_reason
    ):
        raise ExperimentLedgerIntegrityError(
            "operator action classification conflicts with request source"
        )
    expected_intervention = (
        entry["action_class"] == "corrective_intervention"
    )
    if entry["counts_as_intervention"] is not expected_intervention:
        raise ExperimentLedgerIntegrityError(
            "operator intervention flag conflicts with action class"
        )
    expected_action_id = _action_id(
        entry["experiment_run_id"],
        entry["mode"],
        entry["request_source"],
        entry["request_digest"],
    )
    if entry["action_id"] != expected_action_id:
        raise ExperimentLedgerIntegrityError(
            "operator action id conflicts with its request binding"
        )
    try:
        requested_at = _parse_timestamp(entry["requested_at"])
        answered_at = (
            _parse_timestamp(entry["answered_at"])
            if entry["answered_at"] is not None
            else None
        )
    except ExperimentLedgerError as exc:
        raise ExperimentLedgerIntegrityError(
            "operator action timestamp is invalid"
        ) from exc
    if answered_at is not None and answered_at < requested_at:
        raise ExperimentLedgerIntegrityError(
            "operator action answer precedes its request"
        )
    return entry


def _project(entries, experiment_run_id):
    selected = [
        copy.deepcopy(item)
        for item in entries
        if experiment_run_id is None
        or item["experiment_run_id"] == experiment_run_id
    ]
    counts = {
        action_class: sum(
            1
            for item in selected
            if item["action_class"] == action_class
        )
        for action_class in ACTION_CLASSES
    }
    return {
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "experiment_run_id": experiment_run_id,
        "action_count": len(selected),
        "operator_action_counts": counts,
        "intervention_count": counts["corrective_intervention"],
        "entries": selected,
    }


def _action_id(experiment_run_id, mode, request_source, request_digest):
    digest = hashlib.sha256(
        b"agentteam:experiment_operator_action.v1\x00"
        + canonical_json_bytes(
            {
                "experiment_run_id": experiment_run_id,
                "mode": mode,
                "request_digest": request_digest,
                "request_source": request_source,
            }
        )
    ).hexdigest()
    return f"ACTION-{digest}"


def _payload_digest(value, label):
    try:
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    except Exception as exc:
        raise ExperimentLedgerError(
            f"{label} must be canonical JSON"
        ) from exc


def _normalize_source_event(value):
    required = {"event_id", "event_type", "time", "payload"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ExperimentLedgerError(
            "operator input source event fields are invalid"
        )
    event_id = _safe_id(value["event_id"], "event_id")
    event_type = value["event_type"]
    allowed_types = {
        request_type
        for request_type, _response_type, _response_required in (
            _SOURCE_EVENT_POLICY.values()
        )
    } | {
        response_type
        for _request_type, response_type, response_required in (
            _SOURCE_EVENT_POLICY.values()
        )
        if response_required
    }
    if event_type not in allowed_types:
        raise ExperimentLedgerError(
            "operator input source event type is unsupported"
        )
    time_value = _timestamp(value["time"], "source event time")
    raw_payload = value["payload"]
    if not isinstance(raw_payload, dict):
        raise ExperimentLedgerError(
            "operator input source event payload must be an object"
        )
    if "payload_sha256" in raw_payload:
        allowed = {"payload_sha256", *_SOURCE_ID_FIELDS}
        if not set(raw_payload).issubset(allowed):
            raise ExperimentLedgerError(
                "sanitized operator input payload fields are invalid"
            )
        _sha256(raw_payload["payload_sha256"], "payload_sha256")
        safe_payload = copy.deepcopy(raw_payload)
    else:
        safe_payload = {
            "payload_sha256": _payload_digest(
                raw_payload,
                "source event payload",
            )
        }
        for field in _SOURCE_ID_FIELDS:
            value = raw_payload.get(field)
            if value is not None:
                safe_payload[field] = _safe_id(value, field)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "time": time_value,
        "payload": safe_payload,
    }


def _validate_source_events(request_source, request, response):
    expected_request, expected_response, response_required = (
        _SOURCE_EVENT_POLICY[request_source]
    )
    if (
        not isinstance(request, dict)
        or request.get("event_type") != expected_request
    ):
        raise ExperimentLedgerError(
            "operator request does not match its classified source"
        )
    if response_required:
        if (
            not isinstance(response, dict)
            or response.get("event_type") != expected_response
        ):
            raise ExperimentLedgerError(
                "operator response does not match its classified source"
            )
    elif response is not None:
        raise ExperimentLedgerError(
            "request-only operator action cannot include a response event"
        )
    correlation_field = _SOURCE_CORRELATION_FIELDS.get(request_source)
    if correlation_field is not None:
        request_payload = request.get("payload")
        response_payload = response.get("payload")
        if (
            not isinstance(request_payload, dict)
            or not isinstance(response_payload, dict)
            or request_payload.get(correlation_field) is None
            or request_payload.get(correlation_field)
            != response_payload.get(correlation_field)
        ):
            raise ExperimentLedgerError(
                "operator request and response identities do not match"
            )


def _normalize_operator_limits(value):
    if not isinstance(value, dict) or set(value) != set(ACTION_CLASSES):
        raise ExperimentLedgerError(
            "operator_limits must define the closed action vocabulary"
        )
    normalized = {}
    for action_class in ACTION_CLASSES:
        limit = value[action_class]
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
        ):
            raise ExperimentLedgerError(
                f"{action_class} limit must be a non-negative integer"
            )
        normalized[action_class] = limit
    return normalized


def _operator_run_binding(
    value,
    *,
    expected_protocol_sha256,
    stored=False,
):
    if not isinstance(value, dict):
        raise ExperimentLedgerError(
            "operator run binding must be an object"
        )
    if stored:
        required = {
            "schema_version",
            "experiment_run_id",
            "protocol_sha256",
            "mode",
            "repetition_index",
            "stable_request_key",
            "run_manifest_sha256",
        }
        if set(value) != required:
            raise ExperimentLedgerError(
                "operator run binding fields are invalid"
            )
        binding = copy.deepcopy(value)
    else:
        required = {
            "experiment_run_id",
            "protocol_sha256",
            "mode",
            "repetition_index",
            "stable_request_key",
        }
        if not required.issubset(value):
            raise ExperimentLedgerError(
                "operator run manifest is incomplete"
            )
        binding = {
            "schema_version": "experiment_operator_run_binding.v1",
            "experiment_run_id": value["experiment_run_id"],
            "protocol_sha256": value["protocol_sha256"],
            "mode": value["mode"],
            "repetition_index": value["repetition_index"],
            "stable_request_key": value["stable_request_key"],
            "run_manifest_sha256": canonical_json_sha256(value),
        }
    if (
        binding["schema_version"]
        != "experiment_operator_run_binding.v1"
        or binding["protocol_sha256"] != expected_protocol_sha256
        or binding["mode"] not in EXPERIMENT_MODES
        or not isinstance(binding["repetition_index"], int)
        or isinstance(binding["repetition_index"], bool)
        or binding["repetition_index"] < 0
    ):
        raise ExperimentLedgerError(
            "operator run binding identity is invalid"
        )
    _safe_id(binding["experiment_run_id"], "experiment_run_id")
    _safe_id(binding["stable_request_key"], "stable_request_key")
    _sha256(binding["run_manifest_sha256"], "run_manifest_sha256")
    expected_run_id = derive_experiment_run_id(
        binding["protocol_sha256"],
        binding["mode"],
        binding["repetition_index"],
        binding["stable_request_key"],
    )
    if binding["experiment_run_id"] != expected_run_id:
        raise ExperimentLedgerError(
            "operator run binding has an invalid experiment_run_id"
        )
    return binding


def _load_policy(root):
    path = root / _POLICY_FILE
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentLedgerIntegrityError(
            "operator action policy is unavailable"
        ) from exc
    required = {
        "schema_version",
        "protocol_id",
        "protocol_sha256",
        "ledger_root",
        "operator_limits",
        "operator_limits_sha256",
        "lock_device",
        "lock_inode",
        "ledger_device",
        "ledger_inode",
        "checkpoint_device",
        "checkpoint_inode",
        "source_events_device",
        "source_events_inode",
        "run_bindings_device",
        "run_bindings_inode",
    }
    if not isinstance(policy, dict) or set(policy) != required:
        raise ExperimentLedgerIntegrityError(
            "operator action policy fields are invalid"
        )
    if policy["schema_version"] != POLICY_SCHEMA_VERSION:
        raise ExperimentLedgerIntegrityError(
            "operator action policy version is unsupported"
        )
    _safe_id(policy["protocol_id"], "protocol_id")
    _sha256(policy["protocol_sha256"], "protocol_sha256")
    limits = _normalize_operator_limits(policy["operator_limits"])
    if policy["operator_limits_sha256"] != canonical_json_sha256(limits):
        raise ExperimentLedgerIntegrityError(
            "operator action limit digest changed"
        )
    return policy


def _ledger_root(value, *, create):
    path = Path(value).expanduser()
    if path.exists() and path.is_symlink():
        raise ExperimentLedgerIntegrityError(
            "operator action ledger root cannot be a symlink"
        )
    if create:
        path.mkdir(parents=True, exist_ok=True)
    path = path.resolve()
    if not path.is_dir():
        raise ExperimentLedgerIntegrityError(
            "operator action ledger root is unavailable"
        )
    return path


def _touch_regular_file(path):
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if not os.path.isfile(path) or path.is_symlink():
            raise ExperimentLedgerIntegrityError(
                f"operator action authority is not regular: {path}"
            )
    finally:
        os.close(fd)


def _regular_file_identity(path):
    if path.is_symlink() or not path.is_file():
        raise ExperimentLedgerIntegrityError(
            f"operator action authority is not regular: {path}"
        )
    stat_result = path.stat()
    return stat_result.st_dev, stat_result.st_ino


def _identity_fields(prefix, identity):
    return {
        f"{prefix}_device": identity[0],
        f"{prefix}_inode": identity[1],
    }


def _policy_identity(policy, prefix):
    return policy[f"{prefix}_device"], policy[f"{prefix}_inode"]


def _open_regular_file(path, flags, *, expected_identity):
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    stat_result = os.fstat(fd)
    if (stat_result.st_dev, stat_result.st_ino) != expected_identity:
        os.close(fd)
        raise ExperimentLedgerIntegrityError(
            "operator action authority identity changed"
        )
    return fd


@contextmanager
def _locked_file(path, *, expected_identity=None):
    identity = expected_identity or _regular_file_identity(path)
    fd = _open_regular_file(path, os.O_RDWR, expected_identity=identity)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _exclusive_json(path, value):
    staging_path = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = canonical_json_bytes(value) + b"\n"
    try:
        fd = os.open(staging_path, flags, 0o600)
    except FileExistsError as exc:
        raise ExperimentLedgerIntegrityError(
            "operator action policy staging publication raced"
        ) from exc
    try:
        try:
            _write_all_fd(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(staging_path, path, follow_symlinks=False)
            _fsync_directory(path.parent)
        except FileExistsError as exc:
            raise ExperimentLedgerIntegrityError(
                "operator action policy publication raced"
            ) from exc
    finally:
        staging_path.unlink(missing_ok=True)


def _append_entry(path, entry, *, expected_identity):
    payload = canonical_json_bytes(entry) + b"\n"
    if len(payload) > _MAX_LINE_BYTES:
        raise ExperimentLedgerError("operator action entry is oversized")
    fd = _open_regular_file(
        path,
        os.O_WRONLY | os.O_APPEND,
        expected_identity=expected_identity,
    )
    try:
        if os.fstat(fd).st_size + len(payload) > _MAX_LEDGER_BYTES:
            raise ExperimentLedgerError(
                "operator action ledger capacity is exhausted"
            )
        _write_all_fd(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_checkpoint(
    path,
    *,
    ledger_bytes,
    entry_count,
    previous_checkpoint_sha256,
    expected_identity,
):
    payload = {
        "schema_version": "experiment_operator_action_checkpoint.v1",
        "sequence": entry_count,
        "entry_count": entry_count,
        "ledger_size": len(ledger_bytes),
        "ledger_sha256": hashlib.sha256(ledger_bytes).hexdigest(),
        "previous_checkpoint_sha256": previous_checkpoint_sha256,
    }
    checkpoint = {
        **payload,
        "checkpoint_sha256": canonical_json_sha256(payload),
    }
    _append_entry(path, checkpoint, expected_identity=expected_identity)


def _validate_checkpoint(checkpoint, previous_sha256, expected_sequence):
    required = {
        "schema_version",
        "sequence",
        "entry_count",
        "ledger_size",
        "ledger_sha256",
        "previous_checkpoint_sha256",
        "checkpoint_sha256",
    }
    if not isinstance(checkpoint, dict) or set(checkpoint) != required:
        raise ExperimentLedgerIntegrityError(
            "operator action checkpoint fields are invalid"
        )
    if (
        checkpoint["schema_version"]
        != "experiment_operator_action_checkpoint.v1"
        or checkpoint["sequence"] != expected_sequence
        or checkpoint["entry_count"] != expected_sequence
        or not isinstance(checkpoint["ledger_size"], int)
        or isinstance(checkpoint["ledger_size"], bool)
        or checkpoint["ledger_size"] < 0
        or checkpoint["previous_checkpoint_sha256"] != previous_sha256
    ):
        raise ExperimentLedgerIntegrityError(
            "operator action checkpoint sequence is invalid"
        )
    _sha256(checkpoint["ledger_sha256"], "ledger_sha256")
    _sha256(checkpoint["checkpoint_sha256"], "checkpoint_sha256")
    payload = {
        key: value
        for key, value in checkpoint.items()
        if key != "checkpoint_sha256"
    }
    if checkpoint["checkpoint_sha256"] != canonical_json_sha256(payload):
        raise ExperimentLedgerIntegrityError(
            "operator action checkpoint digest is invalid"
        )


def _canonical_ledger_bytes(entries):
    return b"".join(canonical_json_bytes(entry) + b"\n" for entry in entries)


def _read_recoverable_log_fd(fd, expected_size, label):
    content = _read_exact_fd(fd, expected_size)
    if not content or content.endswith(b"\n"):
        return content
    final_newline = content.rfind(b"\n")
    if final_newline < 0:
        os.ftruncate(fd, 0)
        os.fsync(fd)
        return b""
    recovered_size = final_newline + 1
    os.ftruncate(fd, recovered_size)
    os.fsync(fd)
    return content[:recovered_size]


def _write_all_fd(fd, payload):
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise ExperimentLedgerIntegrityError(
                "operator authority write was incomplete"
            )
        offset += written


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_exact_fd(fd, expected_size):
    chunks = []
    remaining = expected_size
    while remaining:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            raise ExperimentLedgerIntegrityError(
                "operator action ledger was truncated during replay"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise ExperimentLedgerIntegrityError(
            "operator action ledger changed during replay"
        )
    return b"".join(chunks)


def _safe_id(value, label):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ExperimentLedgerError(f"{label} must be a safe identifier")
    return value


def _sha256(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ExperimentLedgerError(f"{label} must be a lowercase SHA-256")
    return value


def _optional_safe_id(value, label):
    if value is None:
        return None
    return _safe_id(value, label)


def _timestamp(value, label):
    if not isinstance(value, str):
        raise ExperimentLedgerError(f"{label} must be an RFC3339 timestamp")
    _parse_timestamp(value)
    return value


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperimentLedgerError(
            "operator action timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise ExperimentLedgerError(
            "operator action timestamp must include a timezone"
        )
    return parsed.astimezone(UTC)

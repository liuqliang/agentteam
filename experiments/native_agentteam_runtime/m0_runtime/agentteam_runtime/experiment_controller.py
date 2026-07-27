"""Durable provider admission around the pure experiment budget projection.

The controller owns the protocol-global provider lane.  It deliberately keeps
provider and scheduler policy out of :mod:`experiment_budget`: admission first
observes the frozen budget, and the lane is released only after immutable
terminal evidence has been imported and projected.

The authority detects ordinary file replacement, torn writes, and projection
rollback.  As with the experiment sandbox registry, the trusted controller is
the single writer; deliberate same-UID host tampering with every paired
authority file is outside the provider threat model.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .experiment_budget import (
    advance_experiment_budget,
    create_experiment_budget_state,
    validate_experiment_budget_state,
)
from .experiment_ledger import (
    create_experiment_operator_action_ledger,
    load_experiment_operator_action_ledger,
)
from .experiment_contract import (
    canonical_json_sha256,
    validate_experiment_protocol,
    validate_experiment_run_manifest,
)
from .model_invocation import (
    ModelInvocationIntegrityError,
    import_model_invocation_lifecycle,
    replay_model_invocation_events,
)


CONTROLLER_SCHEMA_VERSION = "experiment_budget_controller.v2"
LEGACY_CONTROLLER_SCHEMA_VERSION = "experiment_budget_controller.v1"
CONTROLLER_STATE_SCHEMA_VERSION = "experiment_budget_controller_state.v2"
CONTROLLER_STATE_CHECKPOINT_SCHEMA_VERSION = (
    "experiment_budget_controller_checkpoint.v1"
)
CONTROLLER_REFERENCE_SCHEMA_VERSION = (
    "experiment_budget_controller_reference.v1"
)
PROVIDER_LANE_SCHEMA_VERSION = "experiment_provider_lane.v1"

_CONTROLLER_METADATA = "experiment-budget-controller.json"
_CONTROLLER_STATE = "experiment-budget-controller-state.json"
_CONTROLLER_STATE_LOCK = "experiment-budget-controller-state.lock"
_CONTROLLER_STATE_JOURNAL = "experiment-budget-controller-state.jsonl"
_PROVIDER_LANE = "experiment-provider-lane.lock"
_AUTHORITY_EVENTS = "events.jsonl"
_BUDGET_EVENTS = "experiment-budget-events.jsonl"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROLLER_STATUSES = {
    "active",
    "budget_draining",
    "budget_stopped",
    "interrupted",
}
_BOUNDARIES = {
    "pre_provider_launch",
    "post_invocation_terminal",
    "pre_integration",
    "post_integration",
}
_OPERATOR_ACTION_CLASSES = {
    "expected_operator_action",
    "corrective_intervention",
    "decision_escalation",
}
_OPERATOR_RUNTIME_REQUEST_TYPES = {
    "manual_gate": "manual_gate_required",
    "permission_request": "permission_request_required",
}


class ExperimentControllerError(RuntimeError):
    """Base error for durable experiment-controller failures."""


class ExperimentControllerIntegrityError(ExperimentControllerError):
    """Raised when controller authority or frozen budget identity changed."""


class ExperimentProviderAdmissionDenied(ExperimentControllerError):
    """Raised before provider launch when the protocol lane is unavailable."""


class ExperimentProviderLaneBusy(ExperimentProviderAdmissionDenied):
    """Raised when another run currently owns the protocol-global lane."""


class ProviderAdmission:
    """One held protocol-global lane; only accounting may normally release it."""

    def __init__(self, controller, fd, record):
        self.controller = controller
        self.record = dict(record)
        self._fd = fd

    @property
    def held(self):
        return self._fd is not None

    def abandon_before_start(self):
        """Release an admission only when no durable invocation start exists."""
        self.controller._abandon_before_start(self)

    def _release(self):
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __del__(self):
        try:
            self._release()
        except OSError:
            pass


class ExperimentController:
    """Protocol-global provider admission and terminal-accounting authority."""

    def __init__(self, controller_root, *, monotonic=None):
        self.root = _controller_root(controller_root, create=False)
        self._monotonic = monotonic
        self._metadata = _load_metadata(self.root)
        self._validate_authority()

    @classmethod
    def create(
        cls,
        controller_root,
        *,
        protocol_id,
        max_total_tokens,
        max_wall_time_seconds,
        soft_warning_ratio,
        budget_id=None,
        scored=True,
        protocol_sha256=None,
        operator_limits=None,
        initial_monotonic=None,
        monotonic=None,
    ):
        root = _controller_root(controller_root, create=True)
        protocol_id = _safe_id(protocol_id, "protocol_id")
        budget_id = _safe_id(
            budget_id or f"{protocol_id}-global",
            "budget_id",
        )
        if not isinstance(scored, bool):
            raise ExperimentControllerError("scored must be a boolean")
        protocol_sha256, operator_limits = _operator_policy_binding(
            protocol_sha256,
            operator_limits,
        )
        _touch_regular_file(root / _AUTHORITY_EVENTS)
        state_lock_path = root / _CONTROLLER_STATE_LOCK
        state_journal_path = root / _CONTROLLER_STATE_JOURNAL
        provider_lane_path = root / _PROVIDER_LANE
        budget_events_path = root / _BUDGET_EVENTS
        _touch_regular_file(state_lock_path)
        _touch_regular_file(state_journal_path)
        _touch_regular_file(provider_lane_path)
        _touch_regular_file(budget_events_path)
        metadata_path = root / _CONTROLLER_METADATA
        existing_metadata = (
            _load_metadata(root) if metadata_path.exists() else None
        )
        operator_ledger_binding = (
            _operator_ledger_binding(
                root,
                protocol_id=protocol_id,
                protocol_sha256=protocol_sha256,
                operator_limits=operator_limits,
                create=True,
            )
            if existing_metadata is None and protocol_sha256 is not None
            else {
                "operator_ledger_root_device": (
                    existing_metadata[
                        "operator_ledger_root_device"
                    ]
                    if existing_metadata is not None
                    else None
                ),
                "operator_ledger_root_inode": (
                    existing_metadata[
                        "operator_ledger_root_inode"
                    ]
                    if existing_metadata is not None
                    else None
                ),
                "operator_ledger_policy_sha256": (
                    existing_metadata[
                        "operator_ledger_policy_sha256"
                    ]
                    if existing_metadata is not None
                    else None
                ),
            }
        )
        expected_state_lock_identity = _metadata_file_identity(
            existing_metadata,
            "state_lock",
        )
        with _locked_file(
            state_lock_path,
            expected_identity=expected_state_lock_identity,
        ):
            state_path = root / _CONTROLLER_STATE
            if metadata_path.exists():
                metadata = existing_metadata or _load_metadata(root)
                document = _read_state_document(state_path)
                budget_state = document["budget_state"]
                validate_experiment_budget_state(budget_state)
                if (
                    metadata["protocol_id"] != protocol_id
                    or metadata["budget_id"] != budget_id
                    or metadata["scored"] is not scored
                    or metadata["protocol_sha256"] != protocol_sha256
                    or metadata["operator_limits"] != operator_limits
                ):
                    raise ExperimentControllerIntegrityError(
                        "existing controller binding differs from requested "
                        "binding"
                    )
                _require_requested_budget(
                    budget_state,
                    budget_id=budget_id,
                    max_total_tokens=max_total_tokens,
                    max_wall_time_seconds=max_wall_time_seconds,
                    soft_warning_ratio=soft_warning_ratio,
                )
            else:
                if state_path.exists():
                    document = _read_state_document(state_path)
                    budget_state = document["budget_state"]
                    validate_experiment_budget_state(budget_state)
                    _require_requested_budget(
                        budget_state,
                        budget_id=budget_id,
                        max_total_tokens=max_total_tokens,
                        max_wall_time_seconds=max_wall_time_seconds,
                        soft_warning_ratio=soft_warning_ratio,
                    )
                else:
                    budget_state = create_experiment_budget_state(
                        budget_id,
                        max_total_tokens,
                        max_wall_time_seconds,
                        soft_warning_ratio,
                        authority_root=root,
                        authority_events_path=_AUTHORITY_EVENTS,
                        initial_monotonic=initial_monotonic,
                        monotonic=monotonic,
                    )
                    document = _state_document(
                        "active",
                        budget_state,
                        checkpoint_sequence=0,
                    )
                    _append_state_checkpoint(
                        state_journal_path,
                        document,
                        expected_identity=None,
                    )
                    _atomic_write_json(state_path, document)

                metadata = {
                    "schema_version": CONTROLLER_SCHEMA_VERSION,
                    "protocol_id": protocol_id,
                    "budget_id": budget_id,
                    "scored": scored,
                    "protocol_sha256": protocol_sha256,
                    "operator_limits": operator_limits,
                    "operator_limits_sha256": (
                        _canonical_json_sha256(operator_limits)
                        if operator_limits is not None
                        else None
                    ),
                    **operator_ledger_binding,
                    "controller_root": str(root),
                    "authority_events_path": _AUTHORITY_EVENTS,
                    "budget_events_path": _BUDGET_EVENTS,
                    "frozen_budget_sha256": budget_state[
                        "frozen_budget_sha256"
                    ],
                    "max_total_tokens": budget_state["max_total_tokens"],
                    "max_wall_time_seconds": budget_state[
                        "max_wall_time_seconds"
                    ],
                    "soft_warning_ratio": budget_state[
                        "soft_warning_ratio"
                    ],
                    "initial_monotonic": budget_state[
                        "initial_monotonic"
                    ],
                    **_identity_metadata(
                        "state_lock",
                        _regular_file_identity(state_lock_path),
                    ),
                    **_identity_metadata(
                        "provider_lane",
                        _regular_file_identity(provider_lane_path),
                    ),
                    **_identity_metadata(
                        "state_journal",
                        _regular_file_identity(state_journal_path),
                    ),
                    **_identity_metadata(
                        "budget_events",
                        _regular_file_identity(budget_events_path),
                    ),
                }
                _exclusive_or_idempotent_json(metadata_path, metadata)
        return cls(root, monotonic=monotonic)

    @property
    def reference(self):
        metadata_sha256 = _file_sha256(self.root / _CONTROLLER_METADATA)
        return {
            "schema_version": CONTROLLER_REFERENCE_SCHEMA_VERSION,
            "protocol_id": self._metadata["protocol_id"],
            "budget_id": self._metadata["budget_id"],
            "controller_root": str(self.root),
            "controller_metadata_sha256": metadata_sha256,
            "frozen_budget_sha256": self._metadata[
                "frozen_budget_sha256"
            ],
        }

    @property
    def controller_status(self):
        with self._state_lock():
            return self._load_document()["controller_status"]

    @property
    def budget_state(self):
        with self._state_lock():
            return copy.deepcopy(self._load_document()["budget_state"])

    def snapshot(self):
        with self._state_lock():
            return copy.deepcopy(self._load_document())

    def record_operator_action(
        self,
        *,
        protocol,
        run_manifest,
        run_dir,
        request_source,
        request,
        response=None,
        requested_at,
        answered_at=None,
        related_task_id=None,
        related_attempt_id=None,
    ):
        """Record one input under the protocol-global frozen action policy."""
        protocol, run_manifest = self._validated_operator_run_binding(
            protocol,
            run_manifest,
        )
        _validate_operator_target_authority(
            self.reference,
            run_manifest,
            run_dir,
            request_source=request_source,
            request=request,
        )
        ledger = self._operator_action_ledger()
        ledger.bind_run(run_manifest)
        return ledger.record(
            experiment_run_id=run_manifest["experiment_run_id"],
            mode=run_manifest["mode"],
            request_source=request_source,
            request=request,
            response=response,
            requested_at=requested_at,
            answered_at=answered_at,
            related_task_id=related_task_id,
            related_attempt_id=related_attempt_id,
        )

    def operator_action_projection(
        self,
        *,
        protocol,
        run_manifest,
    ):
        protocol, run_manifest = self._validated_operator_run_binding(
            protocol,
            run_manifest,
        )
        ledger = self._operator_action_ledger()
        ledger.bind_run(run_manifest)
        return ledger.replay(
            experiment_run_id=run_manifest["experiment_run_id"]
        )

    def _validated_operator_run_binding(self, protocol, run_manifest):
        protocol = copy.deepcopy(protocol)
        run_manifest = copy.deepcopy(run_manifest)
        validate_experiment_protocol(protocol)
        validate_experiment_run_manifest(run_manifest, protocol)
        if protocol["experiment_id"] != self._metadata["protocol_id"]:
            raise ExperimentControllerIntegrityError(
                "operator ledger protocol conflicts with controller authority"
            )
        protocol_sha256 = canonical_json_sha256(protocol)
        if (
            self._metadata["protocol_sha256"] is None
            or self._metadata["operator_limits"] is None
        ):
            raise ExperimentControllerIntegrityError(
                "controller lacks frozen operator policy authority"
            )
        if (
            protocol_sha256 != self._metadata["protocol_sha256"]
            or protocol["operator_limits"]
            != self._metadata["operator_limits"]
        ):
            raise ExperimentControllerIntegrityError(
                "operator protocol differs from frozen controller authority"
            )
        return protocol, run_manifest

    def _operator_action_ledger(self):
        if self._metadata["operator_ledger_policy_sha256"] is None:
            raise ExperimentControllerIntegrityError(
                "controller lacks frozen operator ledger authority"
            )
        ledger_root = self.root / "operator-actions"
        if ledger_root.is_symlink() or not ledger_root.is_dir():
            raise ExperimentControllerIntegrityError(
                "frozen operator ledger authority is unavailable"
            )
        ledger = load_experiment_operator_action_ledger(
            ledger_root
        )
        binding = _operator_ledger_binding(
            self.root,
            protocol_id=self._metadata["protocol_id"],
            protocol_sha256=self._metadata["protocol_sha256"],
            operator_limits=self._metadata["operator_limits"],
            create=False,
        )
        expected = {
            key: self._metadata[key]
            for key in binding
        }
        if binding != expected:
            raise ExperimentControllerIntegrityError(
                "operator ledger authority changed"
            )
        return ledger

    def prelaunch_admission(
        self,
        invocation,
        *,
        lifecycle_root=None,
        run_id=None,
        now_monotonic=None,
    ):
        """Acquire the protocol lane and deny before any durable provider start."""
        binding = _invocation_binding(
            invocation,
            lifecycle_root=lifecycle_root,
            run_id=run_id,
        )
        invocation_id = binding["invocation_id"]
        lifecycle_root = _contained_directory(
            self.root,
            binding["lifecycle_root"],
            "lifecycle_root",
        )
        invocation_dir = (
            lifecycle_root / "model_invocations" / invocation_id
        ).resolve()
        if not _is_relative_to(invocation_dir, self.root):
            raise ExperimentControllerIntegrityError(
                "invocation authority escapes the protocol controller root"
            )
        started_path = invocation_dir / "started.json"
        terminal_path = invocation_dir / "terminal.json"
        fd = self._acquire_provider_lane()
        try:
            prior = _read_json_fd(fd, "provider lane", allow_empty=True)
            self._reconcile_prior_lane(
                fd,
                prior,
                now_monotonic=now_monotonic,
            )
            with self._state_lock():
                document = self._load_document()
                budget_state, _events = advance_experiment_budget(
                    document["budget_state"],
                    now_monotonic=now_monotonic,
                    monotonic=self._monotonic,
                )
                status = document["controller_status"]
                status = _status_after_budget_observation(
                    status,
                    budget_state,
                )
                self._persist(status, budget_state)
                if status != "active":
                    reason = (
                        "budget exhausted"
                        if budget_state["exhausted"]
                        else f"controller state is {status}"
                    )
                    raise ExperimentProviderAdmissionDenied(reason)

            record = {
                "schema_version": PROVIDER_LANE_SCHEMA_VERSION,
                "lane_status": "admitted",
                "protocol_id": self._metadata["protocol_id"],
                "budget_id": self._metadata["budget_id"],
                "frozen_budget_sha256": self._metadata[
                    "frozen_budget_sha256"
                ],
                "invocation_id": invocation_id,
                "run_id": binding["run_id"],
                "lifecycle_root": lifecycle_root.relative_to(
                    self.root
                ).as_posix(),
                "started_path": started_path.relative_to(
                    self.root
                ).as_posix(),
                "terminal_path": terminal_path.relative_to(
                    self.root
                ).as_posix(),
                "owner_pid": os.getpid(),
            }
            _write_json_fd(fd, record, self.root)
            return ProviderAdmission(self, fd, record)
        except Exception:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise

    def post_terminal_accounting(
        self,
        admission,
        *,
        started_path=None,
        terminal_path=None,
        now_monotonic=None,
    ):
        """Import immutable terminal authority, project it once, then release."""
        fd, record = self._validated_admission(admission)
        recorded_started = _contained_file(
            self.root,
            record["started_path"],
            "started_path",
        )
        recorded_terminal = _contained_file(
            self.root,
            record["terminal_path"],
            "terminal_path",
        )
        if started_path is not None and Path(started_path).resolve() != (
            recorded_started
        ):
            raise ExperimentControllerIntegrityError(
                "accounting start path changed after admission"
            )
        if terminal_path is not None and Path(terminal_path).resolve() != (
            recorded_terminal
        ):
            raise ExperimentControllerIntegrityError(
                "accounting terminal path changed after admission"
            )
        with self._state_lock():
            budget_state, emitted = self._account_terminal_locked(
                record,
                now_monotonic=now_monotonic,
            )
            accounted = {
                **record,
                "lane_status": "accounted",
                "usage_event_id": _read_json_file(
                    recorded_terminal,
                    "terminal usage",
                )["usage_event_id"],
                "projection_sha256": budget_state["projection_sha256"],
            }
            _write_json_fd(fd, accounted, self.root)
        admission.record = accounted
        admission._release()
        return {
            "controller_status": self.controller_status,
            "budget_state": copy.deepcopy(budget_state),
            "emitted_events": copy.deepcopy(emitted),
        }

    def observe_boundary(
        self,
        boundary,
        *,
        scheduler_inflight=0,
        integration_active=False,
        open_invocation_ids=(),
        now_monotonic=None,
    ):
        """Observe one safe boundary without interrupting an integration."""
        if boundary not in _BOUNDARIES:
            raise ExperimentControllerError(
                f"unsupported controller boundary: {boundary}"
            )
        if (
            not isinstance(scheduler_inflight, int)
            or isinstance(scheduler_inflight, bool)
            or scheduler_inflight < 0
        ):
            raise ExperimentControllerError(
                "scheduler_inflight must be a nonnegative integer"
            )
        if not isinstance(integration_active, bool):
            raise ExperimentControllerError(
                "integration_active must be a boolean"
            )
        lane_open = self._provider_lane_is_open()
        with self._state_lock():
            document = self._load_document()
            budget_state, _events = advance_experiment_budget(
                document["budget_state"],
                now_monotonic=now_monotonic,
                monotonic=self._monotonic,
            )
            status = document["controller_status"]
            status = _status_after_budget_observation(
                status,
                budget_state,
            )
            if (
                boundary == "post_integration"
                and status == "budget_draining"
                and not lane_open
                and scheduler_inflight == 0
                and not integration_active
                and not tuple(open_invocation_ids)
            ):
                status = "budget_stopped"
            self._persist(status, budget_state)
            return {
                "boundary": boundary,
                "controller_status": status,
                "allow_provider_launch": status == "active",
                "allow_integration": (
                    status == "active" or integration_active
                ),
                "provider_lane_open": lane_open,
            }

    def interrupt(self):
        """Persist an interruption without changing the frozen projection."""
        with self._state_lock():
            document = self._load_document()
            if document["controller_status"] != "active":
                raise ExperimentControllerError(
                    "only an active controller may be interrupted"
                )
            return self._persist(
                "interrupted",
                document["budget_state"],
            )

    def resume_interrupted(
        self,
        *,
        reference=None,
        max_total_tokens=None,
        max_wall_time_seconds=None,
    ):
        """Resume only exact interrupted authority inside the original budget."""
        if reference is not None:
            validate_experiment_controller_reference(
                reference,
                expected_authority_root=self.root,
            )
        try:
            lane_fd = self._acquire_provider_lane()
        except ExperimentProviderLaneBusy as exc:
            raise ExperimentProviderAdmissionDenied(
                "interrupted provider lifecycle must terminate before resume"
            ) from exc
        try:
            prior = _read_json_fd(
                lane_fd,
                "provider lane",
                allow_empty=True,
            )
            self._reconcile_prior_lane(
                lane_fd,
                prior,
                now_monotonic=None,
            )
        finally:
            try:
                fcntl.flock(lane_fd, fcntl.LOCK_UN)
            finally:
                os.close(lane_fd)
        with self._state_lock():
            document = self._load_document()
            state = document["budget_state"]
            if document["controller_status"] != "interrupted":
                raise ExperimentControllerError(
                    "only interrupted experiment work may resume"
                )
            if state["exhausted"]:
                raise ExperimentControllerError(
                    "budget-stopped experiment work cannot resume"
                )
            if (
                max_total_tokens is not None
                and max_total_tokens != state["max_total_tokens"]
            ):
                raise ExperimentControllerIntegrityError(
                    "scored budget extension or reduction is forbidden"
                )
            if (
                max_wall_time_seconds is not None
                and max_wall_time_seconds
                != state["max_wall_time_seconds"]
            ):
                raise ExperimentControllerIntegrityError(
                    "scored budget extension or reduction is forbidden"
                )
            self._persist("active", state)
            return copy.deepcopy(self._load_document())

    def _validate_authority(self):
        if self._metadata["controller_root"] != str(self.root):
            raise ExperimentControllerIntegrityError(
                "controller authority root identity changed"
            )
        if self._metadata["authority_events_path"] != _AUTHORITY_EVENTS:
            raise ExperimentControllerIntegrityError(
                "controller canonical event path changed"
            )
        for prefix, name in (
            ("state_lock", _CONTROLLER_STATE_LOCK),
            ("state_journal", _CONTROLLER_STATE_JOURNAL),
            ("provider_lane", _PROVIDER_LANE),
            ("budget_events", _BUDGET_EVENTS),
        ):
            if _regular_file_identity(self.root / name) != (
                _metadata_file_identity(self._metadata, prefix)
            ):
                raise ExperimentControllerIntegrityError(
                    f"{prefix} authority identity changed"
                )
        with self._state_lock():
            document = self._load_document()
            state = document["budget_state"]
            if state["authority_root"] != str(self.root):
                raise ExperimentControllerIntegrityError(
                    "budget authority root does not match controller root"
                )
            if (
                state["frozen_budget_sha256"]
                != self._metadata["frozen_budget_sha256"]
            ):
                raise ExperimentControllerIntegrityError(
                    "controller frozen budget identity changed"
                )
            self._sync_budget_events(state)
        if self._metadata["operator_ledger_policy_sha256"] is not None:
            self._operator_action_ledger()

    def _load_document(self):
        checkpoint = _latest_state_checkpoint(
            self.root / _CONTROLLER_STATE_JOURNAL,
            expected_identity=_metadata_file_identity(
                self._metadata,
                "state_journal",
            ),
        )
        document = _read_state_document(self.root / _CONTROLLER_STATE)
        checkpoint_document = checkpoint["state_document"]
        state_sequence = document["checkpoint_sequence"]
        checkpoint_sequence = checkpoint["sequence"]
        if state_sequence > checkpoint_sequence:
            raise ExperimentControllerIntegrityError(
                "controller state is ahead of its append-first journal"
            )
        if (
            state_sequence == checkpoint_sequence
            and document["state_sha256"]
            != checkpoint_document["state_sha256"]
        ):
            raise ExperimentControllerIntegrityError(
                "controller state conflicts with its journal checkpoint"
            )
        if state_sequence < checkpoint_sequence:
            _atomic_write_json(
                self.root / _CONTROLLER_STATE,
                checkpoint_document,
            )
            document = copy.deepcopy(checkpoint_document)
        state = document["budget_state"]
        validate_experiment_budget_state(state)
        if (
            state["frozen_budget_sha256"]
            != self._metadata["frozen_budget_sha256"]
        ):
            raise ExperimentControllerIntegrityError(
                "controller state changed frozen budget authority"
            )
        if state["exhausted"] and document["controller_status"] == "active":
            raise ExperimentControllerIntegrityError(
                "exhausted budget cannot retain active controller state"
            )
        return self._reconcile_usage_projection(document)

    def _reconcile_usage_projection(self, document):
        state = document["budget_state"]
        events_path = self.root / state["authority_events_path"]
        fd = _open_regular_file(
            events_path,
            os.O_RDONLY,
            expected_identity=(
                state["authority_events_device"],
                state["authority_events_inode"],
            ),
            label="canonical invocation event authority",
        )
        try:
            event_bytes = _read_bounded_fd(
                fd,
                "canonical invocation event authority",
                max_bytes=64 * 1024 * 1024,
            )
        finally:
            os.close(fd)
        events = []
        for line_number, line in enumerate(
            event_bytes.splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ExperimentControllerIntegrityError(
                    "canonical invocation event authority is invalid JSON "
                    f"at line {line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise ExperimentControllerIntegrityError(
                    "canonical invocation event authority contains a "
                    "non-object event"
                )
            events.append(event)
        try:
            projection = replay_model_invocation_events(events)
        except ModelInvocationIntegrityError as exc:
            raise ExperimentControllerIntegrityError(
                f"canonical invocation event replay failed: {exc}"
            ) from exc
        canonical_usage_ids = list(projection["usage_records"])
        projected_usage_ids = set(state["usage_event_digests"])
        if not projected_usage_ids.issubset(canonical_usage_ids):
            raise ExperimentControllerIntegrityError(
                "budget projection contains usage absent from canonical "
                "invocation authority"
            )
        missing_usage_ids = [
            usage_event_id
            for usage_event_id in canonical_usage_ids
            if usage_event_id not in projected_usage_ids
        ]
        if not missing_usage_ids:
            return document
        reconciled = state
        for usage_event_id in missing_usage_ids:
            reconciled, _events = advance_experiment_budget(
                reconciled,
                usage_event_id,
                now_monotonic=reconciled["observed_monotonic"],
                monotonic=self._monotonic,
            )
        status = _status_after_budget_observation(
            document["controller_status"],
            reconciled,
        )
        return self._publish_state(status, reconciled)

    def _persist(self, status, budget_state):
        if status not in _CONTROLLER_STATUSES:
            raise ExperimentControllerError(
                f"unsupported controller status: {status}"
            )
        validate_experiment_budget_state(budget_state)
        if (
            budget_state["frozen_budget_sha256"]
            != self._metadata["frozen_budget_sha256"]
        ):
            raise ExperimentControllerIntegrityError(
                "cannot persist changed frozen budget authority"
            )
        document = self._publish_state(status, budget_state)
        self._sync_budget_events(budget_state)
        return document

    def _publish_state(self, status, budget_state):
        latest = _latest_state_checkpoint(
            self.root / _CONTROLLER_STATE_JOURNAL,
            expected_identity=_metadata_file_identity(
                self._metadata,
                "state_journal",
            ),
        )
        document = _state_document(
            status,
            budget_state,
            checkpoint_sequence=latest["sequence"] + 1,
        )
        _append_state_checkpoint(
            self.root / _CONTROLLER_STATE_JOURNAL,
            document,
            expected_identity=_metadata_file_identity(
                self._metadata,
                "state_journal",
            ),
        )
        _atomic_write_json(
            self.root / _CONTROLLER_STATE,
            document,
        )
        return copy.deepcopy(document)

    def _sync_budget_events(self, budget_state):
        path = self.root / _BUDGET_EVENTS
        fd = _open_regular_file(
            path,
            os.O_RDWR,
            expected_identity=_metadata_file_identity(
                self._metadata,
                "budget_events",
            ),
            label="budget event authority",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = _read_bounded_fd(
                fd,
                "budget event authority",
                max_bytes=16 * 1024 * 1024,
            )
            existing = {}
            for line in data.splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ExperimentControllerIntegrityError(
                        "budget event authority is invalid JSON"
                    ) from exc
                event_id = event.get("event_id")
                if event_id in existing and existing[event_id] != event:
                    raise ExperimentControllerIntegrityError(
                        "conflicting durable budget event identity"
                    )
                existing[event_id] = event
            os.lseek(fd, 0, os.SEEK_END)
            changed = False
            for event in budget_state["events"]:
                event_id = event["event_id"]
                prior = existing.get(event_id)
                if prior is not None:
                    if prior != event:
                        raise ExperimentControllerIntegrityError(
                            "durable budget event content changed"
                        )
                    continue
                _write_all(fd, _canonical_json_bytes(event) + b"\n")
                existing[event_id] = event
                changed = True
            if changed:
                os.fsync(fd)
                _fsync_directory(self.root)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _acquire_provider_lane(self):
        path = self.root / _PROVIDER_LANE
        fd = _open_regular_file(
            path,
            os.O_RDWR,
            expected_identity=_metadata_file_identity(
                self._metadata,
                "provider_lane",
            ),
            label="provider lane",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ExperimentProviderLaneBusy(
                "protocol-global provider lane is already admitted"
            ) from exc
        return fd

    def _reconcile_prior_lane(self, fd, record, *, now_monotonic):
        if record is None or record.get("lane_status") != "admitted":
            return
        self._validate_lane_record(record)
        started_path = _contained_file(
            self.root,
            record["started_path"],
            "started_path",
            require_exists=False,
        )
        terminal_path = _contained_file(
            self.root,
            record["terminal_path"],
            "terminal_path",
            require_exists=False,
        )
        if terminal_path.is_file():
            with self._state_lock():
                state, _events = self._account_terminal_locked(
                    record,
                    now_monotonic=now_monotonic,
                )
                terminal = _read_json_file(
                    terminal_path,
                    "terminal usage",
                )
                _write_json_fd(
                    fd,
                    {
                        **record,
                        "lane_status": "accounted",
                        "usage_event_id": terminal["usage_event_id"],
                        "projection_sha256": state["projection_sha256"],
                    },
                    self.root,
                )
            return
        if started_path.is_file():
            raise ExperimentProviderAdmissionDenied(
                "prior admitted provider lifecycle remains open"
            )
        _write_json_fd(
            fd,
            {**record, "lane_status": "abandoned_before_start"},
            self.root,
        )

    def _account_terminal_locked(self, record, *, now_monotonic):
        self._validate_lane_record(record)
        started_path = _contained_file(
            self.root,
            record["started_path"],
            "started_path",
        )
        terminal_path = _contained_file(
            self.root,
            record["terminal_path"],
            "terminal_path",
        )
        import_model_invocation_lifecycle(
            self.root / _AUTHORITY_EVENTS,
            started_path,
            terminal_path=terminal_path,
            actor="experiment-budget-controller",
            run_id=record.get("run_id"),
            source_root=self.root,
            require_terminal=True,
        )
        terminal = _read_json_file(terminal_path, "terminal usage")
        usage_event_id = terminal.get("usage_event_id")
        if not isinstance(usage_event_id, str) or not usage_event_id:
            raise ExperimentControllerIntegrityError(
                "terminal usage lacks canonical usage_event_id"
            )
        document = self._load_document()
        budget_state, emitted = advance_experiment_budget(
            document["budget_state"],
            usage_event_id,
            now_monotonic=now_monotonic,
            monotonic=self._monotonic,
        )
        status = document["controller_status"]
        status = _status_after_budget_observation(
            status,
            budget_state,
        )
        self._persist(status, budget_state)
        return budget_state, emitted

    def _validated_admission(self, admission):
        if (
            not isinstance(admission, ProviderAdmission)
            or admission.controller.root != self.root
            or not admission.held
        ):
            raise ExperimentControllerIntegrityError(
                "terminal accounting requires the held original admission"
            )
        self._validate_lane_record(admission.record)
        current = _read_json_fd(
            admission._fd,
            "provider lane",
            allow_empty=False,
        )
        if current != admission.record:
            raise ExperimentControllerIntegrityError(
                "provider lane record changed after admission"
            )
        return admission._fd, admission.record

    def _validate_lane_record(self, record):
        required = {
            "schema_version",
            "lane_status",
            "protocol_id",
            "budget_id",
            "frozen_budget_sha256",
            "invocation_id",
            "run_id",
            "lifecycle_root",
            "started_path",
            "terminal_path",
            "owner_pid",
        }
        if not isinstance(record, dict) or not required.issubset(record):
            raise ExperimentControllerIntegrityError(
                "provider lane record is incomplete"
            )
        if (
            record["schema_version"] != PROVIDER_LANE_SCHEMA_VERSION
            or record["protocol_id"] != self._metadata["protocol_id"]
            or record["budget_id"] != self._metadata["budget_id"]
            or record["frozen_budget_sha256"]
            != self._metadata["frozen_budget_sha256"]
        ):
            raise ExperimentControllerIntegrityError(
                "provider lane binding changed"
            )

    def _abandon_before_start(self, admission):
        fd, record = self._validated_admission(admission)
        started_path = _contained_file(
            self.root,
            record["started_path"],
            "started_path",
            require_exists=False,
        )
        terminal_path = _contained_file(
            self.root,
            record["terminal_path"],
            "terminal_path",
            require_exists=False,
        )
        if started_path.exists() or terminal_path.exists():
            raise ExperimentControllerIntegrityError(
                "started provider admission requires terminal accounting"
            )
        abandoned = {**record, "lane_status": "abandoned_before_start"}
        _write_json_fd(fd, abandoned, self.root)
        admission.record = abandoned
        admission._release()

    def _provider_lane_is_open(self):
        try:
            fd = self._acquire_provider_lane()
        except ExperimentProviderLaneBusy:
            return True
        try:
            record = _read_json_fd(fd, "provider lane", allow_empty=True)
            if record is None or record.get("lane_status") != "admitted":
                return False
            self._validate_lane_record(record)
            return True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _state_lock(self):
        return _locked_file(
            self.root / _CONTROLLER_STATE_LOCK,
            expected_identity=_metadata_file_identity(
                self._metadata,
                "state_lock",
            ),
        )


def create_experiment_controller(controller_root, **kwargs):
    """Create or idempotently reload a frozen protocol budget controller."""
    return ExperimentController.create(controller_root, **kwargs)


def load_experiment_controller(reference_or_root, *, monotonic=None):
    """Load a controller from its immutable reference or authority root."""
    if isinstance(reference_or_root, dict):
        reference = validate_experiment_controller_reference(
            reference_or_root
        )
        root = reference["controller_root"]
    else:
        root = reference_or_root
    return ExperimentController(root, monotonic=monotonic)


def discover_experiment_controller_reference(authority_root):
    """Return the bound controller reference, or ``None`` when not configured."""
    root = _controller_root(authority_root, create=False)
    if not (root / _CONTROLLER_METADATA).is_file():
        return None
    return ExperimentController(root).reference


def validate_experiment_controller_reference(
    reference,
    *,
    expected_authority_root=None,
):
    """Validate an immutable controller reference against its source metadata."""
    required = {
        "schema_version",
        "protocol_id",
        "budget_id",
        "controller_root",
        "controller_metadata_sha256",
        "frozen_budget_sha256",
    }
    if not isinstance(reference, dict) or set(reference) != required:
        raise ExperimentControllerIntegrityError(
            "experiment controller reference fields are invalid"
        )
    if (
        reference["schema_version"]
        != CONTROLLER_REFERENCE_SCHEMA_VERSION
        or not _SHA256.fullmatch(
            str(reference["controller_metadata_sha256"])
        )
        or not _SHA256.fullmatch(str(reference["frozen_budget_sha256"]))
    ):
        raise ExperimentControllerIntegrityError(
            "experiment controller reference identity is invalid"
        )
    root = _controller_root(reference["controller_root"], create=False)
    if (
        expected_authority_root is not None
        and root != Path(expected_authority_root).resolve()
    ):
        raise ExperimentControllerIntegrityError(
            "experiment controller reference authority changed"
        )
    metadata_path = root / _CONTROLLER_METADATA
    if _file_sha256(metadata_path) != reference[
        "controller_metadata_sha256"
    ]:
        raise ExperimentControllerIntegrityError(
            "experiment controller metadata digest changed"
        )
    metadata = _load_metadata(root)
    if (
        metadata["protocol_id"] != reference["protocol_id"]
        or metadata["budget_id"] != reference["budget_id"]
        or metadata["frozen_budget_sha256"]
        != reference["frozen_budget_sha256"]
    ):
        raise ExperimentControllerIntegrityError(
            "experiment controller reference conflicts with metadata"
        )
    return copy.deepcopy(reference)


def _invocation_binding(invocation, *, lifecycle_root, run_id):
    if isinstance(invocation, dict):
        invocation_id = invocation.get("invocation_id")
        lifecycle_root = (
            lifecycle_root
            or invocation.get("lifecycle_root")
            or invocation.get("lifecycle_authority_root")
        )
        run_id = run_id or invocation.get("run_id")
    else:
        invocation_id = invocation
    return {
        "invocation_id": _safe_id(invocation_id, "invocation_id"),
        "lifecycle_root": lifecycle_root,
        "run_id": _safe_id(run_id or "RUN-UNKNOWN", "run_id"),
    }


def _controller_root(path, *, create):
    if not isinstance(path, (str, os.PathLike)):
        raise ExperimentControllerError(
            "controller_root must be a filesystem path"
        )
    path = Path(path)
    if path.is_symlink():
        raise ExperimentControllerIntegrityError(
            "controller_root cannot be a symlink"
        )
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ExperimentControllerError(
            f"controller_root is not a directory: {path}"
        )
    return path.resolve()


def _contained_directory(root, value, label):
    if not isinstance(value, (str, os.PathLike)):
        raise ExperimentControllerIntegrityError(
            f"{label} must be a filesystem path"
        )
    path = Path(value).resolve()
    if not path.is_dir() or not _is_relative_to(path, root):
        raise ExperimentControllerIntegrityError(
            f"{label} must remain inside controller authority"
        )
    return path


def _contained_file(root, value, label, *, require_exists=True):
    if not isinstance(value, (str, os.PathLike)):
        raise ExperimentControllerIntegrityError(
            f"{label} must be a filesystem path"
        )
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    path = candidate.resolve()
    if not _is_relative_to(path, root):
        raise ExperimentControllerIntegrityError(
            f"{label} escapes controller authority"
        )
    if require_exists and (not path.is_file() or path.is_symlink()):
        raise ExperimentControllerIntegrityError(
            f"{label} is not a regular authority file"
        )
    return path


def _load_metadata(root):
    metadata = _read_json_file(
        root / _CONTROLLER_METADATA,
        "experiment controller metadata",
    )
    required = {
        "schema_version",
        "protocol_id",
        "budget_id",
        "scored",
        "protocol_sha256",
        "operator_limits",
        "operator_limits_sha256",
        "operator_ledger_root_device",
        "operator_ledger_root_inode",
        "operator_ledger_policy_sha256",
        "controller_root",
        "authority_events_path",
        "budget_events_path",
        "frozen_budget_sha256",
        "max_total_tokens",
        "max_wall_time_seconds",
        "soft_warning_ratio",
        "initial_monotonic",
        "state_lock_device",
        "state_lock_inode",
        "state_journal_device",
        "state_journal_inode",
        "provider_lane_device",
        "provider_lane_inode",
        "budget_events_device",
        "budget_events_inode",
    }
    legacy_required = required - {
        "protocol_sha256",
        "operator_limits",
        "operator_limits_sha256",
        "operator_ledger_root_device",
        "operator_ledger_root_inode",
        "operator_ledger_policy_sha256",
    }
    if (
        set(metadata) == legacy_required
        and metadata.get("schema_version")
        == LEGACY_CONTROLLER_SCHEMA_VERSION
    ):
        metadata = {
            **metadata,
            "protocol_sha256": None,
            "operator_limits": None,
            "operator_limits_sha256": None,
            "operator_ledger_root_device": None,
            "operator_ledger_root_inode": None,
            "operator_ledger_policy_sha256": None,
        }
    elif (
        set(metadata) == required
        and metadata.get("schema_version")
        == LEGACY_CONTROLLER_SCHEMA_VERSION
    ):
        raise ExperimentControllerIntegrityError(
            "legacy controller metadata cannot contain v2 operator fields"
        )
    elif set(metadata) != required:
        raise ExperimentControllerIntegrityError(
            "experiment controller metadata fields are invalid"
        )
    if (
        metadata["schema_version"]
        not in {
            CONTROLLER_SCHEMA_VERSION,
            LEGACY_CONTROLLER_SCHEMA_VERSION,
        }
        or not isinstance(metadata["scored"], bool)
        or not _SHA256.fullmatch(str(metadata["frozen_budget_sha256"]))
    ):
        raise ExperimentControllerIntegrityError(
            "experiment controller metadata identity is invalid"
    )
    _safe_id(metadata["protocol_id"], "protocol_id")
    _safe_id(metadata["budget_id"], "budget_id")
    protocol_sha256, operator_limits = _operator_policy_binding(
        metadata["protocol_sha256"],
        metadata["operator_limits"],
    )
    expected_limits_sha256 = (
        _canonical_json_sha256(operator_limits)
        if operator_limits is not None
        else None
    )
    if (
        protocol_sha256 != metadata["protocol_sha256"]
        or metadata["operator_limits_sha256"] != expected_limits_sha256
    ):
        raise ExperimentControllerIntegrityError(
            "controller operator policy binding is invalid"
        )
    ledger_binding_values = (
        metadata["operator_ledger_root_device"],
        metadata["operator_ledger_root_inode"],
        metadata["operator_ledger_policy_sha256"],
    )
    if operator_limits is None:
        if ledger_binding_values != (None, None, None):
            raise ExperimentControllerIntegrityError(
                "budget-only controller cannot bind an operator ledger"
            )
    elif (
        not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in ledger_binding_values[:2]
        )
        or not _SHA256.fullmatch(str(ledger_binding_values[2]))
    ):
        raise ExperimentControllerIntegrityError(
            "controller operator ledger binding is invalid"
        )
    return metadata


def _operator_policy_binding(protocol_sha256, operator_limits):
    if protocol_sha256 is None and operator_limits is None:
        return None, None
    if (
        not isinstance(protocol_sha256, str)
        or not _SHA256.fullmatch(protocol_sha256)
    ):
        raise ExperimentControllerError(
            "protocol_sha256 must bind operator limits"
        )
    if (
        not isinstance(operator_limits, dict)
        or set(operator_limits) != _OPERATOR_ACTION_CLASSES
    ):
        raise ExperimentControllerError(
            "operator_limits must define the closed action vocabulary"
        )
    normalized = {}
    for action_class in sorted(_OPERATOR_ACTION_CLASSES):
        limit = operator_limits[action_class]
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
        ):
            raise ExperimentControllerError(
                f"{action_class} limit must be a non-negative integer"
            )
        normalized[action_class] = limit
    return protocol_sha256, normalized


def _validate_operator_target_authority(
    controller_reference,
    run_manifest,
    run_dir,
    *,
    request_source,
    request,
):
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / "state" / "two_phase_scheduler_state.json"
    try:
        state = _read_json_file(
            state_path,
            "experiment target scheduler state",
        )
    except (OSError, ExperimentControllerIntegrityError) as exc:
        raise PermissionError(
            "experiment target run binding is absent or inconsistent"
        ) from exc
    expected = {
        "experiment_controller_reference": controller_reference,
        "experiment_run_id": run_manifest["experiment_run_id"],
        "experiment_run_manifest_sha256": canonical_json_sha256(
            run_manifest
        ),
        "experiment_target_path_sha256": hashlib.sha256(
            str(run_dir).encode("utf-8")
        ).hexdigest(),
    }
    if not isinstance(state, dict) or any(
        state.get(key) != value for key, value in expected.items()
    ):
        raise PermissionError(
            "experiment target run binding is absent or inconsistent"
        )

    expected_event_type = _OPERATOR_RUNTIME_REQUEST_TYPES.get(
        request_source
    )
    if expected_event_type is None:
        return
    if (
        not isinstance(request, dict)
        or request.get("event_type") != expected_event_type
        or not isinstance(request.get("event_id"), str)
    ):
        raise PermissionError(
            "operator request lacks target-run source authority"
        )
    events_path = run_dir / "events.jsonl"
    try:
        raw_events = _read_regular_file_bytes(
            events_path,
            "experiment target runtime events",
        ).decode("utf-8")
    except (
        OSError,
        UnicodeDecodeError,
        ExperimentControllerIntegrityError,
    ) as exc:
        raise PermissionError(
            "operator request lacks target-run source authority"
        ) from exc
    matches = []
    try:
        for line in raw_events.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if (
                isinstance(event, dict)
                and event.get("event_id") == request["event_id"]
            ):
                matches.append(event)
    except json.JSONDecodeError as exc:
        raise PermissionError(
            "operator request target-run authority is invalid"
        ) from exc
    if len(matches) != 1 or matches[0] != request:
        raise PermissionError(
            "operator request lacks target-run source authority"
        )


def _operator_ledger_binding(
    controller_root,
    *,
    protocol_id,
    protocol_sha256,
    operator_limits,
    create,
):
    ledger_root = controller_root / "operator-actions"
    if create:
        ledger = create_experiment_operator_action_ledger(
            ledger_root,
            protocol_id=protocol_id,
            protocol_sha256=protocol_sha256,
            operator_limits=operator_limits,
        )
    else:
        if ledger_root.is_symlink() or not ledger_root.is_dir():
            raise ExperimentControllerIntegrityError(
                "frozen operator ledger authority is unavailable"
            )
        ledger = load_experiment_operator_action_ledger(ledger_root)
        policy = ledger.policy
        if (
            policy["protocol_id"] != protocol_id
            or policy["protocol_sha256"] != protocol_sha256
            or policy["operator_limits"] != operator_limits
        ):
            raise ExperimentControllerIntegrityError(
                "operator ledger policy conflicts with controller"
            )
    stat_result = ledger.root.stat()
    return {
        "operator_ledger_root_device": stat_result.st_dev,
        "operator_ledger_root_inode": stat_result.st_ino,
        "operator_ledger_policy_sha256": _file_sha256(
            ledger.root / "operator-action-policy.json"
        ),
    }


def _state_document(status, budget_state, *, checkpoint_sequence):
    payload = {
        "schema_version": CONTROLLER_STATE_SCHEMA_VERSION,
        "checkpoint_sequence": checkpoint_sequence,
        "controller_status": status,
        "budget_state": copy.deepcopy(budget_state),
    }
    return {
        **payload,
        "state_sha256": _canonical_json_sha256(payload),
    }


def _read_state_document(path):
    return _validate_state_document(
        _read_json_file(path, "experiment controller state")
    )


def _validate_state_document(document):
    if not isinstance(document, dict):
        raise ExperimentControllerIntegrityError(
            "experiment controller state must be an object"
        )
    if set(document) != {
        "schema_version",
        "checkpoint_sequence",
        "controller_status",
        "budget_state",
        "state_sha256",
    }:
        raise ExperimentControllerIntegrityError(
            "experiment controller state fields are invalid"
        )
    payload = {
        "schema_version": document["schema_version"],
        "checkpoint_sequence": document["checkpoint_sequence"],
        "controller_status": document["controller_status"],
        "budget_state": document["budget_state"],
    }
    if (
        document["schema_version"] != CONTROLLER_STATE_SCHEMA_VERSION
        or not isinstance(document["checkpoint_sequence"], int)
        or isinstance(document["checkpoint_sequence"], bool)
        or document["checkpoint_sequence"] < 0
        or document["controller_status"] not in _CONTROLLER_STATUSES
        or document["state_sha256"] != _canonical_json_sha256(payload)
    ):
        raise ExperimentControllerIntegrityError(
            "experiment controller state integrity changed"
        )
    return document


def _identity_metadata(prefix, identity):
    device, inode = identity
    return {
        f"{prefix}_device": device,
        f"{prefix}_inode": inode,
    }


def _metadata_file_identity(metadata, prefix):
    if not isinstance(metadata, dict):
        return None
    device = metadata.get(f"{prefix}_device")
    inode = metadata.get(f"{prefix}_inode")
    if (
        not isinstance(device, int)
        or isinstance(device, bool)
        or device < 0
        or not isinstance(inode, int)
        or isinstance(inode, bool)
        or inode <= 0
    ):
        raise ExperimentControllerIntegrityError(
            f"{prefix} authority identity is invalid"
        )
    return device, inode


def _state_checkpoint(document, sequence, predecessor_sha256):
    document = _validate_state_document(copy.deepcopy(document))
    if document["checkpoint_sequence"] != sequence:
        raise ExperimentControllerIntegrityError(
            "controller state sequence does not match its checkpoint"
        )
    payload = {
        "schema_version": CONTROLLER_STATE_CHECKPOINT_SCHEMA_VERSION,
        "sequence": sequence,
        "predecessor_checkpoint_sha256": predecessor_sha256,
        "state_document": document,
    }
    return {
        **payload,
        "checkpoint_sha256": _canonical_json_sha256(payload),
    }


def _validated_state_checkpoints(data):
    checkpoints = []
    predecessor = None
    lines = data.splitlines(keepends=True)
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if line_number == len(lines) and not line.endswith(b"\n"):
            # A crash may leave the append-first checkpoint tail short.  Only
            # newline-terminated records are committed journal authority.
            break
        try:
            checkpoint = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExperimentControllerIntegrityError(
                "controller state journal is invalid JSON at line "
                f"{line_number}"
            ) from exc
        required = {
            "schema_version",
            "sequence",
            "predecessor_checkpoint_sha256",
            "state_document",
            "checkpoint_sha256",
        }
        if not isinstance(checkpoint, dict) or set(checkpoint) != required:
            raise ExperimentControllerIntegrityError(
                "controller state checkpoint fields are invalid"
            )
        sequence = checkpoint["sequence"]
        payload = {
            key: checkpoint[key]
            for key in required
            if key != "checkpoint_sha256"
        }
        if (
            checkpoint["schema_version"]
            != CONTROLLER_STATE_CHECKPOINT_SCHEMA_VERSION
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != len(checkpoints)
            or checkpoint["predecessor_checkpoint_sha256"] != predecessor
            or checkpoint["checkpoint_sha256"]
            != _canonical_json_sha256(payload)
        ):
            raise ExperimentControllerIntegrityError(
                "controller state checkpoint chain is invalid"
            )
        _validate_state_document(checkpoint["state_document"])
        checkpoints.append(checkpoint)
        predecessor = checkpoint["checkpoint_sha256"]
    return checkpoints


def _append_state_checkpoint(path, document, *, expected_identity):
    fd = _open_regular_file(
        path,
        os.O_RDWR,
        expected_identity=expected_identity,
        label="controller state journal",
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        journal_data = _read_bounded_fd(
            fd,
            "controller state journal",
            max_bytes=64 * 1024 * 1024,
        )
        checkpoints = _validated_state_checkpoints(journal_data)
        committed_length = (
            journal_data.rfind(b"\n") + 1 if journal_data else 0
        )
        if committed_length != len(journal_data):
            os.ftruncate(fd, committed_length)
            os.fsync(fd)
        if (
            checkpoints
            and checkpoints[-1]["state_document"]["state_sha256"]
            == document["state_sha256"]
        ):
            return checkpoints[-1]
        if document["checkpoint_sequence"] != len(checkpoints):
            raise ExperimentControllerIntegrityError(
                "controller state checkpoint sequence is not append-only"
            )
        checkpoint = _state_checkpoint(
            document,
            len(checkpoints),
            (
                checkpoints[-1]["checkpoint_sha256"]
                if checkpoints
                else None
            ),
        )
        os.lseek(fd, 0, os.SEEK_END)
        _write_all(fd, _canonical_json_bytes(checkpoint) + b"\n")
        os.fsync(fd)
        _fsync_directory(Path(path).parent)
        return checkpoint
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _latest_state_checkpoint(path, *, expected_identity):
    fd = _open_regular_file(
        path,
        os.O_RDWR,
        expected_identity=expected_identity,
        label="controller state journal",
    )
    try:
        journal_data = _read_bounded_fd(
            fd,
            "controller state journal",
            max_bytes=64 * 1024 * 1024,
        )
        checkpoints = _validated_state_checkpoints(journal_data)
        committed_length = (
            journal_data.rfind(b"\n") + 1 if journal_data else 0
        )
        if committed_length != len(journal_data):
            os.ftruncate(fd, committed_length)
            os.fsync(fd)
    finally:
        os.close(fd)
    if not checkpoints:
        raise ExperimentControllerIntegrityError(
            "controller state journal is empty"
        )
    return checkpoints[-1]


def _require_requested_budget(
    state,
    *,
    budget_id,
    max_total_tokens,
    max_wall_time_seconds,
    soft_warning_ratio,
):
    expected = {
        "budget_id": budget_id,
        "max_total_tokens": max_total_tokens,
        "max_wall_time_seconds": max_wall_time_seconds,
        "soft_warning_ratio": soft_warning_ratio,
    }
    if any(state.get(field) != value for field, value in expected.items()):
        raise ExperimentControllerIntegrityError(
            "existing scored budget cannot be reset or extended"
        )


def _status_after_budget_observation(status, budget_state):
    if (
        status != "budget_stopped"
        and (
            budget_state["exhausted"]
            or not budget_state["calibration_eligible"]
        )
    ):
        return "budget_draining"
    return status


def _safe_id(value, label):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ExperimentControllerError(
            f"{label} must be a safe nonempty identifier"
        )
    return value


def _read_json_file(path, label):
    try:
        return json.loads(_read_regular_file_bytes(path, label))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentControllerIntegrityError(
            f"{label} is not valid JSON"
        ) from exc


def _exclusive_or_idempotent_json(path, value):
    payload = _canonical_json_bytes(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        if _read_regular_file_bytes(
            path,
            "immutable controller authority",
        ) != payload:
            raise ExperimentControllerIntegrityError(
                f"immutable controller authority conflicts: {path}"
            )
        return
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(Path(path).parent)


def _atomic_write_json(path, value):
    path = Path(path)
    payload = _canonical_json_bytes(value) + b"\n"
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


@contextmanager
def _locked_file(path, *, expected_identity=None):
    fd = _open_regular_file(
        path,
        os.O_RDWR | os.O_CREAT,
        expected_identity=expected_identity,
        label="controller lock",
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _touch_regular_file(path):
    fd = _open_regular_file(
        path,
        os.O_RDWR | os.O_CREAT,
        expected_identity=None,
        label="authority path",
    )
    os.close(fd)


def _regular_file_identity(path):
    fd = _open_regular_file(
        path,
        os.O_RDONLY,
        expected_identity=None,
        label="controller authority",
    )
    try:
        status = os.fstat(fd)
        return status.st_dev, status.st_ino
    finally:
        os.close(fd)


def _open_regular_file(
    path,
    access_flags,
    *,
    expected_identity,
    label,
):
    flags = access_flags | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV}:
            raise ExperimentControllerIntegrityError(
                f"{label} is not a safe regular file: {path}"
            ) from exc
        raise
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise ExperimentControllerIntegrityError(
                f"{label} is not a regular file: {path}"
            )
        identity = status.st_dev, status.st_ino
        if expected_identity is not None and identity != expected_identity:
            raise ExperimentControllerIntegrityError(
                f"{label} authority identity changed: {path}"
            )
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_bounded_fd(fd, label, *, max_bytes):
    os.lseek(fd, 0, os.SEEK_SET)
    data = bytearray()
    while True:
        chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ExperimentControllerIntegrityError(
                f"{label} exceeds its bounded size"
            )


def _read_regular_file_bytes(path, label, *, max_bytes=16 * 1024 * 1024):
    fd = _open_regular_file(
        path,
        os.O_RDONLY,
        expected_identity=None,
        label=label,
    )
    try:
        return _read_bounded_fd(fd, label, max_bytes=max_bytes)
    finally:
        os.close(fd)


def _read_json_fd(fd, label, *, allow_empty):
    status = os.fstat(fd)
    if not stat.S_ISREG(status.st_mode):
        raise ExperimentControllerIntegrityError(
            f"{label} is not a regular file"
        )
    data = _read_bounded_fd(fd, label, max_bytes=1024 * 1024)
    if not data.strip():
        if allow_empty:
            return None
        raise ExperimentControllerIntegrityError(f"{label} is empty")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ExperimentControllerIntegrityError(
            f"{label} is invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ExperimentControllerIntegrityError(
            f"{label} must contain an object"
        )
    return value


def _write_json_fd(fd, value, parent):
    payload = _canonical_json_bytes(value) + b"\n"
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    _write_all(fd, payload)
    os.fsync(fd)
    _fsync_directory(parent)


def _write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing controller authority")
        view = view[written:]


def _canonical_json_bytes(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExperimentControllerIntegrityError(
            "controller authority is not canonical JSON"
        ) from exc


def _canonical_json_sha256(value):
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _file_sha256(path):
    return hashlib.sha256(
        _read_regular_file_bytes(path, "controller authority file")
    ).hexdigest()


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_relative_to(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

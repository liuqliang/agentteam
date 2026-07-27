"""Pure deterministic accounting for protocol-global experiment budgets.

This module deliberately does not perform provider admission or scheduler
transitions.  It projects immutable Phase 1 terminal usage records and an
injected monotonic clock into a serializable budget state and newly emitted
warning/exhaustion events.
"""

import copy
import hashlib
import json
import math
import os
import stat
import time
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .model_invocation import (
    ModelInvocationIntegrityError,
    replay_model_invocation_events,
)
from .token_usage import usage_event_id_for_invocation


BUDGET_STATE_SCHEMA_VERSION = "experiment_budget_state.v2"
BUDGET_EVENT_SCHEMA_VERSION = "experiment_budget_event.v1"
TERMINAL_USAGE_SCHEMA_VERSION = "model_invocation_usage.v1"

_OPTIONAL_COMPONENT_FIELDS = ("cached_input_tokens", "reasoning_tokens")
_STATE_FIELDS = {
    "budget_schema_version",
    "budget_id",
    "max_total_tokens",
    "max_wall_time_seconds",
    "soft_warning_ratio",
    "initial_monotonic",
    "authority_root",
    "authority_root_device",
    "authority_root_inode",
    "authority_events_path",
    "authority_events_device",
    "authority_events_inode",
    "authority_events_prefix_byte_count",
    "authority_events_prefix_sha256",
    "frozen_budget_sha256",
    "projection_sha256",
    "observed_monotonic",
    "elapsed_wall_time_seconds",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "overshoot_tokens",
    "usage_complete",
    "calibration_eligible",
    "incomplete_usage_reasons",
    "usage_event_digests",
    "usage_authority_digests",
    "invocation_usage_events",
    "warning_emitted",
    "exhaustion_emitted",
    "exhausted",
    "warning_dimensions",
    "exhaustion_dimensions",
    "events",
}
_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "event_kind",
    "budget_id",
    "frozen_budget_sha256",
    "max_total_tokens",
    "max_wall_time_seconds",
    "soft_warning_ratio",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "overshoot_tokens",
    "elapsed_wall_time_seconds",
    "threshold_dimensions",
    "usage_complete",
    "incomplete_usage_reasons",
}
_NO_TERMINAL_USAGE = object()
_FROZEN_BUDGET_NAMESPACE = b"agentteam:experiment_budget_state.v2\x00"
_PROJECTION_NAMESPACE = b"agentteam:experiment_budget_projection.v2\x00"
_BUDGET_EVENT_NAMESPACE = b"agentteam:experiment_budget_event.v1\x00"


class ExperimentBudgetError(ValueError):
    """Raised when budget authority or terminal usage is invalid."""


class ExperimentBudgetIntegrityError(ExperimentBudgetError):
    """Raised when replay conflicts with already projected authority."""


class ExperimentBudgetController:
    """Stateless convenience wrapper around an injectable monotonic clock."""

    def __init__(self, monotonic=None):
        self._monotonic = _validated_clock(monotonic)

    def create_state(
        self,
        budget_id,
        max_total_tokens,
        max_wall_time_seconds,
        soft_warning_ratio,
        *,
        authority_root,
        authority_events_path,
    ):
        return create_experiment_budget_state(
            budget_id,
            max_total_tokens,
            max_wall_time_seconds,
            soft_warning_ratio,
            authority_root=authority_root,
            authority_events_path=authority_events_path,
            monotonic=self._monotonic,
        )

    def advance(
        self,
        state,
        terminal_usage=_NO_TERMINAL_USAGE,
    ):
        return advance_experiment_budget(
            state,
            terminal_usage,
            monotonic=self._monotonic,
        )


def create_experiment_budget_state(
    budget_id,
    max_total_tokens,
    max_wall_time_seconds,
    soft_warning_ratio,
    *,
    authority_root,
    authority_events_path,
    initial_monotonic=None,
    monotonic=None,
):
    """Create the frozen, serializable global budget projection."""
    budget_id = _validated_identifier(budget_id, "budget_id")
    max_total_tokens = _positive_integer(
        max_total_tokens,
        "max_total_tokens",
    )
    max_wall_time_seconds = _positive_number(
        max_wall_time_seconds,
        "max_wall_time_seconds",
    )
    soft_warning_ratio = _warning_ratio(soft_warning_ratio)
    initial_monotonic = _monotonic_value(
        initial_monotonic,
        monotonic=monotonic,
        field="initial_monotonic",
    )
    authority = _initialize_authority(
        authority_root,
        authority_events_path,
    )
    frozen = {
        "budget_id": budget_id,
        "max_total_tokens": max_total_tokens,
        "max_wall_time_seconds": max_wall_time_seconds,
        "soft_warning_ratio": soft_warning_ratio,
        "initial_monotonic": initial_monotonic,
        "authority_root": str(authority["root"]),
        "authority_root_device": authority["root_device"],
        "authority_root_inode": authority["root_inode"],
        "authority_events_path": authority["events_path"],
        "authority_events_device": authority["events_device"],
        "authority_events_inode": authority["events_inode"],
    }
    state = {
        "budget_schema_version": BUDGET_STATE_SCHEMA_VERSION,
        **frozen,
        "frozen_budget_sha256": _frozen_budget_sha256(frozen),
        "projection_sha256": None,
        "authority_events_prefix_byte_count": len(authority["events_bytes"]),
        "authority_events_prefix_sha256": hashlib.sha256(
            authority["events_bytes"]
        ).hexdigest(),
        "observed_monotonic": initial_monotonic,
        "elapsed_wall_time_seconds": 0.0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "overshoot_tokens": 0,
        "usage_complete": True,
        "calibration_eligible": True,
        "incomplete_usage_reasons": [],
        "usage_event_digests": {},
        "usage_authority_digests": {},
        "invocation_usage_events": {},
        "warning_emitted": False,
        "exhaustion_emitted": False,
        "exhausted": False,
        "warning_dimensions": [],
        "exhaustion_dimensions": [],
        "events": [],
    }
    state["projection_sha256"] = _projection_sha256(state)
    validate_experiment_budget_state(state)
    return state


def advance_experiment_budget(
    state,
    terminal_usage=_NO_TERMINAL_USAGE,
    *,
    now_monotonic=None,
    monotonic=None,
):
    """Apply zero or one terminal record and evaluate both budget dimensions.

    Omitting ``terminal_usage`` performs a clock-only observation.  Passing
    ``None`` explicitly means a terminal usage record was expected but is
    missing, which permanently makes calibration ineligible.
    """
    validate_experiment_budget_state(state)
    projected = copy.deepcopy(state)
    now = _monotonic_value(
        now_monotonic,
        monotonic=monotonic,
        field="now_monotonic",
    )
    if now < projected["observed_monotonic"]:
        raise ExperimentBudgetIntegrityError(
            "monotonic clock regressed below the prior observation"
        )
    if now < projected["initial_monotonic"]:
        raise ExperimentBudgetIntegrityError(
            "monotonic clock regressed below the frozen origin"
        )
    projected["observed_monotonic"] = now
    projected["elapsed_wall_time_seconds"] = _elapsed_seconds(
        projected["initial_monotonic"],
        now,
    )

    if terminal_usage is None:
        _record_incomplete_usage(
            projected,
            usage_event_id=None,
            usage_status="missing",
            reason="missing_terminal_usage",
        )
    elif terminal_usage is not _NO_TERMINAL_USAGE:
        _project_terminal_usage(
            projected,
            terminal_usage,
        )

    projected["usage_complete"] = not projected["incomplete_usage_reasons"]
    projected["calibration_eligible"] = projected["usage_complete"]
    projected["overshoot_tokens"] = max(
        projected["total_tokens"] - projected["max_total_tokens"],
        0,
    )

    emitted = []
    warning_dimensions = _threshold_dimensions(
        projected,
        ratio=projected["soft_warning_ratio"],
    )
    if warning_dimensions and not projected["warning_emitted"]:
        projected["warning_emitted"] = True
        projected["warning_dimensions"] = warning_dimensions
        emitted.append(_budget_event(projected, "warning", warning_dimensions))

    exhaustion_dimensions = _threshold_dimensions(projected, ratio=1.0)
    if exhaustion_dimensions and not projected["exhaustion_emitted"]:
        projected["exhaustion_emitted"] = True
        projected["exhausted"] = True
        projected["exhaustion_dimensions"] = exhaustion_dimensions
        emitted.append(
            _budget_event(projected, "exhaustion", exhaustion_dimensions)
        )

    projected["events"].extend(copy.deepcopy(emitted))
    projected["projection_sha256"] = _projection_sha256(projected)
    validate_experiment_budget_state(projected)
    return projected, emitted


def validate_experiment_budget_state(state):
    """Fail closed if a reconstructed state changed frozen or projected data."""
    if not isinstance(state, dict):
        raise ExperimentBudgetIntegrityError("budget state must be an object")
    if set(state) != _STATE_FIELDS:
        raise ExperimentBudgetIntegrityError(
            "budget state fields do not match its schema version"
        )
    if state.get("budget_schema_version") != BUDGET_STATE_SCHEMA_VERSION:
        raise ExperimentBudgetIntegrityError(
            "unsupported experiment budget state schema"
        )

    frozen = {
        "budget_id": _validated_identifier(
            state.get("budget_id"),
            "budget_id",
        ),
        "max_total_tokens": _positive_integer(
            state.get("max_total_tokens"),
            "max_total_tokens",
        ),
        "max_wall_time_seconds": _positive_number(
            state.get("max_wall_time_seconds"),
            "max_wall_time_seconds",
        ),
        "soft_warning_ratio": _warning_ratio(
            state.get("soft_warning_ratio"),
        ),
        "initial_monotonic": _finite_number(
            state.get("initial_monotonic"),
            "initial_monotonic",
        ),
        "authority_root": _validated_identifier(
            state.get("authority_root"),
            "authority_root",
        ),
        "authority_root_device": _nonnegative_integer(
            state.get("authority_root_device"),
            "authority_root_device",
        ),
        "authority_root_inode": _nonnegative_integer(
            state.get("authority_root_inode"),
            "authority_root_inode",
        ),
        "authority_events_path": _validated_identifier(
            state.get("authority_events_path"),
            "authority_events_path",
        ),
        "authority_events_device": _nonnegative_integer(
            state.get("authority_events_device"),
            "authority_events_device",
        ),
        "authority_events_inode": _nonnegative_integer(
            state.get("authority_events_inode"),
            "authority_events_inode",
        ),
    }
    if state.get("frozen_budget_sha256") != _frozen_budget_sha256(frozen):
        raise ExperimentBudgetIntegrityError(
            "frozen experiment budget configuration changed"
        )
    projection_sha256 = state.get("projection_sha256")
    if (
        not isinstance(projection_sha256, str)
        or len(projection_sha256) != 64
        or any(character not in "0123456789abcdef" for character in projection_sha256)
        or projection_sha256 != _projection_sha256(state)
    ):
        raise ExperimentBudgetIntegrityError(
            "experiment budget projection integrity changed"
        )
    _nonnegative_integer(
        state.get("authority_events_prefix_byte_count"),
        "authority_events_prefix_byte_count",
    )
    if not _sha256_text(state.get("authority_events_prefix_sha256")):
        raise ExperimentBudgetIntegrityError(
            "authority_events_prefix_sha256 must be a sha256 digest"
        )

    observed = _finite_number(
        state.get("observed_monotonic"),
        "observed_monotonic",
    )
    if observed < frozen["initial_monotonic"]:
        raise ExperimentBudgetIntegrityError(
            "observed monotonic value precedes frozen origin"
        )
    elapsed = _nonnegative_number(
        state.get("elapsed_wall_time_seconds"),
        "elapsed_wall_time_seconds",
    )
    if elapsed != _elapsed_seconds(frozen["initial_monotonic"], observed):
        raise ExperimentBudgetIntegrityError(
            "elapsed wall time does not match frozen monotonic origin"
        )

    for field in ("input_tokens", "output_tokens", "total_tokens"):
        _nonnegative_integer(state.get(field), field)
    for field in _OPTIONAL_COMPONENT_FIELDS:
        value = state.get(field)
        if value is not None:
            _nonnegative_integer(value, field)
    if state["total_tokens"] != (
        state["input_tokens"] + state["output_tokens"]
    ):
        raise ExperimentBudgetIntegrityError(
            "total_tokens must equal input_tokens plus output_tokens"
        )
    if (
        state["cached_input_tokens"] is not None
        and state["cached_input_tokens"] > state["input_tokens"]
    ):
        raise ExperimentBudgetIntegrityError(
            "cached_input_tokens cannot exceed input_tokens"
        )
    if (
        state["reasoning_tokens"] is not None
        and state["reasoning_tokens"] > state["output_tokens"]
    ):
        raise ExperimentBudgetIntegrityError(
            "reasoning_tokens cannot exceed output_tokens"
        )
    overshoot = _nonnegative_integer(
        state.get("overshoot_tokens"),
        "overshoot_tokens",
    )
    if overshoot != max(
        state["total_tokens"] - frozen["max_total_tokens"],
        0,
    ):
        raise ExperimentBudgetIntegrityError(
            "overshoot_tokens does not match the frozen token limit"
        )

    reasons = state.get("incomplete_usage_reasons")
    if not isinstance(reasons, list):
        raise ExperimentBudgetIntegrityError(
            "incomplete_usage_reasons must be a list"
        )
    if reasons != sorted(reasons, key=_canonical_json_text):
        raise ExperimentBudgetIntegrityError(
            "incomplete usage reasons must be canonical and sorted"
        )
    for reason in reasons:
        _validate_incomplete_reason(reason)
    usage_complete = _strict_bool(
        state.get("usage_complete"),
        "usage_complete",
    )
    calibration_eligible = _strict_bool(
        state.get("calibration_eligible"),
        "calibration_eligible",
    )
    if usage_complete != (not reasons):
        raise ExperimentBudgetIntegrityError(
            "usage_complete contradicts incomplete usage reasons"
        )
    if calibration_eligible != usage_complete:
        raise ExperimentBudgetIntegrityError(
            "incomplete usage cannot be calibration eligible"
        )

    usage_event_digests = state.get("usage_event_digests")
    _validate_string_mapping(
        usage_event_digests,
        "usage_event_digests",
        value_pattern="sha256",
    )
    usage_authority_digests = state.get("usage_authority_digests")
    _validate_string_mapping(
        usage_authority_digests,
        "usage_authority_digests",
        value_pattern="sha256",
    )
    if not set(usage_authority_digests).issubset(usage_event_digests):
        raise ExperimentBudgetIntegrityError(
            "usage authority digests have no projected terminal identity"
        )
    invocation_usage_events = state.get("invocation_usage_events")
    _validate_string_mapping(
        invocation_usage_events,
        "invocation_usage_events",
    )
    for event_id in usage_event_digests:
        if not event_id.startswith("USAGE-"):
            raise ExperimentBudgetIntegrityError(
                "projected usage identity is not a terminal usage event"
            )
    for invocation_id, event_id in invocation_usage_events.items():
        if (
            not invocation_id.startswith("INV-")
            or event_id not in usage_event_digests
        ):
            raise ExperimentBudgetIntegrityError(
                "invocation usage projection is inconsistent"
            )
    if any(
        reason["usage_event_id"] is not None
        and reason["usage_event_id"] not in usage_event_digests
        for reason in reasons
    ):
        raise ExperimentBudgetIntegrityError(
            "incomplete usage reason has no projected terminal identity"
        )

    warning_emitted = _strict_bool(
        state.get("warning_emitted"),
        "warning_emitted",
    )
    exhaustion_emitted = _strict_bool(
        state.get("exhaustion_emitted"),
        "exhaustion_emitted",
    )
    exhausted = _strict_bool(state.get("exhausted"), "exhausted")
    warning_dimensions = _dimensions(state.get("warning_dimensions"))
    exhaustion_dimensions = _dimensions(state.get("exhaustion_dimensions"))
    if warning_emitted != bool(warning_dimensions):
        raise ExperimentBudgetIntegrityError(
            "warning event latch and dimensions disagree"
        )
    if exhaustion_emitted != bool(exhaustion_dimensions):
        raise ExperimentBudgetIntegrityError(
            "exhaustion event latch and dimensions disagree"
        )
    if exhausted != exhaustion_emitted:
        raise ExperimentBudgetIntegrityError(
            "exhausted state cannot clear after exhaustion"
        )
    if bool(_threshold_dimensions(state, ratio=state["soft_warning_ratio"])) != (
        warning_emitted
    ):
        raise ExperimentBudgetIntegrityError(
            "warning latch does not match projected consumption"
        )
    if bool(_threshold_dimensions(state, ratio=1.0)) != exhaustion_emitted:
        raise ExperimentBudgetIntegrityError(
            "exhaustion latch does not match projected consumption"
        )

    events = state.get("events")
    if not isinstance(events, list):
        raise ExperimentBudgetIntegrityError("events must be a list")
    kinds = [
        event.get("event_kind")
        for event in events
        if isinstance(event, dict)
    ]
    expected_kinds = []
    if warning_emitted:
        expected_kinds.append("warning")
    if exhaustion_emitted:
        expected_kinds.append("exhaustion")
    if kinds != expected_kinds:
        raise ExperimentBudgetIntegrityError(
            "budget events must contain each latched event exactly once"
        )
    for event in events:
        _validate_projected_event(event, state)
    return state


def _project_terminal_usage(state, usage_event_id):
    if not _canonical_usage_event_id(usage_event_id):
        _record_incomplete_usage(
            state,
            usage_event_id=None,
            usage_status="invalid",
            reason="missing_terminal_usage_authority",
        )
        return
    usage, authority_digest, authority_checkpoint = (
        _load_authoritative_terminal_usage(
        state,
        usage_event_id,
        )
    )
    state.update(authority_checkpoint)
    try:
        digest = hashlib.sha256(_canonical_json_bytes(usage)).hexdigest()
    except (TypeError, ValueError):
        raise ExperimentBudgetIntegrityError(
            "terminal usage must be finite canonical JSON"
        ) from None

    usage_event_id = usage.get("usage_event_id")
    invocation_id = usage.get("invocation_id")
    valid_invocation_id = _canonical_invocation_id(invocation_id)
    valid_event_id = (
        valid_invocation_id
        and usage_event_id == usage_event_id_for_invocation(invocation_id)
    )
    rejection = _terminal_usage_rejection(
        state,
        usage,
        valid_event_id=valid_event_id,
        valid_invocation_id=valid_invocation_id,
        authority_digest=authority_digest,
    )
    if valid_event_id:
        prior_digest = state["usage_event_digests"].get(usage_event_id)
        if prior_digest is not None:
            if prior_digest != digest:
                raise ExperimentBudgetIntegrityError(
                    f"conflicting terminal usage identity: {usage_event_id}"
                )
            if rejection is not None:
                _record_incomplete_usage(
                    state,
                    usage_event_id=usage_event_id,
                    usage_status=(
                        usage.get("usage_status")
                        if usage.get("usage_status")
                        in {"partial", "unavailable", "not_applicable"}
                        else "invalid"
                    ),
                    reason=rejection,
                )
            return
        state["usage_event_digests"][usage_event_id] = digest
        state["usage_event_digests"] = dict(
            sorted(state["usage_event_digests"].items())
        )

    if valid_event_id and valid_invocation_id:
        prior_event_id = state["invocation_usage_events"].get(invocation_id)
        if prior_event_id is not None and prior_event_id != usage_event_id:
            raise ExperimentBudgetIntegrityError(
                f"multiple terminal usage records for {invocation_id}"
            )
        state["invocation_usage_events"][invocation_id] = usage_event_id
        state["invocation_usage_events"] = dict(
            sorted(state["invocation_usage_events"].items())
        )

    if rejection is not None:
        _record_incomplete_usage(
            state,
            usage_event_id=usage_event_id if valid_event_id else None,
            usage_status=(
                usage.get("usage_status")
                if usage.get("usage_status")
                in {"partial", "unavailable", "not_applicable"}
                else "invalid"
            ),
            reason=rejection,
        )
        return

    state["input_tokens"] += usage["input_tokens"]
    state["output_tokens"] += usage["output_tokens"]
    state["total_tokens"] += usage["total_tokens"]
    for field in _OPTIONAL_COMPONENT_FIELDS:
        current = state[field]
        value = usage.get(field)
        state[field] = (
            current + value
            if current is not None and value is not None
            else None
        )


def _terminal_usage_rejection(
    state,
    usage,
    *,
    valid_event_id,
    valid_invocation_id,
    authority_digest,
):
    if usage.get("usage_schema_version") != TERMINAL_USAGE_SCHEMA_VERSION:
        return "unsupported_terminal_usage_schema"
    schema_error = _terminal_usage_schema_error(usage)
    if schema_error is not None:
        return f"invalid_terminal_usage_schema:{schema_error}"
    if not valid_event_id:
        return "noncanonical_usage_event_id"
    if not valid_invocation_id:
        return "missing_stable_invocation_id"
    if not _sha256_text(authority_digest):
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority digest is invalid"
        )
    prior_authority_digest = state["usage_authority_digests"].get(
        usage["usage_event_id"]
    )
    if (
        prior_authority_digest is not None
        and prior_authority_digest != authority_digest
    ):
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority digest changed"
        )
    state["usage_authority_digests"][usage["usage_event_id"]] = authority_digest
    state["usage_authority_digests"] = dict(
        sorted(state["usage_authority_digests"].items())
    )
    status = usage.get("usage_status")
    if status != "reported":
        if status in {"partial", "unavailable", "not_applicable"}:
            reason = usage.get("unavailable_reason")
            if not isinstance(reason, str) or not reason.strip():
                reason = f"{status}_terminal_usage"
            return reason
        return "invalid_terminal_usage_status"

    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(field)
        if not _is_nonnegative_integer(value):
            return f"invalid_reported_{field}"
    for field in _OPTIONAL_COMPONENT_FIELDS:
        value = usage.get(field)
        if value is not None and not _is_nonnegative_integer(value):
            return f"invalid_reported_{field}"
    if usage["total_tokens"] != (
        usage["input_tokens"] + usage["output_tokens"]
    ):
        return "inconsistent_reported_total_tokens"
    cached = usage.get("cached_input_tokens")
    if cached is not None and cached > usage["input_tokens"]:
        return "cached_input_tokens_exceed_input_tokens"
    reasoning = usage.get("reasoning_tokens")
    if reasoning is not None and reasoning > usage["output_tokens"]:
        return "reasoning_tokens_exceed_output_tokens"
    return None


def _load_authoritative_terminal_usage(state, usage_event_id):
    """Load one usage record through its persistent canonical lifecycle chain."""
    root, events_file = _validated_authority_paths(
        state["authority_root"],
        state["authority_events_path"],
    )
    _validate_authority_identity(
        root,
        state["authority_root_device"],
        state["authority_root_inode"],
        "terminal usage authority root",
    )
    _validate_authority_identity(
        events_file,
        state["authority_events_device"],
        state["authority_events_inode"],
        "canonical event log",
    )
    authority_events, authority_bytes = _read_authority_events(
        events_file,
        expected_device=state["authority_events_device"],
        expected_inode=state["authority_events_inode"],
        required_prefix_byte_count=state[
            "authority_events_prefix_byte_count"
        ],
        required_prefix_sha256=state["authority_events_prefix_sha256"],
    )
    try:
        projection = replay_model_invocation_events(authority_events)
    except ModelInvocationIntegrityError as exc:
        raise ExperimentBudgetIntegrityError(
            f"terminal usage authority replay failed: {exc}"
        ) from exc

    usage = projection["usage_records"].get(usage_event_id)
    if usage is None:
        raise ExperimentBudgetIntegrityError(
            "terminal usage is absent from canonical authority"
        )
    invocation_id = usage["invocation_id"]
    if invocation_id not in projection["start_records"]:
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority has no canonical start"
        )

    start_payload = _unique_authority_payload(
        authority_events,
        "model_invocation_started",
        "invocation_id",
        invocation_id,
    )
    terminal_payload = _unique_authority_payload(
        authority_events,
        "model_invocation_usage_recorded",
        "usage_event_id",
        usage_event_id,
    )
    start_record, start_digest = _read_authority_source_record(
        root,
        start_payload,
        "invocation start",
        expected_root_device=state["authority_root_device"],
        expected_root_inode=state["authority_root_inode"],
    )
    terminal_record, terminal_digest = _read_authority_source_record(
        root,
        terminal_payload,
        "terminal usage",
        expected_root_device=state["authority_root_device"],
        expected_root_inode=state["authority_root_inode"],
    )
    if start_record.get("invocation_id") != invocation_id:
        raise ExperimentBudgetIntegrityError(
            "canonical invocation start identity changed"
        )
    if terminal_record != usage:
        raise ExperimentBudgetIntegrityError(
            "terminal source record conflicts with canonical event log"
        )
    if usage.get("start_sha256") != start_digest:
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority start digest mismatch"
        )
    return usage, terminal_digest, {
        "authority_events_prefix_byte_count": len(authority_bytes),
        "authority_events_prefix_sha256": hashlib.sha256(
            authority_bytes
        ).hexdigest(),
    }


def _record_incomplete_usage(
    state,
    *,
    usage_event_id,
    usage_status,
    reason,
):
    item = {
        "usage_event_id": usage_event_id,
        "usage_status": usage_status,
        "reason": reason,
    }
    if item not in state["incomplete_usage_reasons"]:
        state["incomplete_usage_reasons"].append(item)
        state["incomplete_usage_reasons"].sort(key=_canonical_json_text)


def _budget_event(state, kind, dimensions):
    return {
        "schema_version": BUDGET_EVENT_SCHEMA_VERSION,
        "event_id": _budget_event_id(state["budget_id"], kind),
        "event_kind": kind,
        "budget_id": state["budget_id"],
        "frozen_budget_sha256": state["frozen_budget_sha256"],
        "max_total_tokens": state["max_total_tokens"],
        "max_wall_time_seconds": state["max_wall_time_seconds"],
        "soft_warning_ratio": state["soft_warning_ratio"],
        "input_tokens": state["input_tokens"],
        "cached_input_tokens": state["cached_input_tokens"],
        "output_tokens": state["output_tokens"],
        "reasoning_tokens": state["reasoning_tokens"],
        "total_tokens": state["total_tokens"],
        "overshoot_tokens": state["overshoot_tokens"],
        "elapsed_wall_time_seconds": state["elapsed_wall_time_seconds"],
        "threshold_dimensions": list(dimensions),
        "usage_complete": state["usage_complete"],
        "incomplete_usage_reasons": copy.deepcopy(
            state["incomplete_usage_reasons"]
        ),
    }


def _validate_projected_event(event, state):
    if not isinstance(event, dict):
        raise ExperimentBudgetIntegrityError("budget event must be an object")
    if set(event) != _EVENT_FIELDS:
        raise ExperimentBudgetIntegrityError(
            "budget event fields do not match its schema version"
        )
    kind = event.get("event_kind")
    if kind not in {"warning", "exhaustion"}:
        raise ExperimentBudgetIntegrityError("invalid budget event kind")
    if event.get("schema_version") != BUDGET_EVENT_SCHEMA_VERSION:
        raise ExperimentBudgetIntegrityError("invalid budget event schema")
    if event.get("event_id") != _budget_event_id(state["budget_id"], kind):
        raise ExperimentBudgetIntegrityError("budget event identity changed")
    for field in (
        "budget_id",
        "frozen_budget_sha256",
        "max_total_tokens",
        "max_wall_time_seconds",
        "soft_warning_ratio",
    ):
        if event.get(field) != state[field]:
            raise ExperimentBudgetIntegrityError(
                f"budget event changed frozen field: {field}"
            )
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        _nonnegative_integer(event.get(field), f"event {field}")
    for field in _OPTIONAL_COMPONENT_FIELDS:
        value = event.get(field)
        if value is not None:
            _nonnegative_integer(value, f"event {field}")
    if event["total_tokens"] != (
        event["input_tokens"] + event["output_tokens"]
    ):
        raise ExperimentBudgetIntegrityError(
            "budget event total token accounting changed"
        )
    if (
        event["cached_input_tokens"] is not None
        and event["cached_input_tokens"] > event["input_tokens"]
    ):
        raise ExperimentBudgetIntegrityError(
            "budget event cached input accounting changed"
        )
    if (
        event["reasoning_tokens"] is not None
        and event["reasoning_tokens"] > event["output_tokens"]
    ):
        raise ExperimentBudgetIntegrityError(
            "budget event reasoning accounting changed"
        )
    overshoot = _nonnegative_integer(
        event.get("overshoot_tokens"),
        "event overshoot_tokens",
    )
    if overshoot != max(
        event["total_tokens"] - event["max_total_tokens"],
        0,
    ):
        raise ExperimentBudgetIntegrityError(
            "budget event overshoot accounting changed"
        )
    elapsed = _nonnegative_number(
        event.get("elapsed_wall_time_seconds"),
        "event elapsed_wall_time_seconds",
    )
    if (
        event["input_tokens"] > state["input_tokens"]
        or event["output_tokens"] > state["output_tokens"]
        or event["total_tokens"] > state["total_tokens"]
        or elapsed > state["elapsed_wall_time_seconds"]
    ):
        raise ExperimentBudgetIntegrityError(
            "budget event exceeds current projected consumption"
        )
    dimensions = _dimensions(event.get("threshold_dimensions"))
    ratio = event["soft_warning_ratio"] if kind == "warning" else 1.0
    if dimensions != _threshold_dimensions(event, ratio=ratio):
        raise ExperimentBudgetIntegrityError(
            "budget event threshold dimensions changed"
        )
    complete = _strict_bool(
        event.get("usage_complete"),
        "event usage_complete",
    )
    reasons = event.get("incomplete_usage_reasons")
    if not isinstance(reasons, list):
        raise ExperimentBudgetIntegrityError(
            "budget event incomplete usage reasons must be a list"
        )
    if reasons != sorted(reasons, key=_canonical_json_text):
        raise ExperimentBudgetIntegrityError(
            "budget event incomplete usage reasons must be sorted"
        )
    for reason in reasons:
        _validate_incomplete_reason(reason)
    if complete != (not reasons):
        raise ExperimentBudgetIntegrityError(
            "budget event usage completeness changed"
        )


def _threshold_dimensions(state, *, ratio):
    dimensions = []
    if _at_or_above_ratio(
        state["total_tokens"],
        state["max_total_tokens"],
        ratio,
    ):
        dimensions.append("tokens")
    if _at_or_above_ratio(
        state["elapsed_wall_time_seconds"],
        state["max_wall_time_seconds"],
        ratio,
    ):
        dimensions.append("wall_time")
    return dimensions


def _at_or_above_ratio(value, limit, ratio):
    return Decimal(str(value)) >= Decimal(str(limit)) * Decimal(str(ratio))


def _elapsed_seconds(origin, now):
    return float(Decimal(str(now)) - Decimal(str(origin)))


def _frozen_budget_sha256(frozen):
    return hashlib.sha256(
        _FROZEN_BUDGET_NAMESPACE + _canonical_json_bytes(frozen)
    ).hexdigest()


def _projection_sha256(state):
    projection = {
        key: value
        for key, value in state.items()
        if key != "projection_sha256"
    }
    try:
        payload = _canonical_json_bytes(projection)
    except (TypeError, ValueError):
        raise ExperimentBudgetIntegrityError(
            "experiment budget projection is not canonical JSON"
        ) from None
    return hashlib.sha256(_PROJECTION_NAMESPACE + payload).hexdigest()


def _budget_event_id(budget_id, kind):
    digest = hashlib.sha256(
        _BUDGET_EVENT_NAMESPACE
        + budget_id.encode("utf-8")
        + b"\x00"
        + kind.encode("ascii")
    ).hexdigest()
    return f"BUDGET-{kind}-{digest}"


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _canonical_json_text(value):
    return _canonical_json_bytes(value).decode("utf-8")


def _monotonic_value(value, *, monotonic, field):
    if value is None:
        value = _validated_clock(monotonic)()
    return _finite_number(value, field)


def _validated_clock(monotonic):
    clock = time.monotonic if monotonic is None else monotonic
    if not callable(clock):
        raise ExperimentBudgetError("monotonic must be callable")
    return clock


def _validated_identifier(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ExperimentBudgetError(f"{field} must be a nonempty string")
    return value


def _warning_ratio(value):
    value = _finite_number(value, "soft_warning_ratio")
    if not 0 < value < 1:
        raise ExperimentBudgetError(
            "soft_warning_ratio must be greater than zero and less than one"
        )
    return value


def _positive_integer(value, field):
    if not _is_nonnegative_integer(value) or value < 1:
        raise ExperimentBudgetError(f"{field} must be a positive integer")
    return value


def _nonnegative_integer(value, field):
    if not _is_nonnegative_integer(value):
        raise ExperimentBudgetIntegrityError(
            f"{field} must be a nonnegative integer"
        )
    return value


def _is_nonnegative_integer(value):
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _positive_number(value, field):
    value = _finite_number(value, field)
    if value <= 0:
        raise ExperimentBudgetError(f"{field} must be greater than zero")
    return value


def _nonnegative_number(value, field):
    value = _finite_number(value, field)
    if value < 0:
        raise ExperimentBudgetIntegrityError(
            f"{field} must be nonnegative"
        )
    return value


def _finite_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentBudgetError(f"{field} must be a finite number")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise ExperimentBudgetError(
            f"{field} must be a finite number"
        ) from None
    if not math.isfinite(value):
        raise ExperimentBudgetError(f"{field} must be a finite number")
    return value


def _strict_bool(value, field):
    if not isinstance(value, bool):
        raise ExperimentBudgetIntegrityError(f"{field} must be boolean")
    return value


def _dimensions(value):
    if not isinstance(value, list):
        raise ExperimentBudgetIntegrityError(
            "threshold dimensions must be a list"
        )
    if value not in ([], ["tokens"], ["wall_time"], ["tokens", "wall_time"]):
        raise ExperimentBudgetIntegrityError(
            "threshold dimensions are not canonical"
        )
    return value


def _validate_incomplete_reason(reason):
    if not isinstance(reason, dict) or set(reason) != {
        "usage_event_id",
        "usage_status",
        "reason",
    }:
        raise ExperimentBudgetIntegrityError(
            "incomplete usage reason has invalid shape"
        )
    event_id = reason["usage_event_id"]
    if event_id is not None and (
        not isinstance(event_id, str)
        or len(event_id) != 70
        or not event_id.startswith("USAGE-")
        or any(
            character not in "0123456789abcdef"
            for character in event_id[len("USAGE-"):]
        )
    ):
        raise ExperimentBudgetIntegrityError(
            "incomplete usage identity is invalid"
        )
    if reason["usage_status"] not in {
        "partial",
        "unavailable",
        "not_applicable",
        "missing",
        "invalid",
    }:
        raise ExperimentBudgetIntegrityError(
            "incomplete usage status is invalid"
        )
    _validated_identifier(reason["reason"], "incomplete usage reason")


def _canonical_invocation_id(value):
    if (
        not isinstance(value, str)
        or not value.startswith("INV-")
        or len(value) <= len("INV-")
    ):
        return False
    suffix = value[len("INV-"):]
    return suffix[0].isalnum() and all(
        character.isalnum() or character in "_.:-"
        for character in suffix
    )


def _canonical_usage_event_id(value):
    return (
        isinstance(value, str)
        and len(value) == 70
        and value.startswith("USAGE-")
        and all(
            character in "0123456789abcdef"
            for character in value[len("USAGE-"):]
        )
    )


@lru_cache(maxsize=1)
def _terminal_usage_validator():
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "model_invocation_usage.schema.json"
    )
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentBudgetIntegrityError(
            f"terminal usage authority schema is unavailable: {exc}"
        ) from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _terminal_usage_schema_error(usage):
    errors = sorted(
        _terminal_usage_validator().iter_errors(usage),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    if not errors:
        return None
    first = errors[0]
    location = ".".join(str(part) for part in first.absolute_path) or "<root>"
    return f"{location}:{first.validator}"


def _validate_string_mapping(value, field, *, value_pattern=None):
    if not isinstance(value, dict):
        raise ExperimentBudgetIntegrityError(f"{field} must be an object")
    if list(value) != sorted(value):
        raise ExperimentBudgetIntegrityError(f"{field} must be key-sorted")
    for key, item in value.items():
        _validated_identifier(key, f"{field} key")
        _validated_identifier(item, f"{field} value")
        if value_pattern == "sha256" and (
            len(item) != 64 or any(char not in "0123456789abcdef" for char in item)
        ):
            raise ExperimentBudgetIntegrityError(
                f"{field} values must be sha256 digests"
            )


def _contained_authority_file(root, value, label):
    if not isinstance(value, (str, Path)):
        raise ExperimentBudgetIntegrityError(
            f"{label} path must be a string or Path"
        )
    path = Path(value)
    if ".." in path.parts:
        raise ExperimentBudgetIntegrityError(
            f"{label} path traverses outside trusted authority"
        )
    unresolved = path if path.is_absolute() else root / path
    try:
        relative = unresolved.relative_to(root)
    except ValueError as exc:
        raise ExperimentBudgetIntegrityError(
            f"{label} escapes the trusted authority root"
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ExperimentBudgetIntegrityError(
                f"{label} path contains a symbolic link"
            )
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ExperimentBudgetIntegrityError(
            f"{label} escapes the trusted authority root"
        ) from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise ExperimentBudgetIntegrityError(
            f"{label} is not a regular authority file"
        )
    return candidate


def _validated_authority_paths(authority_root, events_path):
    configured_root = Path(authority_root)
    if configured_root.is_symlink():
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority root cannot be a symbolic link"
        )
    try:
        root = configured_root.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority root is unavailable"
        ) from exc
    if not root.is_dir():
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority root must be a directory"
        )
    events_file = _contained_authority_file(
        root,
        events_path,
        "canonical event log",
    )
    return root, events_file


def _initialize_authority(authority_root, events_path):
    configured_root = Path(authority_root)
    if configured_root.is_symlink():
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority root cannot be a symbolic link"
        )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(configured_root, directory_flags)
    except OSError as exc:
        raise ExperimentBudgetIntegrityError(
            "terminal usage authority root is unavailable"
        ) from exc
    try:
        root_stat = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ExperimentBudgetIntegrityError(
                "terminal usage authority root must be a directory"
            )
        try:
            root = configured_root.resolve(strict=True)
            path_stat = root.stat()
        except OSError as exc:
            raise ExperimentBudgetIntegrityError(
                "terminal usage authority root changed during initialization"
            ) from exc
        if (
            path_stat.st_dev != root_stat.st_dev
            or path_stat.st_ino != root_stat.st_ino
        ):
            raise ExperimentBudgetIntegrityError(
                "terminal usage authority root changed during initialization"
            )
        relative_events_path = _authority_relative_path(
            root,
            events_path,
            "canonical event log",
        )
        events_descriptor = _open_relative_authority_file(
            root_descriptor,
            relative_events_path,
            "canonical event log",
        )
        with os.fdopen(events_descriptor, "rb") as stream:
            events_stat = os.fstat(stream.fileno())
            events_bytes = stream.read()
        return {
            "root": root,
            "root_device": root_stat.st_dev,
            "root_inode": root_stat.st_ino,
            "events_path": relative_events_path.as_posix(),
            "events_device": events_stat.st_dev,
            "events_inode": events_stat.st_ino,
            "events_bytes": events_bytes,
        }
    finally:
        os.close(root_descriptor)


def _authority_relative_path(root, value, label):
    if not isinstance(value, (str, Path)):
        raise ExperimentBudgetIntegrityError(
            f"{label} path must be a string or Path"
        )
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise ExperimentBudgetIntegrityError(
                f"{label} escapes the trusted authority root"
            ) from exc
    if ".." in path.parts or not path.parts:
        raise ExperimentBudgetIntegrityError(
            f"{label} path escapes the trusted authority root"
        )
    return path


def _open_relative_authority_file(root_descriptor, relative_path, label):
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_descriptors = []
    try:
        parent_descriptor = root_descriptor
        for part in relative_path.parts[:-1]:
            parent_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            directory_descriptors.append(parent_descriptor)
        file_descriptor = os.open(
            relative_path.parts[-1],
            file_flags,
            dir_fd=parent_descriptor,
        )
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_descriptor)
            raise ExperimentBudgetIntegrityError(
                f"{label} is not a regular authority file"
            )
        return file_descriptor
    except OSError as exc:
        raise ExperimentBudgetIntegrityError(
            f"{label} is not a contained regular file"
        ) from exc
    finally:
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _validate_authority_identity(path, expected_device, expected_inode, label):
    stat_result = Path(path).stat()
    if (
        stat_result.st_dev != expected_device
        or stat_result.st_ino != expected_inode
    ):
        raise ExperimentBudgetIntegrityError(
            f"{label} identity changed"
        )


def _read_authority_events(
    path,
    *,
    expected_device,
    expected_inode,
    required_prefix_byte_count,
    required_prefix_sha256,
):
    events = []
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            stat_result = os.fstat(stream.fileno())
            if not stat.S_ISREG(stat_result.st_mode):
                raise ExperimentBudgetIntegrityError(
                    "canonical event log is not a regular file"
                )
            if (
                stat_result.st_dev != expected_device
                or stat_result.st_ino != expected_inode
            ):
                raise ExperimentBudgetIntegrityError(
                    "canonical event log identity changed"
                )
            data = stream.read()
    except OSError as exc:
        raise ExperimentBudgetIntegrityError(
            f"canonical event log is unreadable: {exc}"
        ) from exc
    if len(data) < required_prefix_byte_count or hashlib.sha256(
        data[:required_prefix_byte_count]
    ).hexdigest() != required_prefix_sha256:
        raise ExperimentBudgetIntegrityError(
            "canonical event log append-only prefix changed"
        )
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ExperimentBudgetIntegrityError(
            "canonical event log is not UTF-8"
        ) from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExperimentBudgetIntegrityError(
                f"canonical event log line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(event, dict):
            raise ExperimentBudgetIntegrityError(
                f"canonical event log line {line_number} is not an object"
            )
        events.append(event)
    return events, data


def _unique_authority_payload(events, event_type, identity_field, identity):
    matches = []
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) else None
        if (
            event.get("event_type") == event_type
            and isinstance(payload, dict)
            and payload.get(identity_field) == identity
        ):
            matches.append(payload)
    if not matches:
        raise ExperimentBudgetIntegrityError(
            f"canonical authority lacks {event_type} source metadata"
        )
    canonical = {_canonical_json_text(payload) for payload in matches}
    if len(canonical) != 1:
        raise ExperimentBudgetIntegrityError(
            f"canonical authority has conflicting {event_type} source metadata"
        )
    return matches[0]


def _read_authority_source_record(
    root,
    payload,
    label,
    *,
    expected_root_device,
    expected_root_inode,
):
    data = _read_contained_authority_bytes(
        root,
        payload.get("_source_artifact_path"),
        label,
        expected_root_device=expected_root_device,
        expected_root_inode=expected_root_inode,
    )
    expected_digest = payload.get("_source_record_sha256")
    if not _sha256_text(expected_digest):
        raise ExperimentBudgetIntegrityError(
            f"{label} has an invalid source-record digest"
        )
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentBudgetIntegrityError(
            f"{label} source record is unreadable"
        ) from exc
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ExperimentBudgetIntegrityError(
            f"{label} source-record digest mismatch"
        )
    if not isinstance(record, dict):
        raise ExperimentBudgetIntegrityError(
            f"{label} source record is not an object"
        )
    projected = {
        key: value
        for key, value in payload.items()
        if key not in {"_source_artifact_path", "_source_record_sha256"}
    }
    if projected != record:
        raise ExperimentBudgetIntegrityError(
            f"{label} source record conflicts with canonical event"
        )
    return record, expected_digest


def _read_contained_authority_bytes(
    root,
    relative_path,
    label,
    *,
    expected_root_device,
    expected_root_inode,
):
    if not isinstance(relative_path, str) or not relative_path:
        raise ExperimentBudgetIntegrityError(
            f"{label} source path must be a nonempty relative path"
        )
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ExperimentBudgetIntegrityError(
            f"{label} source path escapes the trusted authority root"
        )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as exc:
        raise ExperimentBudgetIntegrityError(
            f"{label} authority root is unavailable"
        ) from exc
    try:
        root_stat = os.fstat(root_descriptor)
        if (
            root_stat.st_dev != expected_root_device
            or root_stat.st_ino != expected_root_inode
        ):
            raise ExperimentBudgetIntegrityError(
                "terminal usage authority root identity changed"
            )
        file_descriptor = _open_relative_authority_file(
            root_descriptor,
            relative,
            label,
        )
        with os.fdopen(file_descriptor, "rb") as stream:
            return stream.read()
    finally:
        os.close(root_descriptor)


def _sha256_text(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


# Compact aliases for later P2-03B call sites without coupling this module to
# scheduler code.
create_budget_state = create_experiment_budget_state
advance_budget = advance_experiment_budget

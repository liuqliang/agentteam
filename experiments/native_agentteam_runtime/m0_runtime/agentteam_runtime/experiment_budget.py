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
import time
from decimal import Decimal


BUDGET_STATE_SCHEMA_VERSION = "experiment_budget_state.v1"
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
    "frozen_budget_sha256",
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
_FROZEN_BUDGET_NAMESPACE = b"agentteam:experiment_budget_state.v1\x00"
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
    ):
        return create_experiment_budget_state(
            budget_id,
            max_total_tokens,
            max_wall_time_seconds,
            soft_warning_ratio,
            monotonic=self._monotonic,
        )

    def advance(self, state, terminal_usage=_NO_TERMINAL_USAGE):
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
    frozen = {
        "budget_id": budget_id,
        "max_total_tokens": max_total_tokens,
        "max_wall_time_seconds": max_wall_time_seconds,
        "soft_warning_ratio": soft_warning_ratio,
        "initial_monotonic": initial_monotonic,
    }
    state = {
        "budget_schema_version": BUDGET_STATE_SCHEMA_VERSION,
        **frozen,
        "frozen_budget_sha256": _frozen_budget_sha256(frozen),
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
        "invocation_usage_events": {},
        "warning_emitted": False,
        "exhaustion_emitted": False,
        "exhausted": False,
        "warning_dimensions": [],
        "exhaustion_dimensions": [],
        "events": [],
    }
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
        _project_terminal_usage(projected, terminal_usage)

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
    }
    if state.get("frozen_budget_sha256") != _frozen_budget_sha256(frozen):
        raise ExperimentBudgetIntegrityError(
            "frozen experiment budget configuration changed"
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


def _project_terminal_usage(state, usage):
    if not isinstance(usage, dict):
        _record_incomplete_usage(
            state,
            usage_event_id=None,
            usage_status="invalid",
            reason="terminal_usage_not_an_object",
        )
        return
    try:
        digest = hashlib.sha256(_canonical_json_bytes(usage)).hexdigest()
    except (TypeError, ValueError):
        raise ExperimentBudgetIntegrityError(
            "terminal usage must be finite canonical JSON"
        ) from None

    usage_event_id = usage.get("usage_event_id")
    valid_event_id = (
        isinstance(usage_event_id, str)
        and usage_event_id.startswith("USAGE-")
        and len(usage_event_id) > len("USAGE-")
    )
    if valid_event_id:
        prior_digest = state["usage_event_digests"].get(usage_event_id)
        if prior_digest is not None:
            if prior_digest != digest:
                raise ExperimentBudgetIntegrityError(
                    f"conflicting terminal usage identity: {usage_event_id}"
                )
            return
        state["usage_event_digests"][usage_event_id] = digest
        state["usage_event_digests"] = dict(
            sorted(state["usage_event_digests"].items())
        )

    invocation_id = usage.get("invocation_id")
    valid_invocation_id = (
        isinstance(invocation_id, str)
        and invocation_id.startswith("INV-")
        and len(invocation_id) > len("INV-")
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

    rejection = _terminal_usage_rejection(
        usage,
        valid_event_id=valid_event_id,
        valid_invocation_id=valid_invocation_id,
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
    usage,
    *,
    valid_event_id,
    valid_invocation_id,
):
    if usage.get("usage_schema_version") != TERMINAL_USAGE_SCHEMA_VERSION:
        return "unsupported_terminal_usage_schema"
    if not valid_event_id:
        return "missing_stable_usage_event_id"
    if not valid_invocation_id:
        return "missing_stable_invocation_id"
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
        not isinstance(event_id, str) or not event_id.startswith("USAGE-")
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


# Compact aliases for later P2-03B call sites without coupling this module to
# scheduler code.
create_budget_state = create_experiment_budget_state
advance_budget = advance_experiment_budget

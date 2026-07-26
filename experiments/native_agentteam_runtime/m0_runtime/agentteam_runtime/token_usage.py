import hashlib
import json


TOKEN_USAGE_FIELDS = [
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
]

TOKEN_USAGE_SOURCE_CODEX_JSONL = "codex_jsonl"
TOKEN_USAGE_SOURCE_RUNTIME_RESULT = "runtime_result"
TOKEN_USAGE_SOURCE_MIXED = "mixed"
TOKEN_USAGE_UNAVAILABLE_REASON_MISSING_RUNTIME = "missing_runtime_token_usage"
TOKEN_USAGE_UNAVAILABLE_REASON_INCOMPLETE_PROVIDER_USAGE = (
    "incomplete_provider_usage"
)
TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS = (
    "provider_session_lineage_ambiguous"
)
TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_PREDECESSOR_MISSING = (
    "provider_session_predecessor_missing"
)
TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_NON_MONOTONIC = (
    "provider_session_usage_non_monotonic"
)


_TERMINAL_USAGE_EVENT_TYPES = {
    "response.completed",
    "response_completed",
    "turn.completed",
    "turn_completed",
}
_TERMINAL_USAGE_KEYS = (
    "usage",
    "token_usage",
    "total_token_usage",
)
_USAGE_EVENT_ID_NAMESPACE = b"agentteam:model_invocation_usage.v1\x00"


_FIELD_ALIASES = {
    "input_tokens": ["input_tokens", "prompt_tokens", "prompt"],
    "output_tokens": ["output_tokens", "completion_tokens", "completion"],
    "total_tokens": ["total_tokens", "total"],
    "cached_input_tokens": [
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_prompt_tokens",
    ],
    "reasoning_tokens": ["reasoning_tokens"],
}


def token_usage_from_result(result):
    if not isinstance(result, dict):
        return None
    output = result.get("runtime_output")
    if not isinstance(output, dict):
        output = result.get("output") if isinstance(result.get("output"), dict) else {}
    for candidate in [
        result.get("token_usage"),
        result.get("usage"),
        output.get("token_usage"),
        output.get("usage"),
    ]:
        usage = normalize_token_usage(
            candidate,
            default_source=TOKEN_USAGE_SOURCE_RUNTIME_RESULT,
        )
        if usage:
            return usage
    return None


def usage_event_id_for_invocation(invocation_id):
    """Derive the replay-stable terminal usage identity for one invocation."""
    if not isinstance(invocation_id, str) or not invocation_id.startswith("INV-"):
        raise ValueError("invocation_id must be a stable INV- identifier")
    digest = hashlib.sha256(
        _USAGE_EVENT_ID_NAMESPACE + invocation_id.encode("utf-8")
    ).hexdigest()
    return f"USAGE-{digest}"


def token_usage_from_jsonl(
    text,
    *,
    provider_usage_scope=None,
    session_context=None,
    **lineage_context,
):
    """Return legacy-shaped usage selected by the strict terminal parser.

    Invocation-scoped results retain the compact historical return shape used
    by runtime-result aggregation.  Lifecycle terminal writers should call
    ``parse_terminal_usage_from_jsonl`` to retain status, scope, accounting,
    snapshot, and unavailable-reason fields.
    """
    parsed = _parse_terminal_usage_from_jsonl(
        text,
        provider_usage_scope=provider_usage_scope,
        session_context=session_context,
        **lineage_context,
    )
    if not isinstance(parsed, dict):
        return None
    if parsed.get("provider_usage_scope") != "invocation":
        return parsed
    return normalize_token_usage(
        parsed,
        default_source=TOKEN_USAGE_SOURCE_CODEX_JSONL,
    )


def parse_terminal_usage_from_jsonl(
    text,
    *,
    provider_usage_scope=None,
    session_context=None,
    **lineage_context,
):
    """Return the strict structured terminal usage accounting result."""
    return _parse_terminal_usage_from_jsonl(
        text,
        provider_usage_scope=provider_usage_scope,
        session_context=session_context,
        **lineage_context,
    )


def _parse_terminal_usage_from_jsonl(
    text,
    *,
    provider_usage_scope=None,
    session_context=None,
    **lineage_context,
):
    """Decode one authoritative terminal provider payload.

    ``session_context`` is deliberately separate from provider output.  It may
    contain the keys consumed by ``_session_delta_usage``; keyword arguments
    are merged over it for compatibility with call sites that already carry
    those fields separately.
    """
    terminal = None
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = _terminal_usage_candidate(event)
        if candidate is not None:
            terminal = (event, candidate)
    if terminal is None:
        return None

    event, candidate = terminal
    scope = _provider_usage_scope(
        event,
        candidate,
        requested_scope=provider_usage_scope,
    )
    context = {}
    if isinstance(session_context, dict):
        context.update(session_context)
    context.update(lineage_context)
    context = _provider_lineage_context(event, candidate, context)
    snapshot = _provider_snapshot(candidate)

    if scope == "session_cumulative":
        return _session_delta_usage(snapshot, context)
    if scope == "unknown":
        return _unattributable_session_usage(
            snapshot,
            TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS,
            provider_usage_scope="unknown",
        )
    return _invocation_usage(snapshot)


def normalize_token_usage(candidate, default_source=None):
    if not isinstance(candidate, dict):
        return None
    usage = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        usage[canonical] = _first_int(candidate, aliases)
    if usage["total_tokens"] is None:
        usage["total_tokens"] = _computed_total(usage)
    if not any(usage[field] is not None for field in TOKEN_USAGE_FIELDS):
        return None
    source = _usage_source(candidate, default_source=default_source)
    if source:
        usage["usage_source"] = source
    return usage


def _terminal_usage_candidate(event):
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type not in _TERMINAL_USAGE_EVENT_TYPES:
        return None
    for key in _TERMINAL_USAGE_KEYS:
        candidate = event.get(key)
        if isinstance(candidate, dict):
            return candidate
    return None


def _provider_usage_scope(event, candidate, requested_scope=None):
    for value in (
        requested_scope,
        event.get("provider_usage_scope"),
        event.get("usage_scope"),
        candidate.get("provider_usage_scope"),
        candidate.get("usage_scope"),
    ):
        if value in {"invocation", "session_cumulative", "unknown"}:
            return value
    # The frozen turn/response completion contract reports cumulative
    # snapshots for this invocation.  "Cumulative" does not imply a resumed
    # provider-session total unless the provider says so explicitly.
    return "invocation"


def _provider_lineage_context(event, candidate, supplied):
    context = dict(supplied)
    aliases = {
        "provider_session_id": ("provider_session_id", "session_id"),
        "provider_predecessor_invocation_id": (
            "provider_predecessor_invocation_id",
        ),
        "provider_turn_id": ("provider_turn_id", "turn_id"),
        "provider_predecessor_turn_id": (
            "provider_predecessor_turn_id",
            "predecessor_turn_id",
        ),
        "resume_mode": ("resume_mode", "provider_resume_mode"),
    }
    for canonical, keys in aliases.items():
        if context.get(canonical) is not None:
            continue
        for source in (event, candidate):
            for key in keys:
                value = source.get(key)
                if value is not None:
                    context[canonical] = value
                    break
            if context.get(canonical) is not None:
                break
    return context


def _provider_snapshot(candidate):
    return {
        field: _first_int(candidate, _FIELD_ALIASES[field])
        for field in TOKEN_USAGE_FIELDS
    }


def _invocation_usage(snapshot):
    usage = {
        "usage_status": (
            "reported" if _has_scoring_fields(snapshot) else "partial"
        ),
        "usage_source": TOKEN_USAGE_SOURCE_CODEX_JSONL,
        "provider_usage_scope": "invocation",
        "accounting_method": "provider_reported",
        "provider_usage_snapshot": None,
        "unavailable_reason": None,
        **snapshot,
    }
    if usage["usage_status"] == "partial":
        usage["unavailable_reason"] = (
            TOKEN_USAGE_UNAVAILABLE_REASON_INCOMPLETE_PROVIDER_USAGE
        )
    return usage


def _session_delta_usage(snapshot, context):
    reason = _session_lineage_rejection_reason(context)
    previous = _previous_provider_snapshot(context)
    if reason is None and previous is None:
        reason = TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_PREDECESSOR_MISSING
    if reason is None and not _snapshot_is_monotonic(snapshot, previous):
        reason = TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_NON_MONOTONIC
    if reason is not None:
        return _unattributable_session_usage(snapshot, reason)

    delta = {
        field: _optional_delta(snapshot.get(field), previous.get(field))
        for field in TOKEN_USAGE_FIELDS
    }
    if not _has_scoring_fields(delta):
        return _unattributable_session_usage(
            snapshot,
            TOKEN_USAGE_UNAVAILABLE_REASON_INCOMPLETE_PROVIDER_USAGE,
        )
    return {
        "usage_status": "reported",
        "usage_source": TOKEN_USAGE_SOURCE_CODEX_JSONL,
        "provider_usage_scope": "session_cumulative",
        "accounting_method": "session_delta",
        "provider_usage_snapshot": snapshot,
        "unavailable_reason": None,
        **delta,
    }


def _session_lineage_rejection_reason(context):
    resume_mode = context.get("resume_mode")
    if resume_mode == "resume_last" or context.get("is_resume_last") is True:
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS
    if context.get("concurrent") or context.get("forked") or context.get("unseen_turns"):
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS
    if context.get("lineage_status") not in (None, "authoritative"):
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS
    if not _context_truth(
        context,
        "session_writer_exclusive",
        "provider_session_lock_held",
        "single_writer",
    ):
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS
    if not _context_truth(
        context,
        "project_binding_matches",
        "provider_project_binding_valid",
    ):
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS

    required = (
        "provider_session_id",
        "provider_predecessor_invocation_id",
        "provider_turn_id",
        "provider_predecessor_turn_id",
    )
    if any(not _nonempty_text(context.get(key)) for key in required):
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_PREDECESSOR_MISSING

    previous = _previous_provider_snapshot(context)
    if previous is None:
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_PREDECESSOR_MISSING
    previous_session = _context_value(
        context,
        "previous_provider_session_id",
        "predecessor_provider_session_id",
    )
    previous_invocation = _context_value(
        context,
        "previous_invocation_id",
        "predecessor_invocation_id",
    )
    previous_turn = _context_value(
        context,
        "previous_provider_turn_id",
        "predecessor_provider_turn_id",
    )
    if isinstance(context.get("previous_provider_snapshot"), dict):
        metadata = context["previous_provider_snapshot"]
    elif isinstance(context.get("previous_snapshot"), dict):
        metadata = context["previous_snapshot"]
    else:
        metadata = context.get("predecessor_snapshot", {})
    if isinstance(metadata, dict):
        previous_session = previous_session or metadata.get("provider_session_id")
        previous_invocation = previous_invocation or metadata.get("invocation_id")
        previous_turn = previous_turn or metadata.get("provider_turn_id")
    if (
        previous_session != context["provider_session_id"]
        or previous_invocation != context["provider_predecessor_invocation_id"]
        or previous_turn != context["provider_predecessor_turn_id"]
    ):
        return TOKEN_USAGE_UNAVAILABLE_REASON_SESSION_LINEAGE_AMBIGUOUS
    return None


def _previous_provider_snapshot(context):
    for key in (
        "previous_provider_snapshot",
        "previous_snapshot",
        "predecessor_snapshot",
    ):
        value = context.get(key)
        if isinstance(value, dict):
            return _provider_snapshot(value)
    return None


def _snapshot_is_monotonic(current, previous):
    for field in TOKEN_USAGE_FIELDS:
        current_value = current.get(field)
        previous_value = previous.get(field)
        if current_value is not None and previous_value is not None:
            if current_value < previous_value:
                return False
    return True


def _optional_delta(current, previous):
    if current is None or previous is None:
        return None
    return current - previous


def _unattributable_session_usage(
    snapshot,
    reason,
    *,
    provider_usage_scope="session_cumulative",
):
    return {
        "usage_status": "unavailable",
        "usage_source": TOKEN_USAGE_SOURCE_CODEX_JSONL,
        "provider_usage_scope": provider_usage_scope,
        "accounting_method": "unavailable",
        "provider_usage_snapshot": snapshot,
        "unavailable_reason": reason,
        **{field: None for field in TOKEN_USAGE_FIELDS},
    }


def _has_scoring_fields(usage):
    return all(usage.get(field) is not None for field in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
    ))


def _context_truth(context, *keys):
    return any(context.get(key) is True for key in keys)


def _context_value(context, *keys):
    for key in keys:
        value = context.get(key)
        if value is not None:
            return value
    return None


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def aggregate_token_usage(usages, expected_count=0):
    usages = list(usages or [])
    not_applicable = [_not_applicable_usage(usage) for usage in usages]
    not_applicable = [usage for usage in not_applicable if usage]
    normalized = []
    for usage in usages:
        normalized_usage = normalize_token_usage(usage)
        if normalized_usage:
            normalized.append(normalized_usage)
    expected_count = max(int(expected_count or 0), len(normalized))
    reported_count = len(normalized)
    unreported_count = max(expected_count - reported_count, 0)
    if reported_count == 0:
        if not_applicable and len(not_applicable) == expected_count:
            return _aggregate_not_applicable_usage(not_applicable)
        return {
            "usage_status": "unavailable",
            "reason": TOKEN_USAGE_UNAVAILABLE_REASON_MISSING_RUNTIME,
            "unavailable_reason": TOKEN_USAGE_UNAVAILABLE_REASON_MISSING_RUNTIME,
            "reported_attempt_count": 0,
            "unreported_attempt_count": expected_count,
            **{field: None for field in TOKEN_USAGE_FIELDS},
        }
    totals = {
        field: _sum_optional(usage.get(field) for usage in normalized)
        for field in TOKEN_USAGE_FIELDS
    }
    aggregate = {
        "usage_status": "reported" if unreported_count == 0 else "partial",
        "reported_attempt_count": reported_count,
        "unreported_attempt_count": unreported_count,
        **totals,
    }
    sources = _usage_sources(normalized)
    if sources:
        aggregate["usage_sources"] = sources
        aggregate["usage_source"] = (
            sources[0] if len(sources) == 1 else TOKEN_USAGE_SOURCE_MIXED
        )
    return aggregate


def aggregate_token_usage_from_results(results):
    results = [result for result in results if isinstance(result, dict)]
    usages = [token_usage_from_result(result) for result in results]
    return aggregate_token_usage(usages, expected_count=len(results))


def token_usage_from_state(state):
    if not isinstance(state, dict):
        return aggregate_token_usage([], expected_count=0)
    results = []
    for step in state.get("steps", []):
        if not isinstance(step, dict):
            continue
        result = step.get("result")
        if isinstance(result, dict):
            results.append(result)
    return aggregate_token_usage_from_results(results)


def format_token_usage(usage, label="Token usage"):
    if isinstance(usage, dict) and usage.get("usage_status") == "not_applicable":
        reason = usage.get("reason")
        suffix = f" ({reason})" if reason else ""
        return f"{label}: not applicable{suffix}"
    if isinstance(usage, dict) and usage.get("usage_status") == "unavailable":
        reason = usage.get("unavailable_reason") or usage.get("reason")
        suffix = f" ({reason})" if reason else ""
        return f"{label}: unavailable{suffix}"
    if not isinstance(usage, dict):
        return f"{label}: unavailable"
    total = _value_text(usage.get("total_tokens"))
    input_tokens = _value_text(usage.get("input_tokens"))
    output_tokens = _value_text(usage.get("output_tokens"))
    reported = usage.get("reported_attempt_count", 0)
    unreported = usage.get("unreported_attempt_count", 0)
    expected = reported + unreported
    suffix = f" reported={reported}/{expected}" if expected else ""
    source = usage.get("usage_source")
    if isinstance(source, str) and source.strip():
        suffix = f"{suffix} source={source.strip()}"
    return f"{label}: total={total} input={input_tokens} output={output_tokens}{suffix}"


def _first_int(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _usage_source(candidate, default_source=None):
    source = candidate.get("usage_source") or candidate.get("token_usage_source")
    if isinstance(source, str) and source.strip():
        return source.strip()
    if isinstance(default_source, str) and default_source.strip():
        return default_source.strip()
    return None


def _usage_sources(usages):
    sources = []
    for usage in usages:
        values = usage.get("usage_sources")
        if not isinstance(values, list):
            values = [usage.get("usage_source")]
        for value in values:
            if isinstance(value, str) and value.strip():
                sources.append(value.strip())
    return sorted(set(sources))


def _not_applicable_usage(usage):
    if not isinstance(usage, dict):
        return None
    if usage.get("usage_status") != "not_applicable":
        return None
    reason = usage.get("reason")
    return {
        "usage_status": "not_applicable",
        "reason": reason if isinstance(reason, str) and reason.strip() else None,
    }


def _aggregate_not_applicable_usage(usages):
    reasons = sorted(
        {
            usage["reason"]
            for usage in usages
            if isinstance(usage.get("reason"), str) and usage["reason"].strip()
        }
    )
    aggregate = {
        "usage_status": "not_applicable",
        "reported_attempt_count": 0,
        "unreported_attempt_count": 0,
        **{field: None for field in TOKEN_USAGE_FIELDS},
    }
    if reasons:
        aggregate["reason"] = reasons[0] if len(reasons) == 1 else "mixed"
        aggregate["reasons"] = reasons
    return aggregate


def _computed_total(usage):
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None
    return input_tokens + output_tokens


def _sum_optional(values):
    numbers = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    return sum(numbers) if numbers else None


def _value_text(value):
    return str(value) if value is not None else "unknown"

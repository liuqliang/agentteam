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


def token_usage_from_jsonl(text):
    usage = None
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for candidate in _usage_candidates(event):
            normalized = normalize_token_usage(
                candidate,
                default_source=TOKEN_USAGE_SOURCE_CODEX_JSONL,
            )
            if normalized:
                usage = normalized
    return usage


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


def _usage_candidates(value):
    if not isinstance(value, dict):
        return []
    candidates = []
    for key in [
        "token_usage",
        "usage",
        "total_token_usage",
        "usage_delta",
        "tokens",
    ]:
        candidate = value.get(key)
        if isinstance(candidate, dict):
            candidates.append(candidate)
    if _looks_like_usage(value):
        candidates.append(value)
    for nested in value.values():
        if isinstance(nested, dict):
            candidates.extend(_usage_candidates(nested))
        elif isinstance(nested, list):
            for item in nested:
                candidates.extend(_usage_candidates(item))
    return candidates


def _looks_like_usage(value):
    return any(
        alias in value
        for aliases in _FIELD_ALIASES.values()
        for alias in aliases
    )


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
        if isinstance(value, int):
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

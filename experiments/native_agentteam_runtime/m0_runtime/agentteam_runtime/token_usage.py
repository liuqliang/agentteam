TOKEN_USAGE_FIELDS = [
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
]


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
        usage = normalize_token_usage(candidate)
        if usage:
            return usage
    return None


def normalize_token_usage(candidate):
    if not isinstance(candidate, dict):
        return None
    usage = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        usage[canonical] = _first_int(candidate, aliases)
    if usage["total_tokens"] is None:
        usage["total_tokens"] = _computed_total(usage)
    if not any(usage[field] is not None for field in TOKEN_USAGE_FIELDS):
        return None
    return usage


def aggregate_token_usage(usages, expected_count=0):
    normalized = []
    for usage in usages:
        normalized_usage = normalize_token_usage(usage)
        if normalized_usage:
            normalized.append(normalized_usage)
    expected_count = max(int(expected_count or 0), len(normalized))
    reported_count = len(normalized)
    unreported_count = max(expected_count - reported_count, 0)
    if reported_count == 0:
        return {
            "usage_status": "unavailable",
            "reported_attempt_count": 0,
            "unreported_attempt_count": expected_count,
            **{field: None for field in TOKEN_USAGE_FIELDS},
        }
    totals = {
        field: _sum_optional(usage.get(field) for usage in normalized)
        for field in TOKEN_USAGE_FIELDS
    }
    return {
        "usage_status": "reported" if unreported_count == 0 else "partial",
        "reported_attempt_count": reported_count,
        "unreported_attempt_count": unreported_count,
        **totals,
    }


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
    if not isinstance(usage, dict) or usage.get("usage_status") == "unavailable":
        return f"{label}: unavailable"
    total = _value_text(usage.get("total_tokens"))
    input_tokens = _value_text(usage.get("input_tokens"))
    output_tokens = _value_text(usage.get("output_tokens"))
    reported = usage.get("reported_attempt_count", 0)
    unreported = usage.get("unreported_attempt_count", 0)
    expected = reported + unreported
    suffix = f" reported={reported}/{expected}" if expected else ""
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

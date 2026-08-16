"""Deterministic Codex context and tool-round budget controls."""

from __future__ import annotations

import base64
import copy
import json
import shlex
import textwrap


LEGACY_CONTEXT_POLICY_FIELDS = frozenset(
    {
        "tool_output_token_limit",
        "web_search_policy",
    }
)
CONTEXT_BUDGET_POLICY_FIELDS = frozenset(
    {
        *LEGACY_CONTEXT_POLICY_FIELDS,
        "model_auto_compact_token_limit",
        "tool_call_soft_limit",
        "tool_call_hard_limit",
        "tool_budget_policy",
    }
)
TOOL_BUDGET_POLICY = "codex_pre_tool_budget.v1"
TOOL_BUDGET_ROUTE_SCHEMA_VERSION = "tool_budget_route.v1"
HOOK_TRUST_BYPASS_OPTION = "--dangerously-bypass-hook-trust"
_COUNTED_CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
    }
)
_TOOL_BUDGET_ROLE_GROUPS = {
    "implementation_worker": "implementation",
    "worker_agent": "implementation",
    "worker": "implementation",
    "repo_map_agent": "repo_map",
    "repo_map": "repo_map",
    "taskpack_author": "taskpack_author",
    "reviewer": "review_or_control",
    "code_reviewer": "review_or_control",
    "repair_worker": "implementation",
    "review_or_repair": "review_or_control",
    "evaluator": "review_or_control",
    "integration_reviewer": "review_or_control",
    "architecture_authority": "review_or_control",
    "planner": "review_or_control",
    "task_planner": "review_or_control",
    "task_slicer": "review_or_control",
    "follow_up_author": "taskpack_author",
    "semantic_architecture_agent": "review_or_control",
    "semantic_architecture": "review_or_control",
    "runtime_diagnostic": "review_or_control",
    "development_smoke": "review_or_control",
    "acceptance_live_smoke": "review_or_control",
}
_TOOL_BUDGET_LIMITS = {
    "implementation": {
        "L0": (12, 16),
        "L1": (20, 28),
        "L2": (28, 40),
        "L3": (40, 56),
    },
    "repo_map": {
        "L0": (12, 16),
        "L1": (20, 28),
        "L2": (28, 40),
        "L3": (40, 56),
    },
    "taskpack_author": {
        "L0": (16, 24),
        "L1": (20, 28),
        "L2": (24, 32),
        "L3": (32, 44),
    },
    "review_or_control": {
        "L0": (12, 16),
        "L1": (16, 24),
        "L2": (20, 28),
        "L3": (28, 40),
    },
}
_CONTROLLED_CONFIGURATION_PREFIXES = (
    "tool_output_token_limit=",
    "web_search=",
    "model_auto_compact_token_limit=",
    "hooks.PreToolUse=",
    "hooks.PostToolUse=",
)

_HOOK_SOURCE = textwrap.dedent(
    r"""
    import fcntl
    import hashlib
    import json
    import os
    import pathlib
    import sys

    soft_limit = int(sys.argv[1])
    hard_limit = int(sys.argv[2])
    request = json.load(sys.stdin)
    session_id = str(request.get("session_id") or "")
    turn_id = str(request.get("turn_id") or "")
    if not session_id or not turn_id:
        raise SystemExit("missing Codex hook session or turn identity")

    state_root = pathlib.Path(
        os.environ.get(
            "AGENTTEAM_TOOL_BUDGET_STATE_DIR",
            "/tmp/agentteam-tool-budget",
        )
    )
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    identity = hashlib.sha256(
        (session_id + "\0" + turn_id).encode("utf-8")
    ).hexdigest()
    state_path = state_root / (identity + ".count")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(state_path, flags, 0o600)
    hook_event = str(request.get("hook_event_name") or "")
    if hook_event != "PreToolUse":
        raise SystemExit("unsupported Codex hook event")
    with os.fdopen(descriptor, "r+", encoding="ascii") as state:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        raw = state.read().strip()
        count = int(raw) if raw else 0
        admitted = count < hard_limit
        if admitted:
            count += 1
            state.seek(0)
            state.truncate()
            state.write(str(count))
            state.flush()
            os.fsync(state.fileno())

    response = {}
    if not admitted:
        response = {
            "decision": "block",
            "reason": (
                "AgentTeam has reached the frozen hard tool-call limit of "
                f"{hard_limit}. No further tools may run. Preserve the "
                "current workspace and provide the final report now."
            ),
        }
    elif count in {soft_limit, hard_limit}:
        remaining = max(hard_limit - count, 0)
        message = (
            "AgentTeam tool budget warning: "
            f"{remaining} tool calls remain. Stop broad exploration, "
            "preserve the candidate patch, run only essential focused "
            "checks, and finalize. If implementation is complete but "
            "verification cannot finish, return the explicit "
            "verification_deferred tool_budget_exhausted handoff with "
            "controller-runnable verification additions."
        )
        if count >= hard_limit:
            message = (
                "AgentTeam hard tool-call limit reached. No further tools "
                "will be admitted. Preserve the current workspace and "
                "provide the final report now. If only verification is "
                "unfinished, use the explicit verification_deferred "
                "tool_budget_exhausted handoff."
            )
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": message,
            }
        }
    print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    """
).strip()


def normalize_context_budget_policy(value):
    """Return a validated legacy or bounded context-policy projection."""

    if not isinstance(value, dict):
        raise ValueError("model context policy must be an object")
    present = CONTEXT_BUDGET_POLICY_FIELDS.intersection(value)
    if not present:
        return {}
    if present == LEGACY_CONTEXT_POLICY_FIELDS:
        policy = {
            field: value[field]
            for field in sorted(LEGACY_CONTEXT_POLICY_FIELDS)
        }
    elif present == CONTEXT_BUDGET_POLICY_FIELDS:
        policy = {
            field: value[field]
            for field in sorted(CONTEXT_BUDGET_POLICY_FIELDS)
        }
    else:
        raise ValueError("model context policy fields are incomplete")

    output_limit = policy["tool_output_token_limit"]
    if (
        not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or not 256 <= output_limit <= 100_000
    ):
        raise ValueError("tool output token limit is invalid")
    if policy["web_search_policy"] != "disabled":
        raise ValueError("web search policy must be disabled")

    if present == CONTEXT_BUDGET_POLICY_FIELDS:
        compact_limit = policy["model_auto_compact_token_limit"]
        soft_limit = policy["tool_call_soft_limit"]
        hard_limit = policy["tool_call_hard_limit"]
        if (
            not isinstance(compact_limit, int)
            or isinstance(compact_limit, bool)
            or not 8_192 <= compact_limit <= 1_000_000
        ):
            raise ValueError("model auto-compact token limit is invalid")
        if (
            not isinstance(soft_limit, int)
            or isinstance(soft_limit, bool)
            or not 1 <= soft_limit <= 1_000
        ):
            raise ValueError("tool-call soft limit is invalid")
        if (
            not isinstance(hard_limit, int)
            or isinstance(hard_limit, bool)
            or not soft_limit < hard_limit <= 1_000
        ):
            raise ValueError("tool-call hard limit must exceed the soft limit")
        if policy["tool_budget_policy"] != TOOL_BUDGET_POLICY:
            raise ValueError("tool budget policy is unsupported")
    return policy


def select_tool_budget_route(base_policy, *, role, risk_target):
    """Select deterministic Codex tool limits for one invocation."""

    policy = normalize_context_budget_policy(base_policy)
    if set(policy) != CONTEXT_BUDGET_POLICY_FIELDS:
        raise ValueError("tool budget routing requires a complete context policy")
    role_group = _TOOL_BUDGET_ROLE_GROUPS.get(role)
    if role_group is None:
        raise ValueError(f"unsupported tool budget role: {role}")
    limits = _TOOL_BUDGET_LIMITS[role_group].get(risk_target)
    if limits is None:
        raise ValueError(f"unsupported tool budget risk target: {risk_target}")
    soft_limit, hard_limit = limits
    selected_policy = copy.deepcopy(base_policy)
    selected_policy["tool_call_soft_limit"] = soft_limit
    selected_policy["tool_call_hard_limit"] = hard_limit
    return {
        "schema_version": TOOL_BUDGET_ROUTE_SCHEMA_VERSION,
        "role": role,
        "role_group": role_group,
        "risk_target": risk_target,
        "soft_limit": soft_limit,
        "hard_limit": hard_limit,
        "policy": selected_policy,
    }


def replace_codex_context_policy_arguments(command, value):
    """Replace controlled Codex `-c` options with one exact policy."""

    rewritten = []
    index = 0
    command = list(command)
    while index < len(command):
        item = command[index]
        if item == HOOK_TRUST_BYPASS_OPTION:
            index += 1
            continue
        if item == "-c" and index + 1 < len(command):
            configuration = command[index + 1]
            if any(
                isinstance(configuration, str)
                and configuration.startswith(prefix)
                for prefix in _CONTROLLED_CONFIGURATION_PREFIXES
            ):
                index += 2
                continue
        rewritten.append(item)
        index += 1
    rewritten.extend(codex_context_policy_arguments(value))
    return rewritten


def codex_context_policy_arguments(value):
    """Build exact Codex CLI arguments for a validated context policy."""

    policy = normalize_context_budget_policy(value)
    if not policy:
        return []
    arguments = [
        "-c",
        f"tool_output_token_limit={policy['tool_output_token_limit']}",
        "-c",
        f'web_search="{policy["web_search_policy"]}"',
    ]
    if set(policy) == CONTEXT_BUDGET_POLICY_FIELDS:
        arguments.extend(
            [
                "-c",
                "model_auto_compact_token_limit="
                f"{policy['model_auto_compact_token_limit']}",
            ]
        )
        for configuration in codex_tool_budget_hook_configurations(
            policy["tool_call_soft_limit"],
            policy["tool_call_hard_limit"],
        ):
            arguments.extend(["-c", configuration])
        arguments.append(HOOK_TRUST_BYPASS_OPTION)
    return arguments


def codex_tool_budget_hook_command(soft_limit, hard_limit):
    """Return the standalone command embedded into Codex hook config."""

    normalize_context_budget_policy(
        {
            "tool_output_token_limit": 256,
            "web_search_policy": "disabled",
            "model_auto_compact_token_limit": 8_192,
            "tool_call_soft_limit": soft_limit,
            "tool_call_hard_limit": hard_limit,
            "tool_budget_policy": TOOL_BUDGET_POLICY,
        }
    )
    encoded = base64.b64encode(_HOOK_SOURCE.encode("utf-8")).decode("ascii")
    loader = f'import base64;exec(base64.b64decode("{encoded}"))'
    return shlex.join(
        [
            "/usr/bin/python3",
            "-c",
            loader,
            str(soft_limit),
            str(hard_limit),
        ]
    )


def codex_tool_budget_hook_configurations(soft_limit, hard_limit):
    """Return the deterministic PreToolUse TOML `-c` value."""

    command = codex_tool_budget_hook_command(soft_limit, hard_limit)
    handler = (
        "[{hooks=[{type=\"command\",command="
        + json.dumps(command, ensure_ascii=True)
        + ",timeout=5}]}]"
    )
    return [
        "hooks.PreToolUse=" + handler,
    ]


def measure_codex_jsonl_tool_calls(transcript):
    """Measure a host-captured lower bound of Codex tool activity."""

    counts = {name: 0 for name in sorted(_COUNTED_CODEX_TOOL_ITEM_TYPES)}
    started_counts = {
        name: 0 for name in sorted(_COUNTED_CODEX_TOOL_ITEM_TYPES)
    }
    failed_counts = {
        name: 0 for name in sorted(_COUNTED_CODEX_TOOL_ITEM_TYPES)
    }
    observed_ids = set()
    completed_ids = set()
    malformed_lines = 0
    for line in str(transcript or "").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            malformed_lines += 1
            continue
        item = event.get("item") if isinstance(event, dict) else None
        item_type = item.get("type") if isinstance(item, dict) else None
        event_type = event.get("type")
        item_id = item.get("id") if isinstance(item, dict) else None
        if event_type == "item.started" and item_type in started_counts:
            started_counts[item_type] += 1
            if isinstance(item_id, str) and item_id:
                observed_ids.add(item_id)
        if event_type == "item.completed" and item_type in counts:
            counts[item_type] += 1
            if isinstance(item_id, str) and item_id:
                observed_ids.add(item_id)
                completed_ids.add(item_id)
            if item.get("status") == "failed":
                failed_counts[item_type] += 1
    return {
        "observed_tool_calls_lower_bound": (
            len(observed_ids)
            if observed_ids
            else max(sum(started_counts.values()), sum(counts.values()))
        ),
        "started_tool_calls": sum(started_counts.values()),
        "started_by_type": started_counts,
        "completed_tool_calls": sum(counts.values()),
        "completed_by_type": counts,
        "failed_tool_calls": sum(failed_counts.values()),
        "failed_by_type": failed_counts,
        "unmatched_started_tool_calls": len(observed_ids - completed_ids),
        "malformed_lines": malformed_lines,
        "authority": "host_captured_jsonl_lower_bound",
    }

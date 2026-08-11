from copy import deepcopy

from .retry_decision import validate_retry_decision


MODEL_ROUTING_POLICY_ID = "gpt-5.6-cost-aware.v1"
MODEL_ROUTING_SCHEMA_VERSION = "agentteam_model_routing.v1"
MODEL_ROUTING_MODES = {"adaptive", "fixed"}
REASONING_PROFILES = {"none", "low", "medium", "high", "xhigh", "max"}
RISK_TARGETS = {"L0", "L1", "L2", "L3"}

MODEL_BY_TIER = {
    "economy": "gpt-5.6-luna",
    "balanced": "gpt-5.6-terra",
    "frontier": "gpt-5.6-sol",
}

_IMPLEMENTATION_ROUTES = {
    "L0": ("economy", "medium"),
    "L1": ("balanced", "medium"),
    "L2": ("frontier", "high"),
    "L3": ("frontier", "high"),
}

_ROLE_ROUTES = {
    "repo_map_agent": ("economy", "medium"),
    "repo_map": ("economy", "medium"),
    "context_builder_agent": ("balanced", "medium"),
    "context_builder": ("balanced", "medium"),
    "taskpack_author": ("balanced", "medium"),
    "follow_up_author": ("balanced", "medium"),
    "task_planner": ("balanced", "medium"),
    "planner": ("balanced", "medium"),
    "task_slicer": ("balanced", "medium"),
    "reviewer": ("balanced", "medium"),
    "patch_reviewer": ("balanced", "medium"),
    "verification_agent": ("balanced", "medium"),
    "repair_worker": ("balanced", "medium"),
    "semantic_feedback_agent": ("frontier", "high"),
    "semantic_architecture": ("frontier", "high"),
    "semantic_architecture_agent": ("frontier", "high"),
}


def default_model_routing_policy(*, fixed_profile=None):
    if fixed_profile is not None:
        fixed_profile = _normalize_fixed_profile(fixed_profile)
        mode = "fixed"
    else:
        mode = "adaptive"
    policy = {
        "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        "policy_id": MODEL_ROUTING_POLICY_ID,
        "mode": mode,
        "retry_escalation": mode == "adaptive",
    }
    if fixed_profile is not None:
        policy["fixed_profile"] = fixed_profile
    return policy


def validate_model_routing_policy(policy):
    if policy is None:
        return None
    if not isinstance(policy, dict):
        raise ValueError("model_routing_policy must be an object")
    if policy.get("schema_version") != MODEL_ROUTING_SCHEMA_VERSION:
        raise ValueError("unsupported model_routing_policy schema_version")
    if policy.get("policy_id") != MODEL_ROUTING_POLICY_ID:
        raise ValueError("unsupported model_routing_policy policy_id")
    mode = policy.get("mode")
    if mode not in MODEL_ROUTING_MODES:
        raise ValueError("model_routing_policy.mode must be adaptive or fixed")
    retry_escalation = policy.get("retry_escalation")
    if not isinstance(retry_escalation, bool):
        raise ValueError("model_routing_policy.retry_escalation must be boolean")
    fixed_profile = policy.get("fixed_profile")
    if mode == "fixed":
        _normalize_fixed_profile(fixed_profile)
        if retry_escalation:
            raise ValueError("fixed model routing cannot enable retry escalation")
    elif fixed_profile is not None:
        raise ValueError("adaptive model routing cannot declare fixed_profile")
    unexpected = set(policy) - {
        "schema_version",
        "policy_id",
        "mode",
        "retry_escalation",
        "fixed_profile",
    }
    if unexpected:
        raise ValueError(
            "model_routing_policy has unsupported fields: "
            + ", ".join(sorted(unexpected))
        )
    return deepcopy(policy)


def select_model_route(
    policy,
    *,
    role,
    risk_target="L1",
    attempt_number=1,
    retry_decision=None,
):
    policy = validate_model_routing_policy(policy)
    if policy is None:
        return None
    if not isinstance(role, str) or not role.strip():
        raise ValueError("model routing role must be a non-empty string")
    if risk_target not in RISK_TARGETS:
        risk_target = "L2"
    if not isinstance(attempt_number, int) or attempt_number < 1:
        raise ValueError("model routing attempt_number must be >= 1")
    retry_decision = validate_retry_decision(retry_decision)
    if attempt_number == 1 and retry_decision is not None:
        raise ValueError("initial model route cannot include retry_decision")

    if policy["mode"] == "fixed":
        profile = deepcopy(policy["fixed_profile"])
        reason = "fixed policy selected the frozen model profile"
        escalation_level = 0
    else:
        tier, reasoning_profile = _base_route(role, risk_target)
        escalation_level = int(
            policy["retry_escalation"]
            and retry_decision is not None
            and retry_decision["model_escalation"]
        )
        if escalation_level:
            tier, reasoning_profile = _escalate(tier, reasoning_profile)
            reason = (
                f"retry decision {retry_decision['decision_id']} escalated "
                f"the {role}/{risk_target} base route"
            )
        elif retry_decision is not None:
            reason = (
                f"retry decision {retry_decision['decision_id']} retained "
                f"the {role}/{risk_target} base route"
            )
        else:
            reason = f"adaptive policy matched role={role} risk={risk_target}"
        profile = {
            "model": MODEL_BY_TIER[tier],
            "reasoning_profile": reasoning_profile,
        }

    return {
        "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        "policy_id": policy["policy_id"],
        "mode": policy["mode"],
        "role": role,
        "risk_target": risk_target,
        "attempt_number": attempt_number,
        "model": profile["model"],
        "reasoning_profile": profile["reasoning_profile"],
        "escalation_level": escalation_level,
        "selection_reason": reason,
    }


def route_runtime_profile(base_profile, route):
    if route is None:
        return deepcopy(base_profile)
    profile = deepcopy(base_profile)
    profile["model"] = route["model"]
    profile["reasoning_profile"] = route["reasoning_profile"]
    return profile


def validate_model_route(route):
    if route is None:
        return None
    if not isinstance(route, dict):
        raise ValueError("model_routing selection must be an object")
    required = {
        "schema_version",
        "policy_id",
        "mode",
        "role",
        "risk_target",
        "attempt_number",
        "model",
        "reasoning_profile",
        "escalation_level",
        "selection_reason",
    }
    if set(route) != required:
        raise ValueError("model_routing selection fields are invalid")
    if route["schema_version"] != MODEL_ROUTING_SCHEMA_VERSION:
        raise ValueError("model_routing selection schema_version is invalid")
    if route["policy_id"] != MODEL_ROUTING_POLICY_ID:
        raise ValueError("model_routing selection policy_id is invalid")
    if route["mode"] not in MODEL_ROUTING_MODES:
        raise ValueError("model_routing selection mode is invalid")
    if route["risk_target"] not in RISK_TARGETS:
        raise ValueError("model_routing selection risk_target is invalid")
    if not isinstance(route["attempt_number"], int) or route["attempt_number"] < 1:
        raise ValueError("model_routing selection attempt_number is invalid")
    if not isinstance(route["escalation_level"], int) or route["escalation_level"] < 0:
        raise ValueError("model_routing selection escalation_level is invalid")
    for field in ("role", "model", "selection_reason"):
        if not isinstance(route[field], str) or not route[field].strip():
            raise ValueError(f"model_routing selection {field} is invalid")
    if route["reasoning_profile"] not in REASONING_PROFILES:
        raise ValueError("model_routing selection reasoning_profile is invalid")
    return deepcopy(route)


def author_model_route(policy, *, role="taskpack_author"):
    return select_model_route(
        policy,
        role=role,
        risk_target="L1",
        attempt_number=1,
    )


def _base_route(role, risk_target):
    normalized_role = role.strip()
    if normalized_role in {"implementation_worker", "worker_agent", "worker"}:
        return _IMPLEMENTATION_ROUTES[risk_target]
    return _ROLE_ROUTES.get(normalized_role, _IMPLEMENTATION_ROUTES[risk_target])


def _escalate(tier, reasoning_profile):
    if tier == "economy":
        return "balanced", "medium"
    if tier == "balanced":
        return "frontier", "high"
    if reasoning_profile in {"none", "low", "medium"}:
        return "frontier", "high"
    if reasoning_profile == "high":
        return "frontier", "xhigh"
    return "frontier", reasoning_profile


def _normalize_fixed_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("fixed model routing requires fixed_profile")
    model = profile.get("model")
    reasoning_profile = profile.get("reasoning_profile")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("fixed_profile.model must be a non-empty string")
    if reasoning_profile not in REASONING_PROFILES:
        raise ValueError(
            "fixed_profile.reasoning_profile must be a supported reasoning effort"
        )
    if set(profile) != {"model", "reasoning_profile"}:
        raise ValueError("fixed_profile supports only model and reasoning_profile")
    return {
        "model": model.strip(),
        "reasoning_profile": reasoning_profile,
    }

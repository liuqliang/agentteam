from copy import deepcopy


RETRY_DECISION_SCHEMA_VERSION = "agentteam_retry_decision.v1"
RETRY_DECISION_ACTIONS = {
    "retry_same_model",
    "escalate_model",
    "repair_environment",
    "repair_integration",
    "replan_task",
    "review_required",
    "block",
}

_MODEL_OUTPUT_ERRORS = {
    "invalid_changed_files",
    "invalid_output_last_message_json",
    "invalid_output_last_message_shape",
    "invalid_result_status",
    "invalid_runtime_output",
    "missing_output_last_message",
}
_ENVIRONMENT_ERRORS = {
    "fallback_repository_modified",
    "invalid_provider_result_path",
    "missing_model_invocation_output_root",
    "missing_worktree_path",
    "model_invocation_execution_group_unavailable",
    "model_invocation_lifecycle_error",
    "permission_required",
    "provider_launch_failed",
}
_TRANSIENT_MARKERS = (
    "429",
    "502",
    "503",
    "504",
    "connection reset",
    "gateway timeout",
    "internal server error",
    "rate limit",
    "service unavailable",
    "temporarily unavailable",
    "temporary failure",
    "too many requests",
)
_ENVIRONMENT_MARKERS = (
    "401 unauthorized",
    "403 forbidden",
    "authentication failed",
    "command not found",
    "invalid api key",
    "module not found",
    "no module named",
    "no such file or directory",
    "operation not permitted",
    "permission denied",
)


def decide_retry(
    *,
    attempt_id,
    failure_category,
    retryable,
    runtime_result=None,
    attempt_result=None,
):
    runtime_result = runtime_result if isinstance(runtime_result, dict) else {}
    attempt_result = attempt_result if isinstance(attempt_result, dict) else {}
    output = runtime_result.get("output")
    output = output if isinstance(output, dict) else {}
    error_code = _optional_text(output.get("error"))
    result_status = _optional_text(runtime_result.get("result_status"))
    evidence = {
        "result_status": result_status,
        "runtime_error_code": error_code,
        "integration_status": _optional_text(
            attempt_result.get("integration_status")
        ),
        "integration_verification_status": _optional_text(
            attempt_result.get("integration_verification_status")
        ),
        "baseline_verification_status": _optional_text(
            attempt_result.get("baseline_verification_status")
        ),
    }

    action, reason_code, auto_retry, model_escalation = _classify_retry(
        failure_category=failure_category,
        retryable=bool(retryable),
        output=output,
        error_code=error_code,
        result_status=result_status,
        baseline_verification_status=evidence["baseline_verification_status"],
    )
    decision = {
        "schema_version": RETRY_DECISION_SCHEMA_VERSION,
        "decision_id": f"RETRY-{attempt_id}",
        "authority": "scheduler",
        "action": action,
        "reason_code": reason_code,
        "failure_category": failure_category,
        "auto_retry": auto_retry,
        "model_escalation": model_escalation,
        "evidence": evidence,
    }
    return validate_retry_decision(decision)


def validate_retry_decision(decision):
    if decision is None:
        return None
    if not isinstance(decision, dict):
        raise ValueError("retry_decision must be an object")
    required = {
        "schema_version",
        "decision_id",
        "authority",
        "action",
        "reason_code",
        "failure_category",
        "auto_retry",
        "model_escalation",
        "evidence",
    }
    if set(decision) != required:
        raise ValueError("retry_decision fields are invalid")
    if decision["schema_version"] != RETRY_DECISION_SCHEMA_VERSION:
        raise ValueError("retry_decision schema_version is invalid")
    if decision["authority"] != "scheduler":
        raise ValueError("retry_decision authority must be scheduler")
    if decision["action"] not in RETRY_DECISION_ACTIONS:
        raise ValueError("retry_decision action is invalid")
    for field in ("decision_id", "reason_code"):
        if not isinstance(decision[field], str) or not decision[field].strip():
            raise ValueError(f"retry_decision {field} is invalid")
    if decision["failure_category"] is not None and not isinstance(
        decision["failure_category"], str
    ):
        raise ValueError("retry_decision failure_category is invalid")
    if not isinstance(decision["auto_retry"], bool):
        raise ValueError("retry_decision auto_retry must be boolean")
    if not isinstance(decision["model_escalation"], bool):
        raise ValueError("retry_decision model_escalation must be boolean")
    if decision["model_escalation"] != (
        decision["action"] == "escalate_model"
    ):
        raise ValueError("retry_decision model_escalation conflicts with action")
    if decision["model_escalation"] and not decision["auto_retry"]:
        raise ValueError("model escalation requires an automatic retry")
    evidence = decision["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {
        "result_status",
        "runtime_error_code",
        "integration_status",
        "integration_verification_status",
        "baseline_verification_status",
    }:
        raise ValueError("retry_decision evidence is invalid")
    for value in evidence.values():
        if value is not None and not isinstance(value, str):
            raise ValueError("retry_decision evidence values must be strings or null")
    return deepcopy(decision)


def _classify_retry(
    *,
    failure_category,
    retryable,
    output,
    error_code,
    result_status,
    baseline_verification_status,
):
    if failure_category == "timeout":
        return (
            "retry_same_model",
            "timeout_does_not_establish_model_insufficiency",
            retryable,
            False,
        )
    if failure_category == "integration_apply_failed":
        return (
            "repair_integration",
            "integration_failure_is_not_model_selection_evidence",
            retryable,
            False,
        )
    if failure_category == "integration_verification_failed":
        if not retryable:
            return (
                "review_required",
                "integration_verification_failed_without_retry_authority",
                False,
                False,
            )
        if baseline_verification_status == "failed":
            return (
                "repair_environment",
                "integration_baseline_verification_already_failed",
                False,
                False,
            )
        if baseline_verification_status != "passed":
            return (
                "retry_same_model",
                "integration_failure_lacks_clean_baseline_evidence",
                True,
                False,
            )
        return (
            "escalate_model",
            "verified_patch_failed_deterministic_integration_checks",
            True,
            True,
        )
    if failure_category == "runtime_error" and retryable:
        return _classify_runtime_error(
            output=output,
            error_code=error_code,
            result_status=result_status,
        )
    if failure_category == "scope_violation":
        return (
            "replan_task",
            "scope_violation_requires_contract_or_task_correction",
            False,
            False,
        )
    if failure_category in {
        "missing_required_deliverables",
        "operator_summary_language",
    }:
        return (
            "review_required",
            "semantic_delivery_rejection_requires_bounded_review",
            False,
            False,
        )
    if failure_category in {"blocked", "permission_required"}:
        return (
            "repair_environment",
            "blocked_execution_requires_environment_or_operator_action",
            False,
            False,
        )
    if failure_category == "cancelled":
        return "block", "cancelled_attempt_must_not_auto_retry", False, False
    if retryable:
        return (
            "review_required",
            "retryable_failure_has_no_safe_deterministic_route",
            False,
            False,
        )
    return "block", "failure_is_not_eligible_for_automatic_retry", False, False


def _classify_runtime_error(*, output, error_code, result_status):
    text = " ".join(
        str(value)
        for value in (
            error_code,
            output.get("reason"),
            output.get("stderr"),
            output.get("stdout"),
        )
        if value is not None
    ).lower()
    if any(marker in text for marker in _ENVIRONMENT_MARKERS):
        return (
            "repair_environment",
            "runtime_environment_failure_detected",
            False,
            False,
        )
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return (
            "retry_same_model",
            "transient_provider_failure_detected",
            True,
            False,
        )
    if error_code in _ENVIRONMENT_ERRORS:
        return (
            "repair_environment",
            "runtime_environment_failure_detected",
            False,
            False,
        )
    if error_code in _MODEL_OUTPUT_ERRORS:
        return (
            "escalate_model",
            "model_output_failed_runtime_contract",
            True,
            True,
        )
    if output.get("adapter") == "codex" and output.get("exit_code") is not None:
        return (
            "retry_same_model",
            "provider_process_failed_without_model_quality_evidence",
            True,
            False,
        )
    if result_status == "failed":
        return (
            "escalate_model",
            "worker_reported_failure_without_environment_evidence",
            True,
            True,
        )
    return (
        "review_required",
        "runtime_failure_could_not_be_classified",
        False,
        False,
    )


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request

from .completion_summary import build_completion_summary
from .token_usage import aggregate_token_usage, format_token_usage


DEFAULT_NOTIFICATION_EVENT_TYPES = {
    "run_started",
    "backlog_completed",
    "run_completed",
    "run_failed",
    "run_timed_out",
    "run_stopped",
    "manual_gate_required",
    "permission_request_required",
    "integration_blocked",
    "run_stale_detected",
    "update_activated",
    "rollback_activated",
}

RICH_TEXT_VARIANT = "rich_text"
CONCISE_TEXT_VARIANT = "concise_text"
RICH_MESSAGE_REJECTION_BODY_CODES = {11232}


def build_feishu_notification_sink_from_env(
    webhook_env,
    signing_secret_env=None,
    project="default",
    env=None,
    http_post=None,
    clock=None,
    timeout_seconds=5,
):
    env = env if env is not None else os.environ
    webhook_url = env.get(webhook_env) if webhook_env else None
    if not webhook_url:
        return None
    signing_secret = env.get(signing_secret_env) if signing_secret_env else None
    return FeishuRunEventNotificationSink(
        FeishuWebhookNotifier(
            webhook_url=webhook_url,
            signing_secret=signing_secret,
            project=project,
            http_post=http_post,
            clock=clock,
            timeout_seconds=timeout_seconds,
        )
    )


def feishu_custom_bot_sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuRunEventNotificationSink:
    def __init__(self, notifier, allowed_event_types=None):
        self.notifier = notifier
        self.allowed_event_types = set(allowed_event_types or DEFAULT_NOTIFICATION_EVENT_TYPES)

    def notify(self, event, context):
        if event.get("event_type") not in self.allowed_event_types:
            return []
        return self.notifier.notify_event(
            event,
            run_dir=context.get("run_dir", "unknown"),
        )


class FeishuManualGateNotificationSink(FeishuRunEventNotificationSink):
    def __init__(self, notifier):
        super().__init__(notifier, allowed_event_types={"manual_gate_required"})


class FeishuWebhookNotifier:
    def __init__(
        self,
        webhook_url,
        signing_secret=None,
        project="default",
        http_post=None,
        clock=None,
        timeout_seconds=5,
        message_limit=1800,
        max_attempts=2,
        retry_backoff_seconds=0,
        sleep=None,
    ):
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret
        self.project = project
        self.http_post = http_post or _default_http_post
        self.clock = clock or time.time
        self.timeout_seconds = timeout_seconds
        self.message_limit = message_limit
        self.max_attempts = max(1, int(max_attempts))
        self.retry_backoff_seconds = max(0, retry_backoff_seconds)
        self.sleep = sleep or time.sleep

    def notify_manual_gate(self, event, run_dir):
        return self.notify_event(event, run_dir)

    def notify_event(self, event, run_dir):
        rich_result = self._send_payload(
            self._event_payload(event, run_dir, variant=RICH_TEXT_VARIANT),
            variant=RICH_TEXT_VARIANT,
        )
        if rich_result["sent"]:
            metadata = self._delivery_metadata(rich_result)
            return self._notification_event(
                "notification_sent",
                event,
                "sent",
                delivery_metadata=metadata,
            )

        fallback_result = None
        fallback_reason = None
        if self._rich_message_was_rejected(rich_result):
            fallback_reason = "rich_message_rejected"
            fallback_result = self._send_payload(
                self._event_payload(event, run_dir, variant=CONCISE_TEXT_VARIANT),
                variant=CONCISE_TEXT_VARIANT,
            )
            if fallback_result["sent"]:
                return self._notification_event(
                    "notification_sent",
                    event,
                    "sent",
                    delivery_metadata=self._delivery_metadata(
                        rich_result,
                        fallback_result=fallback_result,
                        fallback_reason=fallback_reason,
                    ),
                )

        failed_result = fallback_result or rich_result
        return self._notification_event(
            "notification_failed",
            event,
            "failed",
            error_class=failed_result.get("error_class") or "FeishuWebhookError",
            error_summary=failed_result.get("error_summary"),
            delivery_metadata=self._delivery_metadata(
                rich_result,
                fallback_result=fallback_result,
                fallback_reason=fallback_reason,
            ),
        )

    def _send_payload(self, payload, variant):
        attempts = []
        last_error_class = None
        last_error_summary = None
        for attempt_index in range(1, self.max_attempts + 1):
            try:
                response = self.http_post(self.webhook_url, payload, self.timeout_seconds)
            except Exception as exc:
                last_error_class = exc.__class__.__name__
                last_error_summary = self._sanitize(str(exc))
                attempts.append(
                    {
                        "variant": variant,
                        "attempt": attempt_index,
                        "result": "failed",
                        "error_class": last_error_class,
                        "error_summary": _bounded_text(last_error_summary, 300),
                    }
                )
                if attempt_index < self.max_attempts:
                    self._sleep_before_retry()
                continue

            status_code = response.get("status_code")
            body = response.get("body")
            body_code = body.get("code") if isinstance(body, dict) else None
            body_msg = body.get("msg") if isinstance(body, dict) else None
            if _feishu_response_succeeded(status_code, body_code):
                attempts.append(
                    {
                        "variant": variant,
                        "attempt": attempt_index,
                        "result": "sent",
                        "status_code": status_code,
                        "body_code": body_code,
                    }
                )
                return {
                    "sent": True,
                    "variant": variant,
                    "attempts": attempts,
                }

            last_error_class = "FeishuWebhookError"
            last_error_summary = self._sanitize(
                _feishu_error_summary(status_code, body_code, body_msg)
            )
            attempts.append(
                {
                    "variant": variant,
                    "attempt": attempt_index,
                    "result": "failed",
                    "status_code": status_code,
                    "body_code": body_code,
                    "error_class": last_error_class,
                    "error_summary": _bounded_text(last_error_summary, 300),
                }
            )
            if (
                attempt_index < self.max_attempts
                and _feishu_response_is_retryable(status_code, body_code)
            ):
                self._sleep_before_retry()
                continue
            break

        return {
            "sent": False,
            "variant": variant,
            "attempts": attempts,
            "error_class": last_error_class,
            "error_summary": last_error_summary,
        }

    def _sleep_before_retry(self):
        if self.retry_backoff_seconds:
            self.sleep(self.retry_backoff_seconds)

    def _event_payload(self, event, run_dir, variant=RICH_TEXT_VARIANT):
        timestamp = str(int(self.clock()))
        text = _event_text(event, run_dir, self.project)
        limit = self.message_limit
        if variant == CONCISE_TEXT_VARIANT:
            text = _concise_event_text(event, run_dir, self.project)
            limit = min(self.message_limit, 600)
        payload = {
            "msg_type": "text",
            "content": {
                "text": _bounded_text(
                    text,
                    limit,
                )
            },
        }
        if self.signing_secret:
            payload["timestamp"] = timestamp
            payload["sign"] = feishu_custom_bot_sign(timestamp, self.signing_secret)
        return payload

    def _delivery_metadata(self, rich_result, fallback_result=None, fallback_reason=None):
        attempts = list(rich_result.get("attempts") or [])
        if fallback_result:
            attempts.extend(fallback_result.get("attempts") or [])
        if not attempts:
            return {}
        include_metadata = (
            len(attempts) > 1
            or not rich_result.get("sent")
            or fallback_result is not None
        )
        if not include_metadata:
            return {}
        metadata = {
            "delivery_variant": attempts[-1].get("variant", rich_result.get("variant")),
            "delivery_attempt_count": len(attempts),
            "max_delivery_attempts": self.max_attempts,
            "delivery_attempts": attempts,
        }
        if fallback_result is not None:
            metadata["fallback_used"] = True
            metadata["fallback_reason"] = fallback_reason or "fallback"
        else:
            metadata["fallback_used"] = False
        return metadata

    def _rich_message_was_rejected(self, result):
        for attempt in result.get("attempts") or []:
            if attempt.get("body_code") in RICH_MESSAGE_REJECTION_BODY_CODES:
                return True
        return False

    def _notification_event(
        self,
        event_type,
        source_event,
        notification_status,
        error_class=None,
        error_summary=None,
        delivery_metadata=None,
    ):
        payload = {
            "provider": "feishu",
            "project": self.project,
            "source_event_type": source_event.get("event_type"),
            "source_event_id": source_event.get("event_id"),
            "source_event_sequence": source_event.get("sequence"),
            "notification_status": notification_status,
            "message_summary": _event_message_summary(source_event),
        }
        if error_class:
            payload["error_class"] = error_class
        if error_summary:
            payload["error_summary"] = _bounded_text(error_summary, 300)
        if delivery_metadata:
            payload.update(delivery_metadata)
        return {
            "event_type": event_type,
            "actor": "agent-notifier",
            "target_agent_id": None,
            "idempotency_key": f"notification:{source_event.get('event_id')}:{notification_status}",
            "correlation_id": source_event.get("correlation_id", "notification"),
            "payload": payload,
        }

    def _sanitize(self, value):
        redacted = value
        secrets = [
            self.webhook_url,
            self.signing_secret,
            str(self.webhook_url).rstrip("/").rsplit("/", 1)[-1],
        ]
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted


def diagnose_feishu_webhook_delivery(
    webhook_url,
    signing_secret=None,
    project="default",
    http_post=None,
    clock=None,
    timeout_seconds=5,
    max_attempts=1,
    dry_run=False,
    message=None,
):
    notifier = FeishuWebhookNotifier(
        webhook_url=webhook_url,
        signing_secret=signing_secret,
        project=project,
        http_post=http_post,
        clock=clock,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )
    event = _diagnostic_event(project, message=message)
    variants = []
    for variant in (RICH_TEXT_VARIANT, CONCISE_TEXT_VARIANT):
        payload = notifier._event_payload(event, run_dir=f"diagnose:{project}", variant=variant)
        variant_summary = _diagnostic_payload_summary(payload, variant)
        if dry_run:
            variants.append({**variant_summary, "notification_status": "dry_run"})
            continue
        result = notifier._send_payload(payload, variant=variant)
        variant_summary.update(
            {
                "notification_status": "sent" if result["sent"] else "failed",
                "delivery_attempt_count": len(result.get("attempts") or []),
                "max_delivery_attempts": notifier.max_attempts,
                "delivery_attempts": result.get("attempts") or [],
            }
        )
        if result.get("error_class"):
            variant_summary["error_class"] = result["error_class"]
        if result.get("error_summary"):
            variant_summary["error_summary"] = _bounded_text(result["error_summary"], 300)
        variants.append(variant_summary)

    if dry_run:
        diagnosis_status = "dry_run"
    elif all(variant.get("notification_status") == "sent" for variant in variants):
        diagnosis_status = "sent"
    else:
        diagnosis_status = "failed"
    return {
        "diagnosis_status": diagnosis_status,
        "provider": "feishu",
        "project": project,
        "webhook_url_set": bool(webhook_url),
        "signing_enabled": bool(signing_secret),
        "delivery_variants": variants,
    }


def _diagnostic_event(project, message=None):
    token_usage = {
        "usage_status": "not_applicable",
        "reason": "diagnostic_notification",
    }
    return {
        "event_id": "feishu-diagnosis",
        "sequence": 0,
        "event_type": "run_completed",
        "actor": "agentteam-cli",
        "target_agent_id": None,
        "idempotency_key": "notify:diagnose-feishu",
        "correlation_id": "notify:diagnose-feishu",
        "payload": {
            "run_status": "diagnostic",
            "operator_report": {
                "report_schema_version": "operator_run_report.v1",
                "task_count": 1,
                "blocked_count": 0,
                "task_reports": [
                    {
                        "task_id": "feishu-diagnosis",
                        "status": "diagnostic",
                        "what_changed": [
                            message or f"Feishu webhook delivery diagnosis for {project}."
                        ],
                        "changed_files": [],
                        "verification": ["Webhook variant delivery diagnosis was triggered."],
                        "integration": "not requested",
                        "merge_recommendation": "No merge action; notification diagnosis only.",
                        "next_steps": [
                            "Review rich_text and concise_text variant delivery results."
                        ],
                        "token_usage": token_usage,
                    }
                ],
                "token_usage": token_usage,
            },
        },
    }


def _diagnostic_payload_summary(payload, variant):
    content = payload.get("content") if isinstance(payload, dict) else {}
    text = content.get("text", "") if isinstance(content, dict) else ""
    return {
        "variant": variant,
        "msg_type": payload.get("msg_type") if isinstance(payload, dict) else None,
        "content_length": len(text),
        "signed": bool(isinstance(payload, dict) and payload.get("sign")),
    }


def _feishu_error_summary(status_code, body_code, body_msg=None):
    parts = [f"status_code={status_code}", f"body_code={body_code}"]
    if body_msg:
        parts.append(f"body_msg={body_msg}")
    return " ".join(parts)


def _feishu_response_succeeded(status_code, body_code):
    try:
        http_status = int(status_code or 0)
    except (TypeError, ValueError):
        http_status = 0
    return 200 <= http_status < 300 and body_code in {None, 0}


def _feishu_response_is_retryable(status_code, body_code):
    if body_code in RICH_MESSAGE_REJECTION_BODY_CODES:
        return False
    try:
        http_status = int(status_code or 0)
    except (TypeError, ValueError):
        return False
    return http_status in {408, 409, 425, 429} or http_status >= 500


def _manual_gate_text(event, run_dir, project):
    payload = event.get("payload", {})
    question_id = payload.get("question_id", "unknown")
    task_id = payload.get("task_id", "unknown")
    question = payload.get("question") or "Worker requested operator guidance."
    reason = payload.get("reason")
    command = (
        "python3 -m agentteam_runtime.agentteam resume "
        f"--run-dir {run_dir} --interactive --question-id {question_id}"
    )
    lines = [
        "[AgentTeam] manual gate required",
        f"Project: {project}",
        f"Task: {task_id}",
        f"Question id: {question_id}",
        f"Question: {question}",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.extend(
        [
            f"Run dir: {run_dir}",
            f"Resume: {command}",
            "Tip: use /context before /answer if you need local runtime context.",
        ]
    )
    return "\n".join(lines)


def _permission_request_text(event, run_dir, project):
    payload = event.get("payload", {})
    request_id = payload.get("request_id", "unknown")
    task_id = payload.get("task_id", "unknown")
    capability = payload.get("requested_capability") or "runtime_permission"
    reason = payload.get("reason")
    approve_command = (
        f"agentteam permissions approve --run-dir {run_dir} --request-id {request_id}"
    )
    deny_command = (
        f"agentteam permissions deny --run-dir {run_dir} --request-id {request_id}"
    )
    lines = [
        "[AgentTeam] permission request required",
        f"Project: {project}",
        f"Task: {task_id}",
        f"Request id: {request_id}",
        f"Capability: {capability}",
    ]
    if reason:
        lines.append(f"Reason: {reason}")
    lines.extend(
        [
            f"Run dir: {run_dir}",
            f"Approve: {approve_command}",
            f"Deny: {deny_command}",
        ]
    )
    return "\n".join(lines)


def _concise_event_text(event, run_dir, project):
    event_type = event.get("event_type", "event")
    payload = event.get("payload", {})
    lines = [
        f"[AgentTeam] {event_type}",
        f"Project: {project}",
    ]
    run_status = payload.get("run_status") or payload.get("scheduler_status")
    if run_status:
        lines.append(f"Status: {run_status}")
    if event_type == "backlog_completed":
        lines.append("Milestone: awaiting required post-backlog gates")
        lines.append(f"Next gate action: {payload.get('next_action') or 'agentteam report'}")
    task_id = payload.get("task_id")
    if not task_id and isinstance(payload.get("operator_report"), dict):
        task_reports = payload["operator_report"].get("task_reports")
        if isinstance(task_reports, list) and task_reports:
            first_task = task_reports[0] if isinstance(task_reports[0], dict) else {}
            task_id = first_task.get("task_id")
    if task_id:
        lines.append(f"Task: {task_id}")
    if event_type == "manual_gate_required":
        question_id = payload.get("question_id", "unknown")
        lines.append(f"Question id: {question_id}")
        lines.append(
            "Resume: python3 -m agentteam_runtime.agentteam resume "
            f"--run-dir {run_dir} --interactive --question-id {question_id}"
        )
    elif event_type == "permission_request_required":
        request_id = payload.get("request_id", "unknown")
        lines.append(f"Request id: {request_id}")
        lines.append(f"Approve: agentteam permissions approve --run-dir {run_dir} --request-id {request_id}")
        lines.append(f"Deny: agentteam permissions deny --run-dir {run_dir} --request-id {request_id}")
    lines.extend(
        [
            f"Run dir: {run_dir}",
            f"Summary: {_event_message_summary(event)}",
        ]
    )
    return "\n".join(lines)


def _event_text(event, run_dir, project):
    if event.get("event_type") == "manual_gate_required":
        return _manual_gate_text(event, run_dir, project)
    if event.get("event_type") == "permission_request_required":
        return _permission_request_text(event, run_dir, project)
    if event.get("event_type") == "backlog_completed":
        return _backlog_completed_text(event, run_dir, project)
    payload = event.get("payload", {})
    lines = [
        f"[AgentTeam] {event.get('event_type', 'event')}",
        f"Project: {project}",
    ]
    run_status = payload.get("run_status") or payload.get("scheduler_status")
    if run_status:
        lines.append(f"Status: {run_status}")
    task_id = payload.get("task_id")
    if task_id:
        lines.append(f"Task: {task_id}")
    failure = payload.get("failure_category") or payload.get("error_summary")
    if failure:
        lines.append(f"Failure: {failure}")
    operator_report = payload.get("operator_report")
    if isinstance(operator_report, dict):
        report = dict(operator_report)
        worker_diagnostics = payload.get("worker_diagnostics")
        if isinstance(worker_diagnostics, dict) and worker_diagnostics:
            report["worker_diagnostics"] = worker_diagnostics
        lines.extend(_operator_report_text(report))
    else:
        _extend_notification_worker_diagnostic_lines(lines, payload)
    lines.extend(
        [
            f"Run dir: {run_dir}",
            f"Summary: {_event_message_summary(event)}",
        ]
    )
    return "\n".join(lines)


def _backlog_completed_text(event, run_dir, project):
    payload = event.get("payload", {})
    return "\n".join(
        [
            "[AgentTeam] backlog_completed",
            f"Project: {project}",
            "Status: awaiting_post_backlog_gates",
            "Backlog: verified idle",
            "Milestone: pending; integration is not yet authorized",
            f"Next gate action: {payload.get('next_action') or 'agentteam report'}",
            f"Run dir: {run_dir}",
            f"Summary: {_event_message_summary(event)}",
        ]
    )


def _operator_report_text(report):
    task_reports = report.get("task_reports", []) if isinstance(report.get("task_reports"), list) else []
    lines = []
    operator_digest = _operator_summary_digest(report)
    if operator_digest:
        lines.append("中文工作汇报:")
        _extend_notification_pursue_recap_lines(lines, report)
        _extend_limited_section_items(lines, operator_digest)
        _extend_notification_worker_diagnostic_lines(lines, report)
        lines.append(format_token_usage(_notification_token_usage(report, task_reports)))
        return lines

    if not task_reports:
        lines.append("工作摘要:")
        lines.append("- 无结构化任务报告；请查看 agentteam report。")
        _extend_notification_pursue_recap_lines(lines, report)
        _extend_notification_worker_diagnostic_lines(lines, report)
        lines.append(format_token_usage(_notification_token_usage(report, task_reports)))
        return lines

    task_count = int(report.get("task_count") or len(task_reports))
    blocked_count = _notification_blocked_count(report, task_reports)
    summary = build_completion_summary(
        run_id=report.get("run_id") or "unknown",
        run_status=report.get("run_status") or "completed",
        task_count=task_count,
        blocked_count=blocked_count,
        task_reports=task_reports,
        integration_baseline=report.get("integration_baseline"),
    )
    lines.append("工作摘要:")
    lines.append(
        f"- 状态：{report.get('run_status') or 'completed'}；"
        f"任务：{task_count}；阻塞：{blocked_count}"
    )
    _extend_notification_pursue_recap_lines(lines, report)
    lines.append("中文工作汇报:")
    _extend_limited_section_items(lines, summary.get("operator_digest"))
    _extend_notification_worker_diagnostic_lines(lines, report)
    lines.append(format_token_usage(_notification_token_usage(report, task_reports)))
    return lines


def _extend_notification_worker_diagnostic_lines(lines, report):
    diagnostics = report.get("worker_diagnostics") if isinstance(report, dict) else None
    if not isinstance(diagnostics, dict) or not diagnostics:
        return
    pool_status = diagnostics.get("pool_diagnostic_status") or "unknown"
    counts = diagnostics.get("diagnostic_worker_counts")
    parts = [f"pool={pool_status}"]
    if isinstance(counts, dict):
        for key, value in sorted(counts.items()):
            if value:
                parts.append(f"{key}={value}")
    lines.append(f"- Worker 诊断：{'；'.join(parts)}")
    workers = diagnostics.get("workers")
    if not isinstance(workers, list):
        return
    for worker in _notable_notification_workers(workers):
        worker_id = worker.get("worker_agent_id") or worker.get("worker_id") or "unknown-worker"
        details = [f"{worker_id} diagnostic={worker.get('worker_diagnostic_state') or 'unknown'}"]
        if worker.get("worker_status"):
            details.append(f"status={worker['worker_status']}")
        if worker.get("last_activity"):
            details.append(f"activity={worker['last_activity']}")
        if worker.get("heartbeat_age_seconds") is not None:
            details.append(f"heartbeat_age_seconds={worker['heartbeat_age_seconds']}")
        lines.append(f"- Worker：{' '.join(details)}")


def _notable_notification_workers(workers):
    notable_states = {"no_heartbeat", "processing_stale", "exited", "unknown"}
    notable = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        state = worker.get("worker_diagnostic_state") or "unknown"
        if state in notable_states:
            notable.append(worker)
    return notable[:3]


def _extend_notification_pursue_recap_lines(lines, report):
    recap = report.get("pursue_recap") if isinstance(report, dict) else {}
    if not isinstance(recap, dict) or not recap:
        return
    rounds_completed = recap.get("rounds_completed")
    max_rounds = recap.get("max_rounds")
    if rounds_completed is not None or max_rounds is not None:
        lines.append(f"- Pursue 轮次：{_count_or_unknown(rounds_completed)}/{_count_or_unknown(max_rounds)}")
    stop_reason = recap.get("stop_reason")
    if stop_reason:
        lines.append(f"- 停止原因：{stop_reason}")
    next_step = _pursue_recap_next_step(recap)
    if next_step:
        lines.append(f"- 下一步：{next_step}")


def _pursue_recap_next_step(recap):
    latest_round_recap = recap.get("latest_round_recap")
    if isinstance(latest_round_recap, dict):
        next_step = _first_text(latest_round_recap.get("recommended_next_step"))
        if next_step:
            return next_step
    return _first_text(recap.get("operator_next_action"))


def _count_or_unknown(value):
    if value is None:
        return "unknown"
    return value


def _operator_summary_digest(report):
    summary = report.get("operator_summary") if isinstance(report.get("operator_summary"), dict) else {}
    digest = _text_items(summary.get("operator_digest"))
    if digest:
        return digest
    return _text_items(report.get("operator_digest"))


def _notification_blocked_count(report, task_reports):
    if report.get("blocked_count") is not None:
        return int(report.get("blocked_count") or 0)
    blocked_count = 0
    for task in task_reports:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "").lower()
        integration = str(task.get("integration") or "").lower()
        if (
            "blocked" in status
            or "failed" in status
            or "blocked" in integration
            or "failed" in integration
            or "failure" in integration
        ):
            blocked_count += 1
    return blocked_count


def _notification_token_usage(report, task_reports):
    raw = report.get("token_usage")
    if isinstance(raw, dict):
        return raw
    expected_count = int(report.get("task_count") or len(task_reports) or 0)
    return aggregate_token_usage(
        [
            task.get("token_usage")
            for task in task_reports
            if isinstance(task, dict)
        ],
        expected_count=expected_count,
    )


def _extend_limited_section_items(lines, values, limit=8):
    items = _text_items(values)
    if not items:
        return
    selected = items[:limit]
    lines.extend(f"- {item}" for item in selected)
    omitted_count = len(items) - len(selected)
    if omitted_count > 0:
        lines.append(f"- ... {omitted_count} more items in agentteam report")


def _text_items(values):
    if values is None:
        return []
    if isinstance(values, list):
        return [str(item) for item in values if item is not None and str(item)]
    if isinstance(values, tuple):
        return [str(item) for item in values if item is not None and str(item)]
    return [str(values)] if str(values) else []


def _first_text(values):
    items = _text_items(values)
    return items[0] if items else None


def _event_message_summary(event):
    event_type = event.get("event_type", "event")
    payload = event.get("payload", {})
    if event_type == "manual_gate_required":
        question_id = payload.get("question_id")
        task_id = payload.get("task_id")
        return f"manual gate {question_id} for {task_id}"
    if event_type == "permission_request_required":
        request_id = payload.get("request_id")
        task_id = payload.get("task_id")
        return f"permission request {request_id} for {task_id}"
    status = payload.get("run_status") or payload.get("scheduler_status") or payload.get("status")
    if status:
        return f"{event_type} {status}"
    task_id = payload.get("task_id")
    if task_id:
        return f"{event_type} {task_id}"
    return event_type


def _bounded_text(value, limit):
    value = str(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 15)] + "...[truncated]"


def _default_http_post(url, payload, timeout_seconds):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw_body = response.read().decode("utf-8")
        try:
            parsed_body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            parsed_body = {"raw": _bounded_text(raw_body, 300)}
        return {"status_code": response.status, "body": parsed_body}

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request


DEFAULT_NOTIFICATION_EVENT_TYPES = {
    "run_started",
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
    ):
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret
        self.project = project
        self.http_post = http_post or _default_http_post
        self.clock = clock or time.time
        self.timeout_seconds = timeout_seconds
        self.message_limit = message_limit

    def notify_manual_gate(self, event, run_dir):
        return self.notify_event(event, run_dir)

    def notify_event(self, event, run_dir):
        payload = self._event_payload(event, run_dir)
        try:
            response = self.http_post(self.webhook_url, payload, self.timeout_seconds)
        except Exception as exc:
            return self._notification_event(
                "notification_failed",
                event,
                "failed",
                error_class=exc.__class__.__name__,
                error_summary=self._sanitize(str(exc)),
            )
        status_code = response.get("status_code")
        body = response.get("body")
        body_code = body.get("code") if isinstance(body, dict) else None
        if 200 <= int(status_code or 0) < 300 and body_code in {None, 0}:
            return self._notification_event("notification_sent", event, "sent")
        return self._notification_event(
            "notification_failed",
            event,
            "failed",
            error_class="FeishuWebhookError",
            error_summary=self._sanitize(f"status_code={status_code} body_code={body_code}"),
        )

    def _event_payload(self, event, run_dir):
        timestamp = str(int(self.clock()))
        payload = {
            "msg_type": "text",
            "content": {
                "text": _bounded_text(
                    _event_text(event, run_dir, self.project),
                    self.message_limit,
                )
            },
        }
        if self.signing_secret:
            payload["timestamp"] = timestamp
            payload["sign"] = feishu_custom_bot_sign(timestamp, self.signing_secret)
        return payload

    def _notification_event(
        self,
        event_type,
        source_event,
        notification_status,
        error_class=None,
        error_summary=None,
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
    command = (
        "python3 -m agentteam_runtime.agentteam permissions approve "
        f"--run-dir {run_dir} --request-id {request_id}"
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
            f"Approve: {command}",
        ]
    )
    return "\n".join(lines)


def _event_text(event, run_dir, project):
    if event.get("event_type") == "manual_gate_required":
        return _manual_gate_text(event, run_dir, project)
    if event.get("event_type") == "permission_request_required":
        return _permission_request_text(event, run_dir, project)
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
        lines.extend(_operator_report_text(operator_report))
    lines.extend(
        [
            f"Run dir: {run_dir}",
            f"Summary: {_event_message_summary(event)}",
        ]
    )
    return "\n".join(lines)


def _operator_report_text(report):
    lines = ["Operator report:"]
    for task in report.get("task_reports", []):
        if not isinstance(task, dict):
            continue
        task_id = task.get("task_id") or "unknown"
        status = task.get("status") or "unknown"
        lines.append(f"Task: {task_id}")
        lines.append(f"Status: {status}")
        _extend_section(lines, "What changed:", task.get("what_changed"))
        _extend_section(lines, "Changed files:", task.get("changed_files"))
        _extend_section(lines, "Verification:", task.get("verification"))
        integration = task.get("integration")
        if integration:
            lines.append(f"Integration: {integration}")
        merge = task.get("merge_recommendation")
        if merge:
            lines.append(f"Merge: {merge}")
        _extend_section(lines, "Next steps:", task.get("next_steps"))
    return lines


def _extend_section(lines, heading, values):
    items = _text_items(values)
    if not items:
        return
    lines.append(heading)
    lines.extend(f"- {item}" for item in items)


def _text_items(values):
    if values is None:
        return []
    if isinstance(values, list):
        return [str(item) for item in values if item is not None and str(item)]
    if isinstance(values, tuple):
        return [str(item) for item in values if item is not None and str(item)]
    return [str(values)] if str(values) else []


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

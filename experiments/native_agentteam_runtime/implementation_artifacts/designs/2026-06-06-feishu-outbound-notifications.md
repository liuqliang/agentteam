# Feishu Outbound Notification Phase 1

## Goal

Add project-scoped Feishu notifications for important AgentTeam runtime events
without allowing Feishu to control the runtime.

The first implemented event is `manual_gate_required`, because it is the point
where the runtime has already paused a task and needs operator guidance.

## Scope

Phase 1 sends outbound notifications through a Feishu custom bot webhook.
It does not receive Feishu messages, process card callbacks, approve merges,
resume tasks, or mutate runtime state from Feishu.

The operator still answers locally through:

```bash
python3 -m agentteam_runtime.agentteam resume \
  --run-dir <run-dir> \
  --interactive \
  --question-id <question-id>
```

## Runtime Flow

```text
manual_gate_required event
  -> notification policy check
  -> project Feishu route lookup
  -> bounded text formatter
  -> Feishu custom bot webhook
  -> notification_sent or notification_failed event
```

Notification failure is operational telemetry. It must not block scheduling,
validation, integration, or manual-gate persistence.

## Configuration

The runtime reads notification configuration from local runtime inputs. Secrets
are environment variables and must not be written to taskpacks, prompts, event
payloads, stdout, or stderr.

```toml
[notifications]
enabled = true
provider = "feishu"
default_project = "agentteam"

[notifications.projects.agentteam]
webhook_env = "AGENTTEAM_FEISHU_AGENTTEAM_WEBHOOK"
signing_secret_env = "AGENTTEAM_FEISHU_AGENTTEAM_SECRET"

[notifications.policy]
events = ["manual_gate_required"]
```

Missing configuration, missing webhook environment variables, or disabled
notifications mean notification sending is skipped without failing the run.

## Message Contract

The first formatter sends a compact text message. The message may include:

- project key;
- runtime event type;
- run directory;
- task id;
- question id;
- worker question;
- bounded reason;
- local resume command.

The message must not include raw worker transcripts, patches, prompts, webhook
URLs, signing secrets, access tokens, or long logs.

## Event Contract

Phase 1 adds two telemetry events:

- `notification_sent`
- `notification_failed`

The payload records bounded route metadata:

```json
{
  "provider": "feishu",
  "project": "agentteam",
  "source_event_type": "manual_gate_required",
  "source_event_id": "EVT-12",
  "source_event_sequence": 12,
  "notification_status": "sent",
  "message_summary": "manual gate Q-TASK-001-ATTEMPT-001 for TASK-001"
}
```

Failure payloads include an error class and bounded error summary, but never
credentials or raw webhook URLs.

## Implementation Shape

- `notifications.py` owns signing, message formatting, config loading, and the
  Feishu webhook client.
- `TwoPhaseFileScheduler` accepts an optional notification sink. After it has
  appended canonical runtime events, it passes durable notification candidates
  to the sink and appends notification telemetry events returned by the sink.
- CLI wiring can be added after the in-process scheduler path is tested. Tests
  use a fake HTTP sender or fake sink and do not call the real Feishu network.

## Acceptance Criteria

- `manual_gate_required` can trigger a Feishu notification through an injected
  notifier.
- Missing or disabled notification config skips sending without failing the
  runtime.
- A send failure records `notification_failed` without blocking the manual gate.
- `notification_sent` and `notification_failed` are valid event schema types.
- Tests prove secrets and webhook URLs are not written to event payloads.

## References

- Feishu custom bot webhook and signing guide:
  https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
- Feishu message FAQ, including custom bot limitations:
  https://open.feishu.cn/document/server-docs/im-v1/faq?lang=zh-CN
- Feishu custom bot frequency governance:
  https://open.feishu.cn/document/faq/breaking-change/webhook-v2-robot-exceeds-frequency-limit-management?lang=zh-CN

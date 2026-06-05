# Feishu Notification Authorization Design

## Goal

Add a project-scoped Feishu notification path for important AgentTeam runtime
events, while keeping future Feishu-based runtime guidance behind a separate,
stronger authorization boundary.

## Decision

Use two different Feishu integration modes:

- Phase 1 uses one Feishu custom bot per project for outbound notifications.
- Phase 2 uses a Feishu app bot only if runtime guidance from Feishu is needed.

The first implementation target is Phase 1. A project maps to exactly one
custom bot. Message type does not select a different bot.

## Why This Split

Feishu custom bots are simple webhook senders. They are suitable for pushing
static notifications into one group, and do not require OpenAPI permissions.
They are not suitable for receiving commands or card interactions.

Feishu app bots can receive user messages and event callbacks after the app is
configured with the relevant event subscriptions and permissions. That makes
them the correct primitive for pause, resume, approval, and operator guidance,
but also gives them a larger security surface.

Relevant Feishu docs:

- Custom bot guide:
  https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
- Custom bot card guide:
  https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/quick-start/send-message-cards-with-custom-bot
- Message FAQ:
  https://open.feishu.cn/document/server-docs/im-v1/faq?lang=zh-CN
- Event subscription overview:
  https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?lang=zh-CN

## Phase 1: Project Notification Bot

Runtime configuration maps a project notification key to a Feishu custom bot:

```toml
[notifications]
enabled = true
provider = "feishu"
default_project = "agentteam"

[notifications.projects.agentteam]
webhook_env = "AGENTTEAM_FEISHU_AGENTTEAM_WEBHOOK"
signing_secret_env = "AGENTTEAM_FEISHU_AGENTTEAM_SECRET"

[notifications.projects.verisilicon]
webhook_env = "AGENTTEAM_FEISHU_VERISILICON_WEBHOOK"
signing_secret_env = "AGENTTEAM_FEISHU_VERISILICON_SECRET"

[notifications.policy]
events = [
  "milestone_completed",
  "integration_verified",
  "merge_completed",
  "manual_gate_required",
  "run_failed_terminal"
]
```

Secrets are local runtime inputs. The webhook URL, signing secret, app secret,
and access token must not be written into taskpacks, worker prompts, backlog
items, result payloads, or event logs.

If a custom bot signing secret is configured, the Feishu adapter signs outgoing
webhook requests using Feishu's custom-bot signing scheme. IP allowlists may be
used when the runtime has stable egress IPs.

## Notification Flow

The scheduler or submit wrapper emits notification candidates only at durable
runtime boundaries:

```text
runtime event
  -> notification policy
  -> project bot lookup
  -> bounded message formatter
  -> Feishu webhook sender
  -> notification_sent or notification_failed event
```

Notification failure must not block task execution, integration, verification,
or merge. It is recorded as operational telemetry and surfaced in observability.

Messages should be concise. They include project key, run id, milestone or
batch id when available, status, verification result, commit when available,
run directory, and the next recommended action. They should not include full
logs, raw patches, secrets, or long worker transcripts.

## Phase 2: Feishu Runtime Guidance

Runtime guidance from Feishu requires a Feishu app bot, not a custom bot.

The app bot receiver writes validated commands into a runtime command inbox:

```text
Feishu message or card callback
  -> receiver verifies Feishu request authenticity
  -> project, group, and user allowlist check
  -> command whitelist check
  -> command inbox entry
  -> scheduler consumes command
  -> operator_command or operator_guidance event
```

Allowed control commands are intentionally narrow:

- status
- pause
- resume
- approve verified merge
- reject merge with reason
- cancel run

Directional corrections are represented as operator guidance, not worker
prompts:

```json
{
  "event_type": "operator_guidance",
  "project": "verisilicon",
  "run_id": "RUN-20260605-001",
  "guidance_type": "reprioritize",
  "message": "Prioritize rtcore resource backpressure before further gap-frame tuning.",
  "scope": "next_scheduler_decision",
  "source": "feishu",
  "status": "accepted"
}
```

The scheduler may apply, defer, or reject guidance, but it must record the
decision. Feishu input never bypasses verification, write-scope checks,
integration gates, or semantic authority documents.

## Authorization Model

Phase 1 notification authorization:

- possession of the project webhook environment variable;
- optional custom-bot signing secret;
- optional Feishu custom-bot keyword or IP allowlist policy;
- local project-key-to-bot mapping.

Phase 2 runtime guidance authorization:

- Feishu app credentials stored only in local runtime secret storage or
  environment variables;
- verified Feishu callback request;
- configured project key;
- allowed group ids;
- allowed user ids;
- command whitelist;
- audit event for every accepted, rejected, or deferred command.

## Event and Observability Policy

Phase 1 adds notification telemetry events:

- `notification_sent`
- `notification_failed`

Phase 2 may add operator events:

- `operator_command_received`
- `operator_command_rejected`
- `operator_guidance`
- `operator_guidance_applied`
- `operator_guidance_deferred`

The event log stores route metadata, status, error class, and a bounded message
summary. It never stores webhook URLs, signing secrets, access tokens, or raw
request credentials.

## Non-Goals

Phase 1 does not receive Feishu messages, implement interactive card callbacks,
create a web server, or allow remote runtime control.

Phase 2 does not directly send prompts to worker agents, modify worker task
payloads in place, skip integration verification, or provide arbitrary shell
execution through Feishu.

## Acceptance Criteria

- A runtime can send an important event notification to the configured project
  bot.
- Missing notification configuration disables sending without failing the run.
- A failed webhook request records notification failure telemetry without
  stopping the runtime.
- Secrets do not appear in taskpack artifacts, events, stdout, or stderr.
- Project routing is one project key to one Feishu bot.
- Runtime guidance remains a later app-bot feature with a separate command
  authorization path.

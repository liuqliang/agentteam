# Native Runtime Model

Status: experimental model for long-lived AgentTeam role agents.

## Core Idea

AgentTeam should not equate "agent" with "Codex subagent". A native AgentTeam
agent is a durable actor:

```text
RoleAgent {
  identity
  role contract
  durable state
  inbox
  outbox
  event subscriptions
  runtime adapter
}
```

The actor may use Codex, another CLI, an API model, or a deterministic script
when it wakes up. Its continuity comes from durable state and events, not from
an always-live model context window.

## Components

| Component | Responsibility |
|---|---|
| Scheduler | Reads backlog/events/agent pool, decides which role agents should work, manages leases and retries. |
| Role Agent | Owns one bounded responsibility such as repo map, context build, risk classification, or verification. |
| Mailbox | Durable inbox/outbox messages used to wake agents and collect results. |
| Event Log | Append-only execution facts for dispatch, wake, result, validation, timeout, and integration. |
| Artifact Store | Durable files such as roadmap, backlog, current task, repo index, context pack, and progress. |
| Runtime Adapter | Boundary to Codex or another execution backend. |
| Validator Gate | Converts raw agent output into accepted or rejected structured results. |

## Role Agents

Initial M0 roles:

| Role | Trigger | Output |
|---|---|---|
| `scheduler` | new event, lease expiry, backlog change | mailbox dispatches, lease decisions, progress events |
| `repo_map_agent` | repo map missing or stale | repo index proposal or stale blocker |
| `worker_agent` | ready task with bounded write scope | compact worker result |

Later roles:

- `roadmap_agent`
- `task_slicer_agent`
- `context_builder_agent`
- `risk_classifier_agent`
- `verification_agent`
- `patch_integration_agent`
- `semantic_feedback_agent`

## Scheduling Rules

The scheduler should wake an agent only when a rule is satisfied:

```text
if backlog has ready task and no current_task:
  wake scheduler to select current_task

if current_task needs repo context and repo_index is stale:
  wake repo_map_agent

if current_task has context and bounded write_scope:
  wake worker_agent

if worker result exists:
  wake validator gate
```

Rules should be deterministic where possible. LLM calls should be used for
judgment, synthesis, and bounded implementation, not for basic queue mechanics.

## Mailbox Contract

Messages are durable JSON records. Every message has:

- `message_id`
- `from_agent`
- `to_agent`
- `message_type`
- `correlation_id`
- `created_at`
- `lease_expires_at`
- `payload`

The scheduler owns dispatch and cancellation messages. Role agents own result
messages. No role agent may silently mutate another agent's inbox.

## State And Leases

Each role agent has an `agent_state` record. The scheduler treats an agent as
available only when:

- `status` is `idle`;
- no active lease exists, or the lease has expired and recovery has run;
- the agent role is allowed for the pending task.

If a lease expires, the scheduler appends an `agent_lease_expired` event before
retrying or reassigning work.

## Runtime Adapter Boundary

The runtime adapter should hide execution backend details:

```text
AgentRuntime.run(role, message, allowed_tools, workspace_policy)
ToolRuntime.call(tool_name, args, permission_scope)
WorkspaceRuntime.exec(command, cwd, sandbox_policy)
```

Codex is one possible backend. The native runtime should not require the Codex
subagent tree to represent long-lived role agents.

## Validation Boundary

Raw role-agent output is not authority. The validator gate must check:

- output schema;
- read/write scope;
- task or message correlation id;
- verification evidence;
- risk signals;
- whether central artifacts require integration.

Only accepted results update backlog, current task, progress, repo map, or
milestone trace.

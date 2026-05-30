# Native AgentTeam Runtime Experiment

Status: isolated experiment, not current SOP authority.

This directory explores a native AgentTeam runtime where role agents are
long-lived actors managed by AgentTeam, not temporary Codex subagents.

The experiment keeps the current `design/` SOP stable. If this runtime model
works, its results can later be promoted into the design documents through the
normal artifact update path.

## Goal

Validate this model:

```text
long-lived role agent
  = stable identity
  + durable state
  + mailbox
  + event subscriptions
  + runtime adapter
```

Codex remains useful as a runtime backend for model calls, tools, MCP, sandbox,
and command execution. It should not define AgentTeam's long-term agent
lifecycle.

## Non-Goals

- Do not replace the current autonomous implementation SOP.
- Do not depend on Codex `spawn_agent` as the primary long-lived agent model.
- Do not implement distributed execution in M0.
- Do not let role agents write central authority artifacts directly.
- Do not treat natural-language agent output as validated result state.

## Directory Layout

```text
experiments/native_agentteam_runtime/
  README.md
  runtime_model.md
  m0_experiment_plan.md
  schemas/
    agent_pool.schema.json
    agent_state.schema.json
    mailbox_message.schema.json
    event.schema.json
  fixtures/
    sample_agent_pool.json
    sample_backlog.json
    sample_mailbox_message.json
    sample_events.jsonl
```

## M0 Scope

M0 validates the scheduling model with files only:

- a scheduler loop can read agent state, backlog, and events;
- role agents can be represented by durable `agent_state` records;
- mailbox messages can wake role agents without relying on chat context;
- events can reconstruct what happened;
- a Codex runtime adapter can be specified without being implemented yet.

The first M0 implementation may be a simple script or manual simulation. The
important output is whether the schemas are enough to represent scheduling,
leases, role ownership, and result validation.

## Relationship To Codex

Codex compatibility should be implemented through adapters:

```text
AgentRuntime adapter:
  spawn / send / wait / close / resume

ToolRuntime adapter:
  list tools / call tool / load MCP

WorkspaceRuntime adapter:
  exec / sandbox / apply patch / git
```

The native runtime should be able to run without Codex subagents in M0. Codex
integration can be added after the actor, mailbox, and event model is stable.

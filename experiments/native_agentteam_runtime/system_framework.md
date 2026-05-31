# Native AgentTeam System Framework

Status: draft for user confirmation.

This document describes the proposed top-level architecture for a native
AgentTeam runtime. It is a design confirmation document, not an implementation
plan.

The goal is to build a lightweight, general-purpose invocation and
communication layer around mature agent tools such as Codex, Claude Code,
Aider, OpenCode, Gemini CLI, and future agent backends.

## One Sentence Summary

AgentTeam owns scheduling, durable state, communication, permissions,
workspace isolation, validation, and artifact authority; mature coding agents
are runtime backends invoked through adapters.

## Core Design Decisions

### Scheduler Is Software, Not An LLM Agent

The top-level scheduler should be a deterministic software process. It reads
state, applies rules, assigns leases, starts runtime sessions, monitors
progress, records events, and dispatches validation.

LLM agents may help with semantic decisions, but they should not own basic
control-plane mechanics such as:

- whether a task is already leased;
- whether an agent is idle, busy, failed, or timed out;
- how many write attempts may run in parallel;
- whether a worktree should be created, retained, merged, or cleaned;
- whether an authority artifact may be updated.

### Long-Lived Logical Agents, Short-Lived Runtime Invocations

A role agent is a durable responsibility boundary:

```text
RoleAgent =
  identity
  role contract
  durable state
  mailbox subscriptions
  permission profile
  preferred runtime profile
```

A Codex, Claude Code, or other CLI process is only a runtime incarnation used to
perform one bounded attempt. The logical agent can survive process exits,
retries, model changes, and backend changes.

### One Writable Attempt, One Worktree

AgentTeam should not use "one agent, one worktree" as the default rule.

The better rule is:

```text
one writable task attempt, one worktree
```

Read-only roles can inspect the main checkout, a snapshot, or a context pack.
Writable roles receive an isolated worktree and branch for each bounded attempt.

This preserves the key benefits of existing multi-agent coding systems:

- isolated parallel edits;
- inspectable diffs;
- controlled merge/integration;
- easy retry and cleanup;
- no silent mutation of the authority checkout.

### Local Mailbox Before External Agent Protocols

M0 should use a local durable mailbox and append-only event log. MCP and A2A are
useful, but they should not be the native control plane.

Recommended boundaries:

```text
MCP  = tool and context access
A2A  = optional external agent interoperability
mail = native AgentTeam task/result communication
```

### Authority Requires Validation

Raw model output is never authoritative. A role agent can propose changes, but
only validator and integrator components may update central artifacts such as
roadmap, backlog, repo map, progress, milestone trace, or semantic design notes.

## Top-Level Architecture

```text
                         user / API / UI
                              |
                              v
                    +--------------------+
                    | AgentTeam Daemon   |
                    | deterministic core |
                    +--------------------+
                              |
          +-------------------+-------------------+
          |                   |                   |
          v                   v                   v
   +-------------+     +-------------+     +----------------+
   | Scheduler   |     | Event Log   |     | Artifact Store |
   | + leases    |     | append-only |     | authority docs |
   +-------------+     +-------------+     +----------------+
          |
          v
   +----------------+
   | Mailbox        |
   | inbox/outbox   |
   +----------------+
          |
          v
   +---------------------+
   | Runtime Adapter     |
   | Codex / Claude /... |
   +---------------------+
          |
          v
   +---------------------+
   | Runtime Session     |
   | short-lived process |
   +---------------------+
          |
          v
   +---------------------+
   | Result Validator    |
   | schema/scope/evid.  |
   +---------------------+
          |
          v
   +---------------------+
   | Integrator          |
   | merge/artifact gate |
   +---------------------+
```

## System Layers

### Control Plane

The control plane is the deterministic runtime core.

Responsibilities:

- load project configuration;
- load agent definitions;
- monitor backlog, roadmap, mailbox, events, leases, and runtime sessions;
- select ready tasks;
- classify dispatch eligibility through deterministic rules;
- call semantic role agents only when judgment is required;
- assign leases;
- create worktrees for write attempts;
- invoke runtime adapters;
- enforce concurrency limits and budget limits;
- handle timeout, retry, cancellation, and recovery.

The control plane should be small, inspectable, and testable without any LLM
call.

### Communication Plane

The communication plane consists of durable mailbox messages and events.

Mailbox messages express intent:

- dispatch this task;
- request context pack;
- request verification;
- request semantic feedback;
- cancel this attempt;
- return structured result.

Events record facts:

- task selected;
- lease acquired;
- worktree created;
- runtime session started;
- runtime output received;
- validation accepted or rejected;
- integration completed;
- timeout or recovery happened.

Messages can be retried. Events should be append-only.

### Artifact Plane

The artifact plane stores authoritative project state:

- roadmap;
- backlog;
- current task;
- repo map;
- context packs;
- progress notes;
- milestone trace;
- semantic feedback proposals;
- accepted design updates.

Role agents may propose artifact updates. They do not directly mutate authority
documents unless their role contract explicitly allows it and the validator gate
accepts the result.

### Execution Plane

The execution plane invokes mature agent tools through runtime adapters.

Each adapter normalizes:

- command construction;
- project instruction files such as `AGENTS.md` and `CLAUDE.md`;
- model/profile selection;
- MCP/tool configuration;
- workspace path;
- sandbox and permission profile;
- stdin or prompt delivery;
- structured output parsing;
- process liveness;
- stop, resume, and cleanup behavior.

Codex and Claude Code are first-class targets, but the adapter contract should
also allow Aider, OpenCode, Gemini CLI, direct API models, and deterministic
scripts.

### Workspace Plane

The workspace plane controls repository access.

Default policies:

- read-only roles do not get private worktrees unless needed;
- writable attempts always get isolated worktrees;
- each worktree has a task-attempt id and branch;
- merge into authority checkout happens only through integration;
- stale worktrees are retained or cleaned according to policy;
- role agents cannot silently edit outside their assigned write scope.

This separates role identity from execution workspace.

### Validation And Integration Plane

Validation converts raw runtime output into accepted or rejected result state.

The validator checks:

- result schema;
- task id and correlation id;
- allowed read/write scope;
- changed files;
- tests or verification evidence;
- risk level;
- whether artifact updates are proposed;
- whether manual review is required.

The integrator applies accepted results:

- merge or reject worktree diffs;
- update backlog state;
- update progress/milestone trace;
- create semantic feedback proposals;
- open authority document update tasks when needed;
- append final events.

## Core Data Objects

### Agent Definition

Static role configuration:

```text
agent_id
role
description
allowed_message_types
permission_profile
preferred_runtime_profile
allowed_tools
default_context_policy
max_parallel_attempts
```

### Agent State

Durable runtime status:

```text
agent_id
status
current_message_id
current_task_id
lease_id
last_started_at
last_completed_at
failure_count
state_summary
```

### Task

Backlog unit:

```text
task_id
title
goal
source_artifact
risk_level
required_roles
read_scope
write_scope
verification_policy
artifact_update_policy
status
```

### Attempt

Concrete execution try:

```text
attempt_id
task_id
assigned_agent_id
runtime_backend
worktree_id
branch
lease_id
status
result_ref
```

### Mailbox Message

Durable communication envelope:

```text
message_id
from_agent
to_agent
message_type
correlation_id
created_at
lease_expires_at
payload
```

### Event

Append-only fact:

```text
event_id
sequence
time
event_type
actor
target_agent_id
correlation_id
idempotency_key
payload
```

## Role Model

### Required Core Roles

| Role | Type | Responsibility |
|---|---|---|
| `scheduler` | deterministic software | Own task selection, leases, retries, runtime invocation, and recovery. |
| `repo_map_agent` | read-only semantic role | Maintain compact repository map and detect stale code understanding. |
| `context_builder_agent` | read-only semantic role | Build task-specific context packs. |
| `risk_classifier_agent` | semantic role | Classify risk and decide required validation depth. |
| `worker_agent` | writable role | Execute bounded code or file modification attempts. |
| `verification_agent` | read-mostly role | Run or design verification and interpret failures. |
| `patch_integrator` | controlled write role | Merge accepted worktree results and update task state. |
| `semantic_feedback_agent` | semantic role | Detect design gaps discovered during implementation and propose artifact updates. |
| `watchdog` | deterministic plus optional semantic role | Detect stalled sessions and trigger recovery. |

### Model Profile Principle

Different roles should not all use the same model profile.

Recommended defaults:

- cheap/fast model or deterministic code for routing, formatting, and simple
  extraction;
- stronger reasoning model for task slicing, semantic feedback, architecture
  review, and high-risk implementation;
- coding-optimized backend for bounded code modification;
- no LLM call for lease management, queue mechanics, branch creation, or basic
  process supervision.

## Main Workflows

### Boot And Recovery

```text
daemon starts
  -> load config and agent definitions
  -> replay event log or load state snapshot
  -> inspect active leases and runtime sessions
  -> mark expired leases
  -> recover or cancel orphaned attempts
  -> resume scheduling loop
```

### From Semantic Artifact To Backlog

```text
semantic docs / roadmap
  -> task_slicer_agent proposes backlog items
  -> risk_classifier_agent assigns risk and validation policy
  -> scheduler accepts ready tasks with bounded scopes
  -> backlog becomes executable state
```

### Code Modification Attempt

```text
ready task
  -> scheduler assigns worker lease
  -> workspace plane creates worktree and branch
  -> context_builder_agent creates context pack if needed
  -> runtime adapter invokes Codex / Claude / other backend
  -> worker returns structured result
  -> validator checks schema, scope, diff, evidence
  -> integrator merges or rejects result
  -> event log records outcome
```

### Semantic Feedback During Implementation

```text
worker or verifier reports design gap
  -> validator confirms it is not just local implementation noise
  -> semantic_feedback_agent writes proposal
  -> roadmap/design authority update task is opened
  -> authority document changes require separate integration
```

This protects architecture authority while still allowing implementation to
feed back new knowledge.

### Failure And Retry

```text
timeout / invalid result / test failure
  -> watchdog records failure event
  -> scheduler releases or expires lease
  -> retry policy decides:
       retry same agent
       switch runtime backend
       request context rebuild
       split task smaller
       escalate to user
```

## Reuse Strategy

### Borrow From Agent Orchestrator

- plugin boundaries;
- worktree/session lifecycle;
- runtime abstraction;
- agent adapter abstraction;
- tracker/SCM/notifier separation;
- feedback from CI and review comments.

### Borrow From Overstory

- persistent coordinator and role workers;
- durable mail;
- typed messages;
- runtime adapter details for Codex and Claude Code;
- watchdog layering;
- role-level access control;
- headless/structured event stream preference.

### Do Not Directly Clone

AgentTeam should not directly adopt AO or Overstory as its core dependency.
Their assumptions are useful but narrower than AgentTeam's goal. The native
core should stay small and generic, with source-level borrowing only after the
adapter boundaries are stable.

## Maturity Stages

### M0: File-Backed Runtime Simulation

Goal: prove the model without real multi-agent execution.

Scope:

- schemas for agent state, mailbox, events, backlog;
- deterministic scheduler loop skeleton;
- no permanent CLI agent sessions;
- optional fake runtime adapter;
- manual or scripted result injection.

### M1: Single Backend Real Execution

Goal: execute bounded tasks through one mature agent CLI.

Scope:

- one runtime adapter, likely Codex or Claude Code;
- one writable attempt per worktree;
- structured result contract;
- validator gate;
- basic watchdog and timeout handling.

### M2: Multi-Backend Execution

Goal: support multiple mature agent tools.

Scope:

- Codex and Claude Code adapters;
- backend selection by role/profile;
- context pack normalization;
- transcript parsing normalization;
- fallback from one backend to another.

### M3: Long-Running Autonomous Project Operation

Goal: let AgentTeam maintain a project over many tasks with limited user
intervention.

Scope:

- roadmap-aware backlog management;
- milestone-level trace, not per-small-task trace;
- semantic feedback into design artifacts;
- automatic task splitting;
- policy-based escalation;
- dashboard or CLI monitor.

## Design Constraints

- Keep the scheduler deterministic.
- Keep the native mailbox simple before adding external protocols.
- Keep role identity independent from runtime process identity.
- Keep worktree lifecycle tied to writable attempts, not role agents.
- Keep authority artifact updates behind validation and integration.
- Keep runtime adapters replaceable.
- Keep M0 small enough to inspect manually.

## Confirmation Questions

Before turning this into an implementation plan, these choices need explicit
confirmation:

1. Storage: start with files for M0, then move to SQLite when message/event
   concurrency matters.
2. First runtime backend: choose either Codex or Claude Code for M1.
3. Parallel write limit: default to two concurrent writable attempts.
4. UI: start with CLI/log files, postpone web dashboard.
5. Protocol bridge: keep MCP as tool/context plumbing and postpone A2A.

The current recommendation is to accept all five defaults.

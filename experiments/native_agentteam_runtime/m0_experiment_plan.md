# Native AgentTeam Runtime M0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a file-backed M0 experiment for long-lived AgentTeam role agents.

**Architecture:** M0 uses durable JSON/JSONL files to represent agent state,
mailbox messages, backlog, and events. No Codex subagent is required for M0;
Codex is treated as a future runtime adapter.

**Tech Stack:** Markdown design notes, JSON Schema, JSON fixtures, shell/JSON
validation commands.

---

### Task 1: Create Experiment Documentation

**Files:**
- Create: `experiments/native_agentteam_runtime/README.md`
- Create: `experiments/native_agentteam_runtime/runtime_model.md`

- [ ] **Step 1: Create the experiment README**

Write the purpose, non-goals, directory layout, M0 scope, and Codex relationship.

- [ ] **Step 2: Create the runtime model**

Document scheduler, role agents, mailbox, event log, artifact store, runtime
adapter, validator gate, scheduling rules, leases, and validation boundary.

- [ ] **Step 3: Verify markdown exists**

Run:

```bash
test -f experiments/native_agentteam_runtime/README.md
test -f experiments/native_agentteam_runtime/runtime_model.md
```

Expected: both commands exit 0.

### Task 2: Define Core Schemas

**Files:**
- Create: `experiments/native_agentteam_runtime/schemas/agent_pool.schema.json`
- Create: `experiments/native_agentteam_runtime/schemas/agent_state.schema.json`
- Create: `experiments/native_agentteam_runtime/schemas/mailbox_message.schema.json`
- Create: `experiments/native_agentteam_runtime/schemas/event.schema.json`

- [ ] **Step 1: Define agent state**

The schema must include stable identity, role, status, subscriptions, mailbox
paths, lease, owned artifacts, last event id, and model profile.

- [ ] **Step 2: Define agent pool**

The schema must contain a list of agent states plus scheduler metadata.

- [ ] **Step 3: Define mailbox message**

The schema must include sender, receiver, message type, correlation id, lease,
and payload.

- [ ] **Step 4: Define event**

The schema must include event id, sequence, type, actor, optional target agent,
idempotency key, correlation id, and payload.

- [ ] **Step 5: Validate JSON syntax**

Run:

```bash
jq empty experiments/native_agentteam_runtime/schemas/*.json
```

Expected: exit 0.

### Task 3: Add Fixtures

**Files:**
- Create: `experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json`
- Create: `experiments/native_agentteam_runtime/fixtures/sample_backlog.json`
- Create: `experiments/native_agentteam_runtime/fixtures/sample_mailbox_message.json`
- Create: `experiments/native_agentteam_runtime/fixtures/sample_events.jsonl`

- [ ] **Step 1: Add sample agent pool**

Represent `scheduler`, `repo_map_agent`, and `worker_agent` as durable role
agents.

- [ ] **Step 2: Add sample backlog**

Represent one ready task and one blocked task.

- [ ] **Step 3: Add sample mailbox message**

Represent a scheduler dispatch to `repo_map_agent`.

- [ ] **Step 4: Add sample events**

Represent scheduler boot, dispatch, agent wake, and result accepted events.

- [ ] **Step 5: Validate fixture JSON**

Run:

```bash
jq empty experiments/native_agentteam_runtime/fixtures/*.json
```

Expected: exit 0.

### Task 4: Review Experiment Boundary

**Files:**
- Read: `experiments/native_agentteam_runtime/README.md`
- Read: `experiments/native_agentteam_runtime/runtime_model.md`

- [ ] **Step 1: Confirm no SOP authority change**

Run:

```bash
git diff --name-only
```

Expected: changed files are only under `experiments/native_agentteam_runtime/`.

- [ ] **Step 2: Confirm M0 does not depend on Codex subagents**

Search:

```bash
rg -n "required to use Codex subagents|depends on Codex subagents" experiments/native_agentteam_runtime/README.md experiments/native_agentteam_runtime/runtime_model.md
```

Expected: no text says Codex subagents are required for M0.

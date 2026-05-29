# Autonomous Implementation Loop SOP

Status: current execution authority for long-running implementation control
after semantic artifacts are approved.

This document starts after `artifact_workflow_sop.md` has produced a semantic
contract and after an implementation pack exists or must be created. It defines
how AgentTeam keeps working without the user manually splitting every task or
issuing every command.

`implementation_workflow_sop.md` owns single-task execution details. This
document owns the control loop around those tasks: backlog generation, task
slicing, agent dispatch, map freshness, progress, recovery, and semantic
feedback.

## Core Principle

Keep state durable. Keep agents disposable.

In the current Codex runtime there is usually one main session. Other agents are
spawned as subagents. AgentTeam therefore treats the main session as the current
runtime supervisor, not as the source of truth.

Long-lived state must live in artifacts:

- implementation roadmap, when the run needs medium/long-term phase guidance
- backlog
- progress
- event log
- repo map and freshness markers
- current task
- agent dispatch/result records
- risk assessments
- semantic feedback CRs

Subagents are launched for bounded work and then discarded. Their hidden
context is never authoritative.

## Runtime Mapping

Logical AgentTeam roles map onto the current Codex execution model as follows:

| Logical component | Physical runtime |
|---|---|
| Implementation Orchestrator | Main Codex session or resumable CLI loop |
| Task Slicer | Ephemeral subagent launched from role spec |
| Repo Map Manager | Tool-driven step, optionally assisted by ephemeral subagent |
| Context Builder | Ephemeral subagent or deterministic pack builder |
| Worker Agent | Ephemeral subagent per bounded task |
| Risk Classifier Gate | Orchestrator rule step in M0/M1; optional subagent/rule engine in M2+ |
| Verification Runner | Deterministic command runner controlled by orchestrator |
| Patch Integration Agent | Orchestrator step or ephemeral integration subagent |
| Semantic Feedback Agent | Ephemeral subagent that drafts CRs only |

The system must be resumable after the main session loses context. Resumption
starts by reading `INDEX.json`, `events.jsonl`, backlog state, repo map
freshness, and the latest task/result records.

## End-To-End Loop

```text
semantic contract artifacts
  -> implementation intake
  -> implementation roadmap check
  -> backlog generation
  -> milestone selection
  -> task slicing
  -> repo map freshness check
  -> context build
  -> worker dispatch
  -> compact worker result
  -> risk classification
  -> verification
  -> patch integration
  -> map invalidation or refresh
  -> progress/event update
  -> next task or semantic feedback CR
```

The user should not be asked to split normal tasks. Human intervention is
reserved for explicit stop conditions.

## Compact Control Layout

M0/M1 should use a compact physical layout. Logical artifacts remain distinct,
but low-risk runs do not need a separate file for every logical record.

```text
output/current/implementation/
  INDEX.json
  implementation_pack.json
  project_roadmap.md        (optional, phase/milestone horizon)
  backlog.json
  repo_index.json
  current_task.json
  events.jsonl
```

Recommended mapping:

| Compact file | Contains |
|---|---|
| `INDEX.json` | Current run id, semantic artifact pointers, current pack, optional roadmap path, current task, progress summary, backlog path, latest checkpoint event, latest event offset, current risk state. |
| `implementation_pack.json` | Source layout contract, build/test contract, milestones, task slicing policy, verification strategy. |
| `project_roadmap.md` | Optional implementation-stage medium/long-term phase and milestone horizon. |
| `backlog.json` | Authoritative backlog snapshot, milestone/task status, dependencies, blockers, and in-flight attempts. |
| `repo_index.json` | Repo manifest, file inventory, unknowns, project detectors, language detector summary, dependency index, test surface, freshness markers. |
| `current_task.json` | Current milestone, current task card, context pack, verification object, write scope, stop conditions. |
| `events.jsonl` | Dispatches, worker results, risk assessments, verification summaries, patch integration records, map invalidations, progress deltas, compact traces. |

Expanded layouts from `implementation_workflow_sop.md` are still valid. M2+
may split compact files into folders when repo size, parallelism, or review
needs justify it.

## Agent Role Specs

Agent roles are persistent templates, not persistent LLM sessions.

Each role should have an `agent.md` style profile containing:

- role purpose
- default model profile
- fallback model profile
- upgrade triggers
- allowed inputs
- output schema
- read scope rules
- write scope rules
- allowed tools
- stop conditions
- risk signals to report
- evidence level expectations

The orchestrator launches subagents by combining:

```text
agent role spec
  + dispatch packet
  + task-local context
  + expected output schema
```

Subagents must not infer missing authority from previous chat history. If
needed state is not in the dispatch, context pack, repo map, or referenced
artifacts, the subagent must return `blocked`.

## Role Responsibilities

### Implementation Orchestrator

Owns the long-running loop.

Responsibilities:

- read semantic artifacts and implementation pack
- create and maintain backlog
- choose the next milestone/task
- launch task slicer, context builder, workers, reviewers, and feedback agents
- run risk classification
- invoke verification
- integrate or reject patches
- update events, progress, repo freshness, and task status
- stop only on declared human-intervention triggers

The orchestrator may dispatch agents. It does not hide decisions in chat
memory; decisions must appear in `events.jsonl` or indexed artifacts.

### Task Slicer

Turns semantic objectives into milestones and bounded task cards.

A worker task must have:

- one clear objective
- bounded read scope
- bounded write scope
- verification method
- expected evidence level target
- rollback or retry route
- stop conditions

Task slicing should prefer L0/L1 worker tasks even when the milestone is L2/L3.
The risk of the milestone remains at the orchestration layer.

### Repo Map Manager

Maintains repo navigation facts and freshness state.

Responsibilities:

- keep file inventory and manifest facts current enough for context selection
- mark stale files or modules after every patch
- refresh only the slices needed by the next task
- avoid full reindexing for L0/L1 unless stale data is used as edit rationale

The repo map is a navigation cache, not semantic truth.

### Context Builder

Builds the smallest useful task context.

Inputs:

- current task
- repo map
- semantic references
- verification object
- stale markers

Output:

- task-local context
- exact files the worker should read
- allowed write scope
- missing context blockers, if any

### Worker Agent

Implements one bounded task.

Worker output is intentionally compact:

```json
{
  "task_id": "TASK-042",
  "result_status": "completed | blocked | failed | cancelled",
  "changed_files": ["paths"],
  "key_files_read": ["paths"],
  "verification_results": [],
  "risk_signals": [],
  "design_findings": [],
  "assumptions": [],
  "structure_docs": ["paths only when explicitly included in write_scope"],
  "next_action": "integrate_patch | revise_task | create_design_cr | stop"
}
```

Workers do not:

- maintain global backlog
- decide final risk level
- update semantic contract artifacts
- launch authority-writing agents
- write milestone traces for normal L0/L1 tasks

If a worker writes an implementation structure document, that path must be
explicitly present in the task or dispatch `write_scope`. Otherwise the result
is scope-invalid.

### Risk Classifier Gate

Assigns the formal evidence level from task, diff, result, and verification
status.

Worker self-assessment is an input. It cannot lower a rule-triggered level.

### Verification Runner

Runs deterministic checks from verification objects.

It records command, cwd, exit code, output summary, and whether the result
covers the current acceptance criterion.

### Patch Integration Agent

Serially integrates patches after risk and verification gates pass.

It may reject or rebase patches. It does not update semantic contract artifacts
directly.

### Semantic Feedback Agent

Classifies implementation-originated design gaps and drafts CRs.

It produces proposals only. The artifact workflow Integration Agent remains the
only writer of authoritative semantic artifacts.

## Risk And Evidence Model

Risk has two layers:

| Layer | Meaning | Owner |
|---|---|---|
| Worker task risk | Risk of one bounded code edit. Prefer L0/L1. | Risk Classifier Gate |
| Milestone risk | Risk of the whole change set or semantic objective. May be L2/L3. | Implementation Orchestrator |

L2/L3 milestones should be decomposed into L0/L1 worker tasks whenever
possible. Splitting tasks reduces worker context pressure; it does not erase
milestone risk.

Evidence ownership:

| Level | Worker output | Orchestrator output |
|---|---|---|
| L0 | changed files, diff summary, verification result, next action | event entry and map invalidation |
| L1 | L0 plus key files read and assumptions | risk assessment and progress update |
| L2 | compact task result | milestone trace, patch/orchestrator review, context/task hashes |
| L3 | compact task result and design findings if any | design CR path, semantic trace, review, serial integration gate |

For L0/L1, the worker result and its `events.jsonl` entries are the trace. Do
not create a separate `IMPL-TRACE-*` or milestone-trace artifact for normal
L0/L1 tasks unless a higher-risk rule is triggered. For L2/L3, the orchestrator
generates milestone or semantic traces from the event log and referenced
artifacts.

Low-risk workers should spend time changing code and running verification, not
writing audit logs.

## Roadmap, Milestones, And Backlog

The implementation roadmap is optional. Create it only when the implementation
run needs medium/long-term guidance beyond the current milestone.

The roadmap belongs to the implementation stage, not the semantic-design stage.
It is `implementation_authority`: it guides phase sequencing and gate selection
for implementation, but it cannot define, edit, or override semantic contracts.

Layering:

| Layer | Artifact | Owns |
|---|---|---|
| Medium/long-term route | `project_roadmap.md` | Implementation phases, deferred work, pause conditions, next milestone direction. |
| Stage goal | milestone record in `implementation_pack.json` or `backlog.json` | One coherent capability and its exit criteria. |
| Task queue | `backlog.json` | Executable tasks, dependencies, blockers, and scheduling state. |
| Current execution | `current_task.json` | The selected bounded task and its task-local context. |
| Execution facts | `events.jsonl` | What actually happened: dispatch, result, verification, integration, invalidation, progress. |

Relationship:

```text
semantic contract
  -> implementation_pack
  -> optional project_roadmap
  -> milestone
  -> backlog
  -> current_task
  -> events
```

The roadmap answers "what direction should implementation take over the next
few phases?" A milestone answers "what capability is this phase trying to
complete?" Backlog answers "which bounded tasks are ready, blocked, done, or
cancelled?" Events answer "what actually happened?"

### Roadmap Creation And Update Rules

Create or update `project_roadmap.md` only when one of these is true:

- implementation needs more than the current milestone to stay oriented;
- a milestone closes and the next phase/gate should be restated;
- the implementation route changes;
- a probe or verification result invalidates the current route;
- cross-phase parallel exploration or independent review begins;
- pause conditions or explicitly deferred work change.

Do not update the roadmap for:

- ordinary L0/L1 local tasks;
- small probes or logging changes that do not change phase sequencing;
- compact event, backlog, or progress bookkeeping;
- evidence generation that only confirms an existing gate;
- proof-of-work narration.

When the roadmap changes, record a compact `events.jsonl` entry. If the change
closes or reopens a milestone, include the update in the milestone trace.
Ordinary L0/L1 task events do not update the roadmap.

Before launching large parallel exploration, independent review, or
cross-phase implementation, the orchestrator should read the roadmap and copy
the relevant phase boundaries into the subagent dispatch.

## Backlog And Progress

Backlog items are generated from semantic objectives and implementation pack
milestones.

`backlog.json` is the authoritative compact backlog snapshot. It is rebuilt from
`events.jsonl` only when the snapshot is missing or fails validation. In
expanded layouts, the same logical records may be split into backlog/progress
folders, but `INDEX.json` must still point to the current backlog snapshot,
progress summary, latest checkpoint event, and latest event offset.

The status vocabulary is deliberately small:

| Record | Status field | Allowed values |
|---|---|---|
| Backlog item | `backlog_status` | `ready`, `running`, `blocked`, `done`, `cancelled`, `rebase_required` |
| Task attempt | `attempt_status` | `created`, `dispatched`, `running`, `completed`, `failed`, `cancelled`, `timed_out` |
| Patch integration | `integration_status` | `not_integrated`, `integrated`, `rejected`, `rebase_required` |

Backlog `done` means the required patch, documentation update, or no-op
decision has passed its verification and integration gate. `integrated` is an
integration status, not a backlog item status.

Minimum backlog item:

```json
{
  "id": "BL-001",
  "semantic_refs": ["artifact ids"],
  "milestone_id": "MILESTONE-001",
  "backlog_status": "ready | running | blocked | done | cancelled | rebase_required",
  "risk_target": "L1 | L2 | L3",
  "depends_on": [],
  "next_task_id": "TASK-001 or null",
  "attempt_id": "ATTEMPT-001 or null",
  "attempt_status": "created | dispatched | running | completed | failed | cancelled | timed_out | null",
  "integration_status": "not_integrated | integrated | rejected | rebase_required",
  "read_closure": ["paths or context ids"],
  "write_scope": ["paths"],
  "blockers": []
}
```

Minimum progress summary in `INDEX.json`:

```json
{
  "progress": {
    "total_backlog_items": 42,
    "ready": 10,
    "running": 1,
    "blocked": 2,
    "done": 29,
    "cancelled": 0,
    "rebase_required": 0,
    "current_milestone_id": "MILESTONE-001 or null",
    "current_task_id": "TASK-001 or null",
    "current_attempt_id": "ATTEMPT-001 or null",
    "last_event_id": "EVT-0100",
    "last_event_sequence": 100,
    "last_event_offset": 12345,
    "last_checkpoint_id": "CHK-001",
    "verification_state": "passing | failing | skipped | unknown",
    "blocked_reasons": []
  }
}
```

Progress is derived from backlog and events. On resume, recompute it before
selecting the next task and persist a `progress_updated` event if the stored
summary was stale.

Backlog and task status are advanced by events, not by chat memory.

Backlog status transitions:

```text
ready -> running -> done
ready -> running -> blocked
running -> ready
blocked -> ready
running -> cancelled
running -> rebase_required
rebase_required -> ready
rebase_required -> cancelled
```

`running -> ready` is allowed only when an attempt has failed or timed out and
the retry budget permits a fresh attempt. It must be accompanied by a terminal
attempt event.

Attempt status transitions:

```text
created -> dispatched -> running -> completed
created -> dispatched -> completed
created -> dispatched -> running -> failed
created -> dispatched -> failed
created -> dispatched -> running -> cancelled
created -> dispatched -> cancelled
created -> dispatched -> timed_out
created -> cancelled
```

`running` is recorded only when the platform can emit an `attempt_started`
event. If a platform starts work immediately after dispatch and provides no
separate start signal, a `worker_result` may legally close an attempt from
`dispatched`.

`failed` and `timed_out` are terminal for the attempt. A retry creates a new
`attempt_id` instead of reopening the old attempt.

Integration status transitions:

```text
not_integrated -> integrated
not_integrated -> rejected
not_integrated -> rebase_required
rebase_required -> not_integrated
rebase_required -> rejected
```

`integrated` is terminal for that patch result. Follow-up changes create a new
task or attempt.

Status mapping across task records:

| Source status | Attempt status | Backlog status | Integration status |
|---|---|---|---|
| task card `ready` | `created` or null | `ready` | `not_integrated` |
| task card `running` | `running` | `running` | `not_integrated` |
| worker result `completed` | `completed` | `running` until integration gate passes, then `done` | `not_integrated` until `patch_integrated`, then `integrated` |
| worker result `blocked` | `failed` | `blocked` | `not_integrated` |
| worker result `failed` | `failed` | `ready` if retry budget remains, otherwise `blocked` | `not_integrated` |
| worker result `cancelled` | `cancelled` | `cancelled` or `ready` if retry policy explicitly creates a new attempt | `not_integrated` |
| integration `rebase_required` | terminal attempt remains unchanged | `rebase_required` | `rebase_required` |

Implementation task-card status is local execution state. Backlog status is the
global scheduler state. Attempt status is immutable after it reaches a terminal
value.

Unknown statuses are invalid. A resume pass must route them to `blocked` with a
diagnostic event rather than guessing.

## Event Log

`events.jsonl` is the durable execution spine for the autonomous loop.

Minimum event shape:

```json
{
  "run_id": "RUN-20260528-001",
  "event_id": "EVT-0001",
  "sequence": 1,
  "offset": 12345,
  "checkpoint_id": "CHK-0000 or null",
  "time": "ISO-8601",
  "type": "task_created | dispatch_created | attempt_started | dispatch_timed_out | worker_result | risk_assessed | verification_result | patch_integrated | patch_rejected | map_invalidated | attempt_cancelled | attempt_retry_scheduled | orphan_patch_adopted | orphan_patch_discarded | task_rebased | manual_gate_required | design_finding_routed | progress_updated | event_log_recovered | checkpoint",
  "actor": "orchestrator | subagent id | tool id",
  "task_id": "TASK-001 or null",
  "milestone_id": "MILESTONE-001 or null",
  "attempt_id": "ATTEMPT-001 or null",
  "idempotency_key": "stable key for retry-safe replay",
  "event_status": "created | accepted | rejected | applied | blocked | terminal",
  "workspace_snapshot": "git sha, diff id, or null",
  "lease": {
    "lease_id": "LEASE-001 or null",
    "expires_at": "ISO-8601 or null"
  },
  "payload": {},
  "derived_from": ["event ids or artifact ids"]
}
```

For L0/L1, events are the trace. For L2/L3, events feed milestone or semantic
traces.

Offset semantics:

- `offset` is the byte offset at the start of this event line in
  `events.jsonl`;
- checkpoint `last_event_offset` is the byte offset immediately after the
  newline of the last fully parsed and applied event;
- replay starts at checkpoint `last_event_offset` and then applies complete
  JSONL records in sequence order;
- a partial final line or corrupt JSON object after the last complete event is
  ignored for replay and must be followed by an `event_log_recovered` event
  before normal dispatch continues.

Event state effects:

| Event type | Attempt effect | Backlog effect | Integration effect |
|---|---|---|---|
| `task_created` | none | create or update item as `ready` | `not_integrated` |
| `dispatch_created` | create `attempt_id` as `dispatched` and start lease | set item `running` | `not_integrated` |
| `attempt_started` | set attempt `running` | keep item `running` | unchanged |
| `worker_result` with `result_status=completed` | set attempt `completed` | keep item `running` until verification/integration | `not_integrated` |
| `worker_result` with `result_status=blocked` | set attempt `failed` | set item `blocked` | `not_integrated` |
| `worker_result` with `result_status=failed` | set attempt `failed` | set item `ready` if retry budget remains, else `blocked` | `not_integrated` |
| `worker_result` with `result_status=cancelled` | set attempt `cancelled` | set item `cancelled` unless retry event follows | `not_integrated` |
| `dispatch_timed_out` | set attempt `timed_out` | keep item `running` until a routing event follows | unchanged |
| `attempt_cancelled` | set attempt `cancelled` | set item `cancelled`, `ready`, or `rebase_required` as declared in payload | unchanged |
| `attempt_retry_scheduled` | create new `attempt_id` as `created` | set item `ready` | `not_integrated` |
| `orphan_patch_adopted` | create or mark adopted attempt `completed` | keep item `running` until verification/integration | `not_integrated` |
| `orphan_patch_discarded` | close or detach orphan attempt in payload | set item `ready`, `blocked`, or `cancelled` as declared in payload | unchanged |
| `verification_result` passed | no status change by itself | keep item `running` | `not_integrated` |
| `verification_result` failed | keep terminal attempt unchanged | set item `blocked` unless retry policy follows | `not_integrated` |
| `patch_integrated` | keep terminal attempt unchanged | set item `done` | `integrated` |
| `patch_rejected` | keep terminal attempt unchanged | set item `blocked`, `ready`, or `rebase_required` as declared in payload | `rejected` |
| `task_rebased` | old terminal attempt remains unchanged; new attempt not created yet | set item `ready` with new context/task hashes | `rebase_required -> not_integrated` |
| `manual_gate_required` | keep attempt unchanged | set item `blocked` | unchanged |

Any event whose payload declares a resulting status must match this table.
Otherwise replay must stop and route the item to `blocked` with a diagnostic
event.

Checkpoint event:

```json
{
  "type": "checkpoint",
  "sequence": 100,
  "payload": {
    "backlog_hash": "sha256:<hash>",
    "repo_index_hash": "sha256:<hash>",
    "current_task_hash": "sha256:<hash>",
    "workspace_snapshot": "git sha or diff id",
    "last_integrated_event": "EVT-0098",
    "last_event_offset": 12345
  }
}
```

Replay rules:

- events are applied in increasing `sequence` order;
- duplicate `idempotency_key` values are ignored after the first successful
  application;
- event `offset` values must be monotonic and match the physical JSONL file
  position for crash recovery;
- every dispatch creates an `attempt_id`;
- every dispatch has a lease; an expired lease with no terminal event produces
  `dispatch_timed_out`;
- an in-flight attempt with no terminal event is routed by policy to one of
  `cancel_attempt`, `retry_attempt`, `adopt_orphan_patch`, `rebase_task`, or
  `manual_gate`;
- every recovery decision must be recorded by one standard terminal or routing
  event: `attempt_cancelled`, `attempt_retry_scheduled`,
  `orphan_patch_adopted`, `orphan_patch_discarded`, `task_rebased`, or
  `manual_gate_required`;
- if there is no worker result and no workspace diff, cancel the attempt and
  retry within the retry budget;
- if workspace diff exists entirely inside the attempt `write_scope`, run risk
  classification and verification before adopting it as an orphan patch;
- if workspace diff touches outside `write_scope`, generated outputs are
  ambiguous, or base/context hashes no longer match, block the backlog item and
  require rebase or manual gate;
- workspace diff without a matching integration event blocks autonomous
  continuation until it is adopted by policy, integrated, or explicitly
  discarded by an approved cleanup action recorded as `orphan_patch_discarded`;
- checkpoint hashes must match reconstructed snapshots before the loop resumes
  from that checkpoint.

## Map Freshness Policy

Every integrated code change must produce map freshness information.

`repo_index.json` must keep enough reverse pointers to answer:

- which context packs read this file or module;
- which task cards include it in `read_closure` or `write_scope`;
- which dispatches and in-flight attempts were built from those task cards;
- which repo-index slices or language-pack records contain stale facts.

Minimum map invalidation:

```json
{
  "changed_files": ["paths"],
  "map_invalidation": {
    "files": ["paths"],
    "affected_modules": ["module ids if known"],
    "dependency_index_hits": ["module ids, symbols, or unknown"],
    "read_closure_hits": ["task ids whose read_closure touched changed files"],
    "write_scope_hits": ["task ids whose write_scope touched changed files"],
    "invalidates_context_packs": ["context ids or paths"],
    "invalidates_task_cards": ["task ids"],
    "invalidates_dispatches": ["dispatch ids"],
    "pending_attempts_action": [
      {
        "attempt_id": "ATTEMPT-001",
        "action": "keep | cancel | rebase | manual_gate",
        "reason": "why this attempt is or is not stale"
      }
    ],
    "needs_reindex": false,
    "reason": "changed file content"
  }
}
```

Rules:

- L0/L1: invalidate changed files and affected modules; do not rebuild unless
  the next task needs stale slices.
- If `dependency_index_hits` is `unknown`, conservatively mark dependent
  ready tasks as `rebase_required` and route in-flight attempts to `rebase` or
  `manual_gate` unless the context builder proves the changed files are outside
  their read/write closure.
- Before selecting a ready task, check its `read_closure`, `write_scope`,
  context pack, task card, and in-flight dispatch against the latest
  invalidation records. Stale ready tasks move to `rebase_required` before
  dispatch.
- L2: refresh affected repo index slices, context packs, and task cards before
  dependent work continues.
- L3: invalidate or rebase affected implementation pack, task cards, context
  packs, verification objects, and semantic references after CR integration.

## Semantic Feedback Loop

When implementation discovers a semantic gap:

```text
worker_result.design_findings
  -> orchestrator classification
  -> semantic feedback agent
  -> CR draft
  -> artifact workflow integration gate
  -> affected implementation records invalidated or rebased
```

The worker can report. The orchestrator can route. The semantic Integration
Agent writes authority.

## Human Intervention Triggers

The autonomous loop should continue without user input unless one of these
conditions occurs:

- L3 semantic change requires explicit approval by policy
- required permissions, network, dependency installation, or destructive action
  are unavailable
- repeated verification failure exhausts retry budget
- acceptance criteria conflict with repository reality
- security, data migration, or external protocol risk cannot be bounded
- semantic CR is blocked by missing product decision
- the backlog has no ready task and no safe recovery route

## Resume Procedure

On resume:

1. Read `output/current/INDEX.json`.
2. Read implementation `INDEX.json` or compact layout `INDEX.json`.
3. Load `backlog.json` and verify its hash against the latest checkpoint.
4. Replay `events.jsonl` from the last known valid checkpoint.
5. Recompute current backlog, current task, map freshness, in-flight attempts,
   progress summary, and risk state.
6. Check workspace diff against the latest integration event.
7. Route in-flight attempts using the attempt policy.
8. Persist corrected progress if the stored summary was stale.
9. Continue from the next ready task or route the detected inconsistency.

## Acceptance Criteria

The autonomous implementation loop is acceptable only if:

- semantic artifacts are the only source of semantic truth;
- backlog and task status can be reconstructed from durable artifacts;
- worker tasks are bounded and normally L0/L1;
- milestone L2/L3 risk is owned by the orchestrator, not the worker;
- subagents are launched from role specs and dispatch packets;
- subagent hidden context is never authority;
- ordinary worker results are compact;
- map invalidation exists for every integrated patch;
- design findings route through CRs before semantic authority changes;
- the loop can pause and resume without losing state.

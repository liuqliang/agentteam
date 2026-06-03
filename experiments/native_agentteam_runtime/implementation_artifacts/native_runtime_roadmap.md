# Native Runtime Long-Term Roadmap

Status: active implementation roadmap for the native AgentTeam runtime
experiment.

This document records the medium and long-term direction for the native
runtime implementation after M22. It is not a semantic architecture authority
document and it is not a per-milestone implementation plan. Its job is to keep
the implementation sequence coherent across multiple milestones.

## Artifact Role

The roadmap answers:

- what capability layer should be built next;
- what milestone-level acceptance means;
- which design risks require an explicit decision before implementation;
- which tempting features are intentionally outside the current route.

The roadmap does not replace:

- `system_framework.md`, which explains the top-level system architecture;
- `runtime_model.md`, which explains the runtime actor and message model;
- `implementation_artifacts/m0_file_runtime.md`, which records implemented
  runtime behavior;
- milestone design and plan files under `implementation_artifacts/designs/` and
  `implementation_artifacts/plans/`.

Milestone plans may refine the details of a roadmap item. If implementation
evidence shows that the roadmap is stale, the controller or integrator should
update this file after the milestone result is validated. A worker agent may
emit a roadmap feedback proposal, but it should not directly mutate this
roadmap as part of ordinary code work.

## Current Baseline

The implementation has already proven these layers:

1. File-backed scheduler state, mailbox dispatch, event replay, and SQLite
   state index.
2. Runtime adapters for fake workers, shell commands, and Codex CLI execution.
3. Attempt-scoped worktrees, diff audit, patch capture, integration worktree
   apply, verification, and opt-in integration commit checkpoints.
4. Scheduler and daemon loops, worker registry, one-shot mailbox subprocess
   workers, and long-running mailbox worker processes.
5. Static worker pools and two-phase dispatch/collect scheduling with retry,
   timeout, and bounded inflight execution.
6. Integration gate separation: task result acceptance, patch integration,
   integration verification, and integration commit remain distinct gates.
7. Worker health supervision with restart accounting and failed-worker state.
8. Planner-generated backlog proposals for decomposition tasks.
9. Bounded planner context packages that expose current state, available roles,
   allowed scopes, and proposal contract hints without dumping the full repo.
10. Codex planner prompt contract, no-worktree fallback execution, fallback
    dirty-check rejection, and fake Codex planner worker-pool coverage.
11. Selected semantic artifact context ingestion with digest, timestamp,
    heading, bounded excerpt, and warning metadata.
12. Proposal quality gate for self-dependencies, generated dependency cycles,
    risk-target enforcement, L0/L1 scope size limits, L2 review blocking, and
    inspectable decomposition rejection details.
13. Rolling milestone decomposition waves with generated task lineage,
    milestone decomposition state, max-wave terminal status, and default
    single-wave compatibility.
14. File-backed worker-pool resume from process registry with attached PID
    health and stop-file shutdown.
15. Durable accepted-patch integration queue with `pending`, `applied`,
    `verified`, `blocked`, and `committed` states, plus replay visibility.
16. Batch integration verification over queued patch sets in a dedicated batch
    worktree, with persisted batch results.
17. Verified batch fast-forward merge back to the source branch, with source
    cleanliness, batch commit, and `--ff-only` safety gates.

This means the experiment is no longer only a file-format prototype. It is now a
small local multi-process runtime with a deterministic scheduler, durable
communication files, scoped worker execution, proposal validation, and Codex as
an optional backend.

## Roadmap Principles

- The scheduler remains deterministic software. LLM agents propose work and
  results; they do not own leases, retries, artifact authority, or merge policy.
- Context packages are bounded. The planner receives selected state, role,
  scope, and artifact summaries instead of the full repository.
- Each writable attempt owns one isolated worktree. Long-lived role identity is
  independent from short-lived runtime processes.
- Planner output is a proposal, not authority. The scheduler validates role,
  scope, shape, dependency, and risk constraints before adding tasks to the
  backlog.
- Live model calls are opt-in smoke coverage. Local tests must remain
  deterministic with fake or shell adapters.
- A milestone should deliver one independently verifiable runtime capability.

## Near-Term Route

### M23: Codex Planner Prompt Contract

Status: implemented.

Goal: make the real Codex runtime able to produce the same structured
`task_proposal` shape that the fake planner currently returns.

Scope:

- add a planner-specific prompt path for `task_kind == "decompose_backlog"`;
- include the planner context file path and result schema in the prompt;
- require Codex to write one JSON proposal through the existing
  `--output-last-message` contract;
- keep fake Codex command coverage for deterministic tests;
- add a gated live smoke that can be enabled for one real planner call;
- preserve scheduler-side role and write-scope enforcement.

Acceptance:

- fake planner, fake Codex planner, and scheduler loop tests pass;
- invalid Codex planner output is rejected without mutating backlog authority;
- a live planner smoke is skipped unless the live gate is enabled;
- when enabled, the live smoke produces at least one accepted bounded worker
  task inside the allowed scope.

### M24: Semantic Artifact Context Ingestion

Status: implemented.

Goal: let decomposition use selected design and implementation artifacts without
placing large documents or source files directly into the model context.

Scope:

- define a small allowlist of source artifacts for planner context;
- summarize roadmap, architecture, backlog, and milestone state into compact
  context sections;
- include source path, source digest, timestamp, and excerpt budget metadata;
- keep source-code indexing out of the planner context unless a later milestone
  adds a dedicated repo map source.

Acceptance:

- planner context includes compact artifact summaries with source provenance;
- context size is bounded by explicit per-section limits;
- stale or missing artifacts produce clear context warnings instead of silent
  hallucinated state;
- tests verify that full document bodies are not embedded by default.

### M25: Proposal Quality Gate

Status: implemented.

Goal: reject bad automatic task splits before they become executable backlog
state.

Scope:

- enforce task size and risk rules for generated L0/L1/L2 work;
- reject duplicate task ids, self-dependencies, dependency cycles, and
  impossible role or scope combinations;
- require high-risk generated tasks to route through review before execution;
- record compact rejection reasons in events and scheduler state.

Acceptance:

- malformed, cyclic, duplicate, over-broad, and out-of-policy proposals are
  rejected deterministically;
- accepted proposals contain enough fields for execution without asking the
  user to manually split the task;
- rejection events are inspectable from the state index.

### M26: Rolling Milestone Decomposition

Status: implemented.

Goal: turn automatic decomposition from a one-shot generated task into a
milestone-level loop.

Scope:

- generate a bounded batch of executable tasks for the current milestone;
- mark milestone decomposition status separately from worker task status;
- prevent infinite decomposition loops;
- update backlog state after a milestone completes;
- open the next decomposition task only when evidence shows the current batch
  is done, blocked, or insufficient.

Acceptance:

- a scheduler run can decompose, execute, collect, and advance one milestone
  without manual task injection;
- generated tasks remain bounded and executable;
- completed milestone state records the proposal source, accepted task ids, and
  validation outcome;
- the scheduler does not generate repeated duplicate decomposition tasks.

## Mid-Term Route

### M27: Persistent Runtime Process Model

Status: implemented.

Goal: make resident role agents feel like durable workers rather than short
experiments launched only for a CLI run.

Scope:

- define process lifecycle state for long-running workers;
- support resume after scheduler restart;
- separate worker process health from logical agent availability;
- make mailbox consumption idempotent across restarts.

Decision gate: choose whether the first persistent supervisor is still file
based or moves worker/session state into SQLite.

### M28: Worktree Isolation And Integration Queue

Goal: make parallel writable work practical for real repositories.

Status: implemented with feature-level verified batch merge.

Scope:

- create one worktree per writable attempt;
- retain rejected worktrees for inspection;
- queue accepted patches for integration;
- verify an integrated batch before merge;
- require the whole task or feature slice to pass before merging to the main
  branch.

Decision: use feature-level verified batch merge. Task-level integration commits
remain checkpoints for audit and debugging, while final source-branch delivery
is gated by batch verification and fast-forward merge.

### M29: Health-Driven Reassignment

Goal: let the scheduler react to unhealthy workers without user intervention.

Scope:

- restart failed worker processes within policy;
- quarantine repeatedly failing runtime profiles;
- reassign eligible tasks to another compatible role or backend;
- preserve the original attempt lineage.

Decision gate: define the maximum automatic retry and reassignment budget for a
single task before escalation.

### M30: Runtime Observability

Goal: make long-running operation inspectable without reading raw JSONL files.

Scope:

- add CLI views for backlog, leases, workers, events, sessions, and integration
  queue;
- expose latest failure reasons and blocked tasks;
- show current roadmap milestone and next scheduled decomposition;
- keep the underlying event log as the source of truth.

Decision gate: decide whether the first monitor remains CLI-only or adds a
minimal local dashboard.

## Longer-Term Route

These items should wait until M23-M30 have made the local runtime reliable:

- Claude Code adapter compatibility after a stable result extraction contract
  exists.
- MCP tool and context compatibility as adapter capabilities, not as the native
  control plane.
- Cross-model role routing where planner, implementer, reviewer, and integrator
  can use different model profiles.
- A stronger durable store if file locking and JSONL replay become insufficient
  for long project runs.
- Policy-governed semantic feedback where implementation evidence can propose
  updates to design authority artifacts without letting ordinary workers edit
  those artifacts directly.
- Repository map integration using language-aware tools such as compilers, LSP,
  build systems, and static analyzers, with compact summaries fed to planners.

## Explicit Non-Goals For The Current Route

- No full-repository JSON dump as planner context.
- No unrestricted planner writes to backlog, roadmap, design, or source code.
- No automatic edits to semantic authority artifacts by implementation workers.
- No live-model call requirement in normal unit tests or CI.
- No distributed multi-host orchestration before the local runtime is stable.
- No A2A dependency for the native control plane.

## Update Policy

Update this roadmap when one of these events occurs:

- a milestone changes what the next milestone should be;
- a validation result exposes a missing capability or invalid assumption;
- the user makes an explicit product or architecture decision;
- implementation evidence shows that a listed milestone should be split,
  reordered, or removed.

Do not update this roadmap for ordinary local implementation details that are
already captured in milestone plans, events, or test output.

The next recommended milestone is M29: Health-Driven Reassignment.

# Post-M67 Cost Attribution And Long-Run Validation Route

Status: implementation-stage route note for measurement, reliability, and
long-running validation. It is subordinate to the operator-approved research
positioning authority in
[`agentteam_research_positioning.md`](../../research/agentteam_research_positioning.md).

This note records the next engineering route after the risk-aware repo-map
handoff work. It does not change semantic authority. It turns the current
uncertainty around token consumption and long-running value into a bounded,
measurable implementation program.

## Decision Summary

Do not expand AgentTeam with more model adapters, roles, dashboards, protocols,
or artifact types before its net benefit is measurable.

The next route is:

```text
complete model-usage attribution
  -> calibrate collection on short tasks
  -> compare single-Codex and AgentTeam paths on medium tasks
  -> validate crash recovery
  -> run one real long-running task
  -> continue, narrow, or pivot based on evidence
```

The long-running task is the final experiment, not the first measurement step.

## Repository Understanding Summary

The native runtime already has a substantial local control plane:

- deterministic scheduling, leases, retries, and durable events;
- taskpack authoring, validation, freezing, and execution;
- long-lived logical roles backed by short-lived runtime invocations;
- attempt-scoped worktrees and verified integration baselines;
- bounded repo context, role context, and repo-map handoff reuse;
- L0/L1/L2/L3 risk and evidence routing;
- bounded `pursue` rounds and follow-up queues;
- operator reports, Feishu notifications, release pinning, and a rebuildable
  SQLite projection.

The remaining uncertainty is no longer whether these mechanisms can be
implemented. The uncertainty is whether they improve verified long-running
work enough to justify their token, latency, artifact, and maintenance cost.

## Current Measurement Evidence

A read-only scan on 2026-07-11 of the existing `verisilicon` work root reported:

```text
runs: 5
worker_results: 4
reported_attempt_count: 0
unreported_attempt_count: 4
token_usage_status: unavailable
```

The current runtime can parse provider usage from Codex JSONL worker output and
aggregate usage at run/project level. It does not yet provide complete usage
attribution for taskpack authoring, planning, repo mapping, implementation,
review/repair, or follow-up authoring. Existing taskpack-author diagnostics
record context byte and estimated-token inputs, but those estimates are not
provider-reported usage.

M67 calibration also showed that live taskpack authoring can take roughly
283-287 seconds even when it eventually produces a valid taskpack. Without
stage-level token attribution, that latency cannot be separated from worker,
repo-context, retry, or follow-up cost.

## Authority And Storage Decision

Model usage should follow the existing hybrid storage rule:

- append a compact, schema-versioned usage event or session result to the
  authoritative run artifacts;
- project the usage records into `agentteam.db` for queries and aggregation;
- keep the database rebuildable;
- do not create one prose trace document per model call;
- do not make context-size estimates interchangeable with provider-reported
  token usage.

One model invocation should add one compact structured record. Deterministic
scheduler, validation, Git, and integration operations should record wall time
and status but `token_usage_status: not_applicable`.

## Model Invocation Usage Contract

Each supported model invocation should be attributable through a record with
at least these fields:

```text
usage_schema_version
usage_event_id
project
run_id
pursue_id
round_index
taskpack_id
task_id
attempt_id
runtime_session_id
agent_id
role
usage_stage
backend
model
input_tokens
cached_input_tokens
output_tokens
reasoning_tokens
total_tokens
usage_source
usage_status
unavailable_reason
started_at
finished_at
wall_time_seconds
```

Initial `usage_stage` values:

```text
taskpack_author
planner_or_task_slicer
repo_map
implementation_worker
review_or_repair
follow_up_author
semantic_architecture
```

Counting rules:

- provider-reported usage is preferred over estimates;
- cached input remains separate from non-cached input;
- repeated cumulative JSONL snapshots are counted once per invocation, not once
  per event line;
- when a provider reports session-cumulative counters across resumed
  invocations, each later invocation is counted only from a validated
  monotonic delta against the prior authoritative session snapshot;
- retry attempts receive distinct attempt/session identifiers;
- missing provider usage remains explicitly unavailable and contributes to
  coverage gaps;
- estimated context bytes/tokens may be recorded as diagnostics but must not be
  added to provider token totals;
- aggregate totals must be reproducible by replaying authoritative usage
  records.

## Ordered Engineering Route

The implementation-ready Phase 1 taskpack for Tasks 1 and 2 is:

[`2026-07-23-phase1-model-invocation-usage.md`](2026-07-23-phase1-model-invocation-usage.md).

It freezes invocation identity, token counting, authority, stage routing,
projection, verification, and integration gates. Execute its Phase 1A capture
work before Phase 1B projection work, and complete the whole taskpack before
starting Task 3 below.

### Task 1: Complete Usage Capture

Objective: capture real usage for every Codex-backed model invocation path.

Scope:

- define the usage schema and stage vocabulary;
- reuse the existing JSONL parser for worker sessions;
- add real taskpack-author usage extraction;
- carry run, round, task, attempt, role, stage, backend, and model correlation;
- distinguish unavailable, partial, reported, and not-applicable usage;
- prevent cumulative JSONL records from being double-counted;
- add deterministic parser and aggregation tests.

Stop condition: do not infer provider usage from prompt bytes when the runtime
does not report it. Mark the record unavailable instead.

Acceptance:

- every supported Codex invocation emits one terminal usage record;
- fake and shell adapters report not-applicable usage;
- totals reconcile with the final provider/session usage payload;
- repeated replay does not change totals.

### Task 2: Stage And Role Projection

Objective: make token cost explainable without reading raw runtime logs.

Scope:

- extend the projection database with invocation-level usage rows;
- index run, round, stage, role, task, attempt, backend, model, and status;
- expose aggregate usage by run, stage, role, round, and model;
- report collection coverage as `reported/expected` invocations;
- retain file-scan fallback when the projection DB is missing or stale;
- extend the existing `agentteam stats` command instead of adding a separate
  command family.

Acceptance:

- project totals equal replayed authoritative totals;
- `stats --json` exposes stage, role, round, model, and coverage breakdowns;
- compact text reports total usage, coverage, and the largest stage only;
- stale projection behavior remains explicit and rebuildable.

### Task 3: Budget And Stop Policy

Objective: prevent an experiment from consuming an unbounded budget before it
produces a verified milestone.

Scope:

- support optional run, pursue, milestone, and invocation budgets;
- distinguish soft warning from hard stop;
- check hard budgets at safe scheduler boundaries rather than terminating in
  the middle of a Git integration transaction;
- record the stage and invocation that crossed a budget;
- preserve resumable state after a budget stop;
- keep budgets optional until calibration establishes useful defaults.

Acceptance:

- a fake usage fixture can trigger warning and hard-stop paths;
- a hard stop leaves the run resumable and does not lose accepted patches;
- operator reports explain used, remaining, and unavailable usage.

### Task 4: Reproducible Experiment Harness

Objective: compare AgentTeam with a simpler baseline on the same repository
state, goal, and acceptance checks.

Define an experiment manifest containing:

```text
experiment_id
source_repository
source_commit
goal
task_class
acceptance_commands
time_budget
token_budget
baseline_mode
agentteam_mode
model_profile
repetition_index
```

Required execution modes:

1. `single_codex`: one Codex task with the same goal and acceptance checks.
2. `agentteam_direct`: AgentTeam executes a reviewed pre-authored taskpack,
   isolating runtime/governance overhead from authoring overhead.
3. `agentteam_full`: AgentTeam performs authoring, routing, execution,
   verification, and follow-up.

Required outcome metrics:

```text
acceptance checks passed
verified milestones completed
accepted and rejected attempts
operator interventions
retries and repairs
wall time
input, cached input, output, and reasoning tokens
usage coverage
changed files
regressions
artifact bytes written
```

Acceptance:

- every run starts from the same source commit or equivalent clean snapshot;
- acceptance checks are identical across modes;
- results are machine-readable and produce a concise comparison report;
- failed and interrupted experiments remain part of the result set.

### Task 5: Short Calibration

Objective: prove usage accounting before spending a long-running budget.

Use one deterministic small L1 fixture and one bounded L2 multi-file fixture.
Run each mode once, then repeat one mode to detect counting drift.

Exit gate:

- 100% of benchmark-counted model invocations have reported usage;
- totals do not change after projection rebuild;
- no invocation is counted twice;
- cached and non-cached input remain distinguishable;
- missing usage has a concrete source and reason.

Partial coverage may be used only to debug collection. It cannot pass this gate
or contribute a scored benchmark result.

Do not proceed to the medium comparison while this gate fails.

### Task 6: Medium Multi-Round Comparison

Objective: determine where AgentTeam overhead appears before attempting a long
task.

Task shape:

- two or three bounded milestones;
- multiple source files and tests;
- at least one failed verification or repair opportunity;
- mechanical final acceptance;
- no semantic-authority update required yet.

Compare all three execution modes. The direct/full split should reveal whether
the dominant cost comes from taskpack authoring or from runtime governance.

Initial decision thresholds, to be confirmed before the first run:

- small L1 AgentTeam-direct token overhead should remain within 25% of the
  single-Codex baseline;
- usage coverage must remain 100%;
- AgentTeam-full should not exceed twice the baseline token usage before its
  first verified milestone;
- an AgentTeam advantage must appear as higher verified completion, fewer
  operator interventions, safer recovery, or fewer regressions, not merely
  more generated artifacts.

### Task 7: Fault-Injection And Resume Matrix

Objective: prove that durable state prevents repeated work and duplicate model
cost after interruption.

Begin this task only after the normal medium comparison has completed. Do not
mix fault injection into the first core comparison.

Inject termination at these boundaries:

```text
taskpack author running
worker running
result written before collect
result accepted before integration
integration verified before merge
scheduler restart with inflight attempt
```

Acceptance:

- no terminal attempt is executed twice;
- no accepted patch is silently lost;
- replay does not duplicate usage or integration events;
- resume reuses completed evidence and context where policy allows;
- repeated model cost caused by recovery is separately attributable.

This task closes the remaining M29 inflight-migration risk before a long run.

### Task 8: Real Long-Running Validation

Objective: test the actual AgentTeam contribution on a real repository goal.

Required task characteristics:

- at least three milestones;
- multiple implementation rounds;
- a bounded semantic-design gap discovered during implementation;
- one semantic feedback proposal and reviewed resolution;
- at least one interruption or failed verification;
- mechanical final acceptance criteria;
- explicit operator and token budgets.

The primary outcome is not total token usage alone. Report:

```text
tokens per verified milestone
tokens per accepted integration
verified acceptance items per million tokens
operator interventions
repeated-reading and retry share
semantic findings accepted or rejected
recovery cost
```

## Continue, Narrow, Or Pivot Gate

After the long-running comparison, choose one route:

### Continue The Native Runtime

Choose this only when AgentTeam shows measurable value in verified completion,
operator effort, recovery, regression prevention, or semantic feedback quality
at an acceptable token and latency cost.

### Narrow To An Artifact-Governance Layer

Choose this when semantic authority, evidence routing, and feedback proposals
show value but the custom scheduler/runtime does not outperform simpler
orchestrators. Preserve the governance contracts and integrate them with Codex,
Agent Orchestrator, Overstory, or another execution layer.

### Stop Or Archive The Experiment

Choose this when the framework adds substantial cost without measurable
improvement over the single-agent baseline and its governance mechanisms do not
produce independently useful evidence.

## Maintainability Work After Measurement

Do not perform broad refactoring before usage capture and the calibration
experiment, because measurements should determine which paths matter. After
calibration, prioritize:

- split the oversized CLI, runtime, scheduler, projection, and taskpack modules
  along existing service boundaries;
- add artifact, event, and database schema migration tests;
- add command permission, secret redaction, and write-scope security tests;
- normalize the repository entrypoint and Git/worktree layout;
- update M0-era README sections that no longer describe the implemented
  runtime;
- add release install, update, rollback, and run-pinning end-to-end coverage.

## Explicit Non-Goals

Until the continue/narrow/pivot gate is evaluated, do not prioritize:

- M68 or other multi-model adapters;
- a web dashboard;
- A2A as the native control plane;
- DB-primary authority storage;
- distributed multi-host execution;
- additional role types without experiment evidence;
- additional prose trace files for ordinary model invocations.

## Recommended First Implementation Task

Execute the dedicated Phase 1 taskpack:

[`2026-07-23-phase1-model-invocation-usage.md`](2026-07-23-phase1-model-invocation-usage.md)

It first completes usage capture, then canonical replay accounting, then the
required projection and query surface. Stop before projection work if capture
cannot interpret the provider payload without guessing.

## Verification Plan For This Route Note

This document-only change should be verified by:

- `git diff --check`;
- confirming all route tasks have objectives, scope, acceptance, and stop
  conditions;
- confirming the roadmap links this note and replaces its stale post-M66 next
  step;
- confirming no semantic authority artifact changed;
- confirming no runtime code changed.

## AgentTeam Target Review Gate

This route authorizes planning and future bounded implementation tasks. It does
not authorize automatic source merge, push, release activation, semantic
authority mutation, or an unbudgeted live long-running experiment.

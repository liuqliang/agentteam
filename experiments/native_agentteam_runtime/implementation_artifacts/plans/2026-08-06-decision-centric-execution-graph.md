# Decision-Centric Execution Graph

Status: operator-approved architecture and staged migration plan. This document
is implementation authority for reorganizing AgentTeam recovery, trace, Git
state, and durable artifacts around decisions. It does not rewrite historical
runs or change the research claims established by the Phase 2 experiment
harness.

## Decision Summary

Decision is the durable aggregate root of AgentTeam execution.

Tasks, attempts, runs, Git commits, verification evidence, and reports exist to
implement or evaluate a decision. A run is an execution container, not the
top-level authority. Recovery starts from active decisions and their latest Git
state instead of reconstructing intent from every historical event.

The target graph is:

```text
Decision
|- child decisions
|- contract artifacts
|- tasks and attempts
|- code-state artifacts
|- evidence artifacts
`- report artifacts
```

This changes the organizational model without making the database primary
authority and without treating every runtime file as an artifact.

## Decision Boundary

A decision exists only when the system selects among meaningful alternatives
or accepts/rejects an outcome. Mechanical operations inherit the nearest active
decision.

Examples that create a decision:

- changing objective, scope, constraints, architecture, or acceptance;
- choosing an implementation route when multiple credible routes exist;
- choosing task decomposition, model routing, retry, recovery, or termination
  when the choice affects cost, behavior, or evidence;
- accepting or rejecting worker output, integration, or milestone completion.

Examples that do not create a decision:

- creating or deleting a worktree;
- running a command already required by a frozen contract;
- writing a heartbeat, acquiring a lease, or dispatching an approved task;
- recording an observation or test result before deciding what it means.

The ledger records concise engineering rationale. It must not store hidden
chain-of-thought or duplicate raw model output.

## Three Decision Kinds

The closed `decision_kind` vocabulary is:

```text
direction
execution
acceptance
```

- `direction` decides what must be achieved and why. Architecture decisions
  are `direction` decisions with `authority_level: L3`.
- `execution` decides how an authorized direction should proceed. Task
  decomposition, model routing, retries, and recovery are subjects of this
  kind, not separate decision kinds.
- `acceptance` decides whether evidence is sufficient to accept, integrate,
  reject, or supersede an outcome.

`subject` is an open, searchable label such as `task_split`, `model_routing`,
`implementation_route`, `retry`, `recovery`, or `integration`. It does not
affect the state machine.

`authority_level` is orthogonal to kind:

- L0 mechanical work creates no decision;
- L1 records only a meaningful local choice;
- L2 records task, interface, integration, and evidence choices;
- L3 requires semantic-architecture authority and escalates to the operator if
  the architecture role cannot resolve it.

## Decision Record

One immutable revision contains at least:

```text
schema_version
decision_id
revision
decision_kind
subject
authority_level
parent_decision_id
supersedes_decision_id
statement
selected_option
alternatives
rationale
scope
expected_outcome
acceptance_refs
status
created_at
created_by
previous_revision_sha256
```

The minimal status vocabulary is:

```text
proposed
active
completed
rejected
superseded
```

Authorization and execution details remain events or linked evidence. They do
not require more decision statuses. A published revision is immutable. A
change appends a new revision bound to the prior revision digest. Replacing the
decision itself creates a new decision with `supersedes_decision_id`.

## Artifact Boundary

An artifact is a durable, addressable object needed as decision input, code
state, evidence, or human-facing outcome. It is not synonymous with a file.

The closed `artifact_kind` vocabulary is:

```text
contract
code_state
evidence
report
```

- `contract`: taskpack, protocol, execution plan, acceptance criteria, or
  frozen resource and permission constraints;
- `code_state`: Git commit or protected internal Git ref. Long-lived patch
  files are compatibility inputs, not the target code-state authority;
- `evidence`: verification, benchmark, usage, calibration, failure diagnosis,
  or integration result selected to support an acceptance decision;
- `report`: bounded human-facing projection generated from decisions, Git, and
  evidence.

Heartbeat, lease, scheduler queue, mutable state snapshot, temporary worktree,
ordinary event, raw provider stream, and SQLite row are not artifacts. A
bounded excerpt or digest may become evidence only through an explicit link to
a decision.

Each durable artifact link contains:

```text
artifact_id
decision_id
artifact_kind
locator
digest_algorithm
digest
producer
created_at
```

Artifact links are append-only. Reports may be rebuilt. Contract, code-state,
and evidence objects remain authoritative at their declared locators.

## Storage Responsibilities

### Git

Git is authoritative for recoverable code state:

- task base commit;
- meaningful checkpoint commit;
- terminal result commit;
- verified integration commit.

Attempt and checkpoint commits may be retained by protected internal refs:

```text
refs/agentteam/runs/<run-id>/<task-id>/<attempt-id>
refs/agentteam/checkpoints/<decision-id>/<task-id>
```

Normal branches remain reserved for reviewed integration. Worktrees are
disposable and can be reconstructed from retained commits. Commit trailers may
reference `AgentTeam-Decision`, `AgentTeam-Task`, and
`AgentTeam-Verification`, but decision rationale remains in the ledger.

### Decision Ledger

One project-level append-only ledger stores decision revisions and artifact
links. It is the authority for intent lineage. It must support content-digest
validation, idempotent append, parent/supersession validation, and bounded
records.

The ledger must not create one file per decision. The initial implementation
uses one hash-chained ledger stream plus one lock file per project. A separate
checkpoint file is added only if measured replay cost justifies it.

### Compact Event Journal

Events record execution facts only: dispatch, lease, process lifecycle,
verification, integration, interruption, and recovery. Events carry the
inherited `decision_id` when one is active. They do not repeat rationale,
patches, complete file lists, or task descriptions already reachable from the
decision graph.

### SQLite Projection

SQLite remains rebuildable. It projects:

- latest decision revision and status;
- parent and supersession edges;
- decision-to-artifact links;
- decision-to-run/task/attempt execution edges;
- latest recoverable Git commit;
- evidence and acceptance coverage.

Deleting the database must not delete authority. Rebuild scans the decision
ledger, compact events, retained artifact authorities, and Git refs.

## Execution And Recovery

Every executable task inherits exactly one active decision. Child decisions
may refine work but cannot silently broaden the parent scope or authority.

Recovery proceeds as follows:

1. load latest valid revisions for active decisions;
2. validate parent and supersession relationships;
3. locate the latest retained code-state artifact for each active execution;
4. replay only the compact event tail after that checkpoint;
5. mark an orphaned running attempt as interrupted;
6. resume a provider session only when its identity and usage lineage remain
   valid; otherwise launch a replacement worker against the checkpoint;
7. rerun contract-required verification before accepting new work;
8. create an acceptance decision before integration or milestone completion.

Recovery does not require reading all raw logs or reconstructing source changes
from patch files.

## Checkpoint Policy

- L0/L1 work creates a terminal commit only.
- L2 work creates a checkpoint at a meaningful implementation or verification
  boundary, not on a timer alone.
- Long-running L2 work may inspect checkpoint eligibility every 10-20 minutes,
  but creates a commit only when the tree changed and the checkpoint is
  coherent.
- L3 decisions checkpoint semantic authority separately from implementation
  code and retain the architecture approval relation.
- Failed attempts retain at most the latest useful checkpoint, terminal reason,
  and selected evidence. Raw output follows bounded retention policy.

## Compatibility And Migration

Historical taskpacks, runs, events, patches, reports, and database projections
remain immutable. Migration is additive:

1. create one synthetic `legacy` direction decision per existing root goal or
   taskpack lineage when no explicit decision exists;
2. link the frozen taskpack as `contract`;
3. link known base, result, and integration commits as `code_state`;
4. promote only validation summaries and accepted failure diagnostics to
   `evidence`;
5. link existing operator reports as `report`;
6. retain old files until the rebuilt decision projection proves equivalent
   recovery and reporting;
7. stop creating redundant patch and trace artifacts only for new-format runs.

Migration must not infer rationale that historical evidence does not contain.
Synthetic records use a bounded statement such as `legacy execution lineage`
and identify missing rationale explicitly.

## Ordered Implementation

### D0: Contract And Vocabulary

- add schemas for decision revisions and artifact links;
- add validators and canonical digest helpers;
- prove the closed kinds, minimal statuses, bounded rationale, revision chain,
  parent, and supersession rules.

### D1: Append-Only Decision Authority

- add a project-level decision ledger;
- provide idempotent create, revise, and artifact-link operations;
- reject mutation, conflicting replay, invalid revision chains, unsafe
  locators, and dangling decisions.

### D2: Projection And Queries

- project decisions, edges, and artifact links into `agentteam.db`;
- add compact queries for active decisions and one decision execution graph;
- make projection rebuild and stale detection include decision authority.

### D3: Runtime Propagation

- bind taskpacks, runs, tasks, attempts, model invocations, and events to an
  inherited decision;
- require an acceptance decision before integration;
- keep legacy runs executable without fabricated rationale.

### D4: Git-Backed Recovery

- publish protected attempt/checkpoint refs;
- recover disposable worktrees from code-state artifacts;
- replay only the event tail after the selected checkpoint;
- prove crash, missing-worktree, stale-ref, conflicting-ref, and provider
  session fallback behavior.

### D5: Artifact Reduction And Migration

- create deterministic legacy decision indexes;
- stop persisting redundant patch, changed-file, and prose trace copies for
  new-format runs;
- add retention for raw logs and abandoned internal refs;
- demonstrate equivalent report and recovery outcomes with lower artifact
  count and bytes.

## Acceptance Criteria

The migration is complete only when:

- every new-format task and attempt resolves to one active decision;
- every retained artifact resolves to a decision and one of four kinds;
- Git can reconstruct every retained code checkpoint without a patch file;
- SQLite can be deleted and rebuilt without losing decision lineage;
- recovery resumes from decision plus Git checkpoint and bounded event tail;
- integration is impossible without a recorded acceptance decision;
- L0/L1 execution does not create decision or trace noise;
- historical runs remain readable and executable;
- a comparison run demonstrates lower retained artifact count and bytes without
  reducing recovery or audit coverage.

## Non-Goals

- no graph database;
- no DB-primary authority;
- no Git commit per command or heartbeat;
- no one-file-per-decision layout;
- no migration rewrite of immutable historical runs;
- no automatic L3 architecture approval;
- no storage of hidden model reasoning;
- no requirement that all observations become evidence artifacts.

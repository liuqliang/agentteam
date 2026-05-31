# Native Runtime Semantic Architecture

Status: current semantic contract for the native AgentTeam runtime experiment.

This specification defines the semantic objects and state boundaries that must
remain true regardless of whether the first implementation uses files, SQLite,
Codex, Claude Code, Aider, OpenCode, scripts, or another backend.

## Purpose

The native runtime lets AgentTeam coordinate mature agent tools without making
any single tool's session model the authority model. AgentTeam owns durable
coordination. Runtime backends own concrete model interaction and tool
execution.

```text
AgentTeam owns:
  scheduler, state, mailbox, events, leases, scopes, validation, integration

Runtime backends own:
  model interaction, code editing, command execution, transcript production
```

## Identity And Lifecycle Boundaries

The system must keep these identities separate:

| Object | Lifecycle | Authority Meaning |
|---|---|---|
| `RoleAgent` | Long-lived logical state | A durable responsibility boundary and permission profile. |
| `RuntimeSession` | Short-lived process or API call | One backend invocation used to perform a bounded attempt. |
| `Attempt` | One concrete try at one task | The unit of retry, result validation, and evidence. |
| `Worktree` | One writable execution workspace | The isolated filesystem state for one writable attempt. |

The rule is:

```text
long-lived logical agent
short-lived runtime invocation
one writable attempt, one worktree
```

A `RoleAgent` may run through different `RuntimeAdapter` implementations over
time. A failed `RuntimeSession` does not destroy the role. A stale `Worktree`
does not redefine the task. A retry creates a new `Attempt` rather than
reopening a terminal attempt.

## Authority And Artifact Governance

Artifacts are classified by the kind of truth they carry:

- `semantic_contract`: behavior, invariants, role boundaries, message meaning,
  state transitions, acceptance criteria, and canonical shared facts.
- `implementation_authority`: implementation route, task cards, context packs,
  worktree policy, verification objects, progress, and local ADRs.
- `derived_observation`: repo map, file inventory, dependency observations, and
  stale navigation facts.
- `evidence_note`: command summaries, worker findings, review findings, and
  trace attachments.

Raw runtime output can create a proposal or evidence note. It cannot directly
change authority.

Authority updates follow this rule:

```text
runtime output
  -> structured result
  -> Validator
  -> accepted proposal or rejected result
  -> Integrator
  -> updated authority artifact and event
```

Semantic contract changes require a design CR and serial integration.
Implementation authority changes require validated implementation events and
integration. Derived observations may be regenerated with provenance and stale
conditions.

## Decision Boundaries

The `Scheduler` is deterministic software. It owns:

- loading project configuration and agent definitions;
- detecting ready tasks;
- checking dependencies and stale state;
- issuing and expiring leases;
- enforcing concurrency limits;
- creating worktrees for writable attempts;
- invoking runtime adapters;
- recording state transitions as events;
- routing retries, cancellation, recovery, and manual gates.

LLM role agents may own semantic judgments:

- task slicing;
- context selection advice;
- risk classification assistance;
- implementation attempt;
- verification interpretation;
- semantic feedback proposal;
- adversarial review.

LLM agents must not own queue mechanics, lease issuance, event replay,
authority mutation, or final integration.

## Communication Semantics

Mailbox messages and events are different.

Mailbox messages express intent:

```text
dispatch_task
request_context_pack
request_verification
request_semantic_feedback
cancel_attempt
worker_result
validation_result
integration_result
```

Events record accepted facts:

```text
task_selected
lease_acquired
worktree_created
runtime_session_started
runtime_output_received
validation_accepted
validation_rejected
patch_integrated
artifact_update_proposed
artifact_update_integrated
attempt_timed_out
attempt_cancelled
recovery_routed
```

Messages may be retried or superseded. Events are append-only. Rebuilding state
must use events and indexed artifacts, not chat memory.

Every message and event must carry a correlation id. Every execution-affecting
event must carry an idempotency key so replay does not duplicate side effects.

## Writable Attempt Lifecycle

A writable task attempt follows this semantic path:

```text
Task ready
  -> Scheduler selects task
  -> lease_acquired
  -> Worktree created for Attempt
  -> ContextPack built or referenced
  -> RuntimeAdapter starts RuntimeSession
  -> RuntimeSession returns structured result
  -> Validator checks schema, scope, diff, evidence, and risk
  -> Integrator merges, rejects, rebases, or opens follow-up task
  -> events record terminal outcome
```

Required invariants:

- A writable attempt has at most one assigned worktree.
- A worktree is not shared by parallel writable attempts.
- A completed runtime session does not imply accepted work.
- A completed attempt remains `running` at backlog level until verification and
  integration pass.
- A retry creates a new attempt id.
- Authority checkout mutation happens only through the Integrator.

## Read-Only Role Lifecycle

Read-only roles do not receive private worktrees by default. They receive
artifact references, repository snapshots, exact file read lists, or context
packs.

Examples:

- `repo_map_agent` reads manifests and files to produce derived observations.
- `context_builder_agent` selects files and semantic references for a task.
- `risk_classifier_agent` reads task, diff summary, and evidence to recommend
  an evidence level.
- `semantic_feedback_agent` reads worker findings and proposes design CRs.

Read-only roles may produce proposals and observations. They must not silently
edit implementation or semantic authority.

## State Machines

### Task

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

Unknown task states are invalid and must route to `blocked` with a diagnostic
event.

### Attempt

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

Terminal attempts are immutable. Retrying creates a new attempt.

### Lease

```text
active -> released
active -> expired
active -> cancelled
expired -> recovery_routed
```

An expired lease must produce an event before reassignment.

### Integration

```text
not_integrated -> integrated
not_integrated -> rejected
not_integrated -> rebase_required
rebase_required -> not_integrated
rebase_required -> rejected
```

`integrated` is terminal for that result. Follow-up work creates a new task or
attempt.

## Runtime Adapter Contract

A runtime adapter normalizes backend-specific mechanics. It must expose
semantic operations, not vendor-specific chat assumptions:

```text
start_session(role, message, workspace_policy, runtime_profile)
send_input(session, message)
observe(session)
stop(session)
collect_result(session)
parse_transcript(session)
```

The adapter must report:

- backend id;
- runtime profile;
- command or API mode;
- workspace path;
- environment and permission profile;
- liveness state;
- raw output reference;
- structured result extraction status;
- exit status or timeout status.

The adapter must not decide whether the result is authoritative.

## External Protocol Boundaries

MCP is accepted as tool and context plumbing:

```text
RuntimeSession -> MCP tool/resource/prompt access
```

MCP does not own task selection, leases, artifact authority, or integration.

A2A is postponed. It may later become an external interoperability bridge:

```text
External agent system <-> A2A bridge <-> AgentTeam mailbox adapter
```

A2A is not part of the M0 or M1 native control plane.

## Semantic Feedback Path

Implementation can discover that the semantic design is incomplete. That
finding follows a controlled path:

```text
worker finding
  -> structured result.design_findings
  -> Validator confirms it is in scope and evidence-backed
  -> semantic_feedback_agent drafts proposal
  -> design CR opened
  -> Integration Agent updates semantic contract if accepted
```

Workers may report design gaps. Workers may not dispatch authority-writing
design agents or edit semantic artifacts directly.

## Reuse Semantics

Agent Orchestrator and Overstory are references, not core dependencies.

Borrowed concepts:

- plugin boundaries;
- runtime adapter shape;
- worktree/session lifecycle;
- persistent mail and typed messages;
- watchdog layering;
- role access control.

Rejected coupling:

- no assumption that every task is a GitHub issue;
- no assumption that every role owns a permanent worktree;
- no assumption that one existing project's process model defines AgentTeam's
  semantic model.

## M0 Semantic Target

M0 is semantically valid when a file-backed simulation can show:

1. a ready task is selected by deterministic scheduler rules;
2. a lease is acquired;
3. a mailbox dispatch is recorded;
4. a fake or real runtime result is returned as structured data;
5. validation accepts or rejects the result;
6. the event log can reconstruct the task and attempt state;
7. no authority artifact changes without an integration event.

M0 does not require permanent agent processes, A2A, a web UI, or multi-backend
execution.

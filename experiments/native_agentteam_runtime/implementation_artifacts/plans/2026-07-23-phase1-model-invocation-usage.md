# Phase 1 Model Invocation Usage Attribution Taskpack

Status: complete semantic taskpack specification. Runtime execution remains
blocked until P0-A is accepted, the operator approves the revised contract,
and execution preflight gates G0-G5 pass.

This taskpack implements Phase 1 of the operator-approved research positioning
authority:

- [`agentteam_research_positioning.md`](../../research/agentteam_research_positioning.md)
- [`2026-07-11-cost-attribution-and-long-run-validation.md`](2026-07-11-cost-attribution-and-long-run-validation.md)
- [`2026-07-24-phase1-execution-preflight.md`](2026-07-24-phase1-execution-preflight.md)
- [`2026-07-25-p0a-experiment-readiness-guard.md`](2026-07-25-p0a-experiment-readiness-guard.md)
- [`2026-07-23-phase1-model-invocation-usage.blueprint.json`](2026-07-23-phase1-model-invocation-usage.blueprint.json)

It does not change the research claims or semantic authority. It turns the
approved Phase 1 objective into bounded implementation tasks that can be
executed by AgentTeam and integrated only after operator review.

## Taskpack Header

```text
taskpack_id: phase1-model-invocation-usage
goal_kind: implementation
milestone: research-phase-1
semantic_review_risk: L3, executed outside the frozen backlog
executable_taskpack_risk: L2
implementation_item_risk: bounded L0/L1 tasks
runtime_backend: codex
runtime_host: Linux with pidfd and a usable systemd user manager
allow_source_merge: false
allow_push: false
allow_release_activation: false
semantic_authority_write: false
integration_policy: verified integration baseline, then operator review
```

Recommended role and capability routing:

| Work | Required role | Capability profile | Reason |
| --- | --- | --- | --- |
| Contract and authority review | `semantic_architecture_agent` | high reasoning | The record identity, counting, and authority rules affect all later experiments. |
| Existing path inventory | deterministic repository scan | no model call | The target modules and direct callers are frozen in this tracked plan. |
| Core parser and capture changes | `implementation_worker` | high reasoning | Provider payload semantics and no-double-count behavior are correctness critical. |
| Projection and CLI changes | `implementation_worker` | high reasoning | File authority, DB rebuild, fallback, and aggregation must remain consistent. |
| Mechanical regression additions | `implementation_worker` | medium where supported | Fixture and output tests are bounded after the contract is frozen. |
| Final source merge and release | operator | not delegated | AgentTeam is the target repository. |

Do not hard-code a provider model name in source. Resolve the actual model from
the runtime profile, command, or provider output. If it cannot be established,
record it as unavailable rather than guessing.

## Goal

Implement complete, replay-stable attribution for every supported
Codex-backed model invocation, then make that usage queryable by:

```text
project
run
run kind
implementation run
gate epoch
pursue
round
stage
role
task
attempt
backend
model
```

Phase 1 is complete only when a deterministic end-to-end fixture reports one
terminal usage record for every model invocation, projection rebuild produces
the same totals, and no supported invocation path is silently absent from
coverage. Positive fixtures must reach 100% lifecycle and token coverage;
negative recovery fixtures must match their explicitly lower expected token
coverage without being mixed into the positive score.

## Definition Of Done

All of the following must be true:

1. `model_invocation_started.v1` and `model_invocation_usage.v1` are represented
   by validated structured records.
2. Every supported Codex invocation durably records its start before process
   launch and eventually has exactly one terminal record, including completed,
   failed, blocked, cancelled, timed-out, launch-failed, and recovered-orphan
   calls.
3. Initial and follow-up taskpack authoring, planner/task slicing, repository
   mapping, implementation, review/repair, semantic architecture, runtime
   diagnostic, tracked development-smoke, and acceptance calls are
   attributable to an explicit stage.
4. Fake and shell adapters are represented as `not_applicable` only when they
   pass through a model-invocation boundary; deterministic scheduler, Git,
   validation, notification, and SQLite operations are not counted as model
   calls.
5. Provider-reported token usage is never replaced by prompt-size estimates.
6. Cached input and reasoning tokens remain separate fields and are not added
   to totals a second time.
7. Replaying events and rebuilding `agentteam.db` does not change invocation
   count or token totals.
8. `agentteam stats --json` exposes lifecycle coverage, token coverage, exact
   reported totals, partial lower-bound diagnostics, and the required
   attribution dimensions; compact text reports these without raw logs.
9. Existing runs without invocation records remain readable and are identified
   as legacy or unavailable rather than being assigned fabricated usage.
10. Normal deterministic tests do not require a live Codex call.
11. At least one operator-gated live-smoke attempt confirms that the candidate
    runtime understands the installed Codex JSONL format before Phase 1 is
    marked complete; every failed or retried live attempt remains attributed.
12. No source merge, push, release activation, or semantic authority update
    occurs without operator review.
13. The deterministic and live acceptance gates have zero open invocation
    lifecycles; a process crash cannot remove a launched call from the coverage
    denominator.
14. A controller-owned finalization artifact binds the validated code commit
    to a report-only child commit without a self-referential SHA or additional
    source changes.
15. Commit-bound gates belong to one immutable validated gate epoch; target
    divergence creates and fully revalidates a new branch/epoch rather than
    rebasing or rewriting accepted evidence.
16. Controller-valid finalization remains `awaiting_operator_review`; only a
    separate immutable approval bound to the exact epoch, evidence, diff, and
    integration head can complete P1-06E.

## Baseline And Current Gaps

The current implementation already provides:

- `token_usage.py`, which normalizes usage dictionaries, reads the last usable
  candidate from Codex JSONL, aggregates task-level usage, and formats a compact
  token line;
- `CodexRuntimeAdapter`, which invokes `codex exec --json` and attaches parsed
  usage on the successful result path;
- mailbox and scheduler propagation of `token_usage`;
- run-level token totals in operator reports;
- run-level token columns in the rebuildable SQLite projection;
- project-level `agentteam stats`;
- deterministic tests for reported, unavailable, and not-applicable summaries.

The current gaps are:

- taskpack authoring does not request and parse JSONL usage;
- failure, timeout, blocked, missing-output, and some early-return worker paths
  can lose usage that was emitted before termination;
- the aggregate unit is a task result rather than a model invocation;
- there is no stable invocation or usage-event identity;
- stage, role, round, model, and runtime-session correlation is incomplete;
- author usage lives outside run reports and projections;
- planner, repo-map, semantic architecture, implementation, and retries use the
  same adapter without an authoritative stage record;
- the projection stores run totals but not invocation rows;
- coverage currently means reported task attempts, not reported model
  invocations;
- replay cannot prove no-double-count behavior because there is no
  invocation-level primary key.
- there is no durable invocation-start authority, so a crash after provider
  launch but before terminal-result collection can make a real call disappear
  from the coverage denominator;
- `runtime_session_id` currently identifies an AgentTeam execution session,
  while Codex resume settings identify a separate provider session; using one
  field for both would make resumed-session deltas ambiguous.

## Repository Grounding Handoff

This section is the pre-authored repository handoff for the taskpack. A model
must not spend another full-repository pass rediscovering these entry points
unless implementation evidence shows the map is stale.

| Concern | Primary path | Current responsibility |
| --- | --- | --- |
| Usage parsing and aggregation | `m0_runtime/agentteam_runtime/token_usage.py` | Normalize result/JSONL usage and aggregate legacy summaries. |
| Codex worker invocation | `m0_runtime/agentteam_runtime/m0_runtime.py` | Run Codex, build prompts, normalize results, attach JSONL usage. |
| Mailbox result propagation | `m0_runtime/agentteam_runtime/mailbox_worker.py` | Copy runtime usage into worker outbox results. |
| Dispatch, collect, event, report | `m0_runtime/agentteam_runtime/two_phase_scheduler.py` | Correlate attempts, append canonical events, aggregate task reports. |
| Taskpack author invocation | `m0_runtime/agentteam_runtime/taskpack_author.py` | Run Codex author, spool stdout/stderr, write author result/state. |
| Start, follow-up, pursue context | `m0_runtime/agentteam_runtime/agentteam.py` | Submit taskpacks, create pursue rounds, render project stats. |
| Rebuildable DB projection | `m0_runtime/agentteam_runtime/projection_db.py` | Scan authoritative files, rebuild SQLite, serve stats. |
| Human reports | `m0_runtime/agentteam_runtime/operator_report.py` | Render run and pursue token summaries. |
| Feishu summaries | `m0_runtime/agentteam_runtime/notifications.py` | Reuse structured completion reports for notifications. |
| Event contract | `schemas/event.schema.json` | Enumerate canonical event types and event envelope. |
| Runtime tests | `m0_runtime/tests/test_m0_runtime.py` | Adapter, scheduler, mailbox, parser, and replay behavior. |
| CLI/projection tests | `m0_runtime/tests/test_taskpack.py` | Author, stats, report, DB, pursue, and command behavior. |
| Projection path tests | `m0_runtime/tests/test_projection_db_operator_paths.py` | Real projection rebuild and operator evidence paths. |

The worker should update this map only when a concrete implementation path is
missing. It should not generate a prose repo map artifact per subtask.

## Contract Decisions

### Invocation Versus Usage Event

`invocation_id` identifies one actual attempt to call a model backend.
`usage_event_id` identifies the terminal usage record for that invocation.

- An invocation ID is allocated before launch. A gated supervisor may be
  created first, but it cannot launch the provider until the immutable start
  record containing its process identity is durably published.
- Rewriting or replaying the same terminal record preserves both IDs.
- Starting the provider process again creates a new invocation ID, even when it
  is for the same task attempt. The repeated model cost is real and must remain
  visible.
- `usage_event_id` is derived deterministically from `invocation_id`, for
  example with a namespaced SHA-256 digest.
- Projection deduplicates byte-equivalent records by `usage_event_id`.
- Two non-equivalent records with the same `usage_event_id` are a projection
  integrity error. The projection must not silently choose one.

### Invocation Lifecycle

Add a JSON Schema at:

`experiments/native_agentteam_runtime/schemas/model_invocation_started.schema.json`

Also add the immutable revocation-record schema at:

`experiments/native_agentteam_runtime/schemas/model_invocation_writer_revoked.schema.json`

The immutable `model_invocation_started.v1` record contains the invocation ID,
all correlation fields available before launch, `started_at`, backend, model,
usage stage, runtime execution session, requested provider-resume metadata,
`coverage_class`, `lifecycle_owner_token`, the gated supervisor PID and process
group, host boot ID, process start ticks, launch nonce digest, and the
invocation's systemd user transient-service unit name, systemd `InvocationID`,
manager identity, and `ControlGroup` path. It also records the enclosing
system-level `user@<uid>.service` `InvocationID`, `ControlGroup`, and
`KillMode`. The persisted tuple identifies the process, its execution group,
and the user-manager lifetime across PID reuse or manager restart. The record
contains no token fields.

The runtime writes lifecycle records below a runtime-owned output root outside
the target repository:

```text
model_invocations/<invocation_id>/started.json
model_invocations/<invocation_id>/revoked.json
model_invocations/<invocation_id>/terminal.json
model_invocations/<invocation_id>/terminal.lock
```

The taskpack author uses the same relative layout below its author context
directory. JSON records are atomically published and immutable;
`terminal.lock` is synchronization only and is never projected as evidence. A
retry creates a new directory and invocation ID; it never overwrites an earlier
lifecycle.

Lifecycle rules:

- a supported live adapter must have a writable, runtime-owned output root
  registered below the configured project work root before launch;
- before creating a service, the parent requires `loginctl` linger for the
  current user and records the enclosing system-level
  `user@<uid>.service` `InvocationID`, `ControlGroup`, and a `KillMode` whose
  completed stop/restart kills all child processes;
- the parent first creates a unique systemd user transient service for the
  invocation with `Type=oneshot`, `RemainAfterExit=yes`,
  `KillMode=control-group`, and a bounded unit name derived from the invocation
  ID. That service launches the minimal gated supervisor in a new process
  group. The parent verifies and persists the service's systemd
  `InvocationID`, user-manager identity, enclosing user-service identity, and
  exact `ControlGroup` path. A plain PGID without this execution-group
  authority is not a supported live path;
- the supervisor waits on a runtime-owned one-shot Unix-domain launch channel
  and cannot start the provider before receiving a nonce-bearing permit; EOF,
  nonce mismatch, or a bounded handshake timeout exits without a provider call;
- the supervisor remains the process-group leader and waits for the provider
  child through terminalization; it does not `exec` away its persisted
  identity. The provider is launched in that group with the supported
  parent-death safeguard, and the supervisor kills/reaps the group on abnormal
  exit. The current Linux runtime fails closed before provider launch if this
  safeguard cannot be installed;
- the parent captures the supervisor PID/PGID, host boot ID, process start
  ticks, owner token, launch nonce digest, and verified transient-service
  identity in `started.json`, then atomically publishes and flushes the file
  and parent directory;
- only after that durable publication may the parent issue the one-shot launch
  permit whose nonce matches the persisted digest. If start publication fails
  or the parent dies before the permit, the channel closes and the supervisor
  exits without invoking the provider;
- this two-phase handshake is the only supported provider-launch path. A
  direct `Popen` followed by later PID/PGID publication is forbidden because it
  creates an unfenceable crash window;
- a launch failure still receives one terminal unavailable record with
  `terminal_status: launch_failed`;
- normal finalization writes `terminal.json` exactly once;
- `terminal.json` is durably and atomically published with no-replace
  semantics. A second byte-equivalent observation is idempotent, while a
  conflicting terminal is an integrity failure;
- scheduler collection and recovery import both lifecycle records into
  canonical events without changing their IDs;
- a start without a terminal remains `open` while its process or lease is
  demonstrably live;
- lease expiry alone never authorizes a recovery terminal;
- terminal ownership remains with the worker/author launch identified by
  `lifecycle_owner_token` until the scheduler or owning controller durably records
  `model_invocation_writer_revoked`, terminates the owned worker/provider
  process group, and waits until process death is confirmed;
- recovery first compares the persisted host boot ID. A changed boot proves
  that no process from the recorded invocation survives, so recovery sends no
  signal and may proceed to terminal evidence under the invocation lock;
- on the same boot, recovery first verifies the enclosing
  `user@<uid>.service`. If its `InvocationID` changed through a completed
  systemd stop/restart and its persisted `KillMode` guarantees control-group
  cleanup, no process from the old user-manager lifetime survives; recovery
  sends no signal and may proceed. If the enclosing identity is unchanged,
  the persisted user-manager identity must also match. A changed inner manager
  without a provable enclosing-service transition remains open;
- with the same verified user-manager lifetime, recovery verifies the
  persisted transient unit, `InvocationID`, and `ControlGroup` before any
  action. If the supervisor still exists, it opens a Linux `pidfd` first, then
  compares process start ticks and notifies only through that pinned handle.
  It never sends a later numeric PID/PGID signal;
- if `pidfd_open` returns `ESRCH` because the supervisor was already reaped,
  recovery may confirm death only when the same transient service identity
  still resolves the persisted `ControlGroup` and that cgroup's
  `cgroup.events` reports `populated 0`. If it reports populated, recovery
  stops that exact verified service and waits for `populated 0`. A
  missing/recycled unit, unverifiable user-manager lifetime or `ControlGroup`,
  or failed stop remains open;
- the transient service is reset/removed only after terminal authority is
  durable. Thus a reaped supervisor still has a queryable execution-group
  identity during recovery. Unsupported or ambiguous pidfd/systemd fencing
  fails closed before live launch or leaves an existing invocation open rather
  than risking an unrelated process;
- every normal or recovery terminal writer takes the invocation's exclusive
  `terminal.lock` across its final evidence read, owner check, temp write,
  no-replace publish, and directory flush;
- recovery takes that lock, rereads any existing terminal, and, if none exists,
  atomically publishes `revoked.json` for the exact owner token before
  requesting supervisor shutdown. A normal writer that later acquires the lock
  must reread `revoked.json` and is forbidden to publish for a revoked token;
- recovery keeps the lock through confirmed process death, a final terminal and
  bounded-spool reread, and its exclusive terminal publish. Thus an old writer
  either commits first and is retained, or is fenced before recovery commits;
  it cannot race a later terminal into authority;
- after confirmed death, recovery rereads `terminal.json`. It imports an
  already-written valid terminal; otherwise it parses the bounded provider
  spool and exclusively creates one recovery terminal. Valid terminal provider
  usage remains reported; absent or uninterpretable usage is unavailable with
  a bounded reason such as `worker_terminated_before_usage_finalize`;
- if process ownership or death cannot be proven, the invocation remains open
  and blocks verified idle; recovery never fabricates token values merely to
  make progress;
- a run cannot become verified idle, pass a benchmark gate, or report 100%
  coverage while any supported invocation lifecycle remains open.

### Record Shape

Add a JSON Schema at:

`experiments/native_agentteam_runtime/schemas/model_invocation_usage.schema.json`

The logical `model_invocation_usage.v1` record contains:

```json
{
  "usage_schema_version": "model_invocation_usage.v1",
  "usage_event_id": "USAGE-...",
  "invocation_id": "INV-...",
  "project": "agentteam",
  "run_id": "phase1-example",
  "pursue_id": null,
  "round_index": null,
  "taskpack_id": "phase1-example",
  "implementation_run_id": null,
  "gate_epoch": null,
  "task_id": "TASK-001",
  "attempt_id": "ATTEMPT-001",
  "runtime_execution_session_id": "SESSION-ATTEMPT-001",
  "provider_session_id": null,
  "provider_predecessor_invocation_id": null,
  "provider_turn_id": null,
  "provider_predecessor_turn_id": null,
  "lifecycle_owner_token": "LEASE-...",
  "terminal_writer": "worker",
  "agent_id": "agent-implementation-worker-1",
  "role": "implementation_worker",
  "usage_stage": "implementation_worker",
  "backend": "codex",
  "model": null,
  "coverage_class": "supported_model_invocation",
  "terminal_status": "completed",
  "usage_status": "reported",
  "usage_source": "codex_jsonl",
  "provider_usage_scope": "invocation",
  "accounting_method": "provider_reported",
  "provider_usage_snapshot": null,
  "unavailable_reason": null,
  "input_tokens": 1200,
  "cached_input_tokens": 800,
  "output_tokens": 260,
  "reasoning_tokens": null,
  "total_tokens": 1460,
  "started_at": "2026-07-23T00:00:00Z",
  "finished_at": "2026-07-23T00:01:00Z",
  "wall_time_seconds": 60.0,
  "source_artifact_path": "runs/.../events.jsonl"
}
```

Schema rules:

- token fields are non-negative integers or `null`;
- `round_index` is a positive integer or `null`;
- `implementation_run_id` is null for ordinary implementation-run calls and
  identifies the parent implementation run for controller evidence;
- `gate_epoch` is a positive integer for post-backlog controller calls and
  otherwise null; `acceptance_live_smoke` requires both it and
  `implementation_run_id`;
- identifiers that do not apply are `null`, not invented placeholder strings;
- timestamps are UTC RFC 3339 values;
- `wall_time_seconds` is non-negative;
- `terminal_status` describes subprocess/runtime outcome and is independent of
  usage availability;
- `terminal_status` is one of `completed`, `failed`, `blocked`, `cancelled`,
  `timed_out`, `launch_failed`, `missing_result`, `invalid_result`, or
  `recovered_orphan`;
- `usage_status` is one of `reported`, `partial`, `unavailable`, or
  `not_applicable`;
- `coverage_class` is fixed in the start record as
  `supported_model_invocation` or `not_applicable_adapter`; terminal status
  cannot retroactively remove a supported start from the denominator;
- `usage_source` identifies the source contract, not a human estimate;
- `provider_usage_scope` is `invocation`, `session_cumulative`, or `unknown`;
- `accounting_method` is `provider_reported`, `session_delta`,
  `not_applicable`, or `unavailable`;
- `provider_usage_snapshot` is null for invocation-scoped reporting and stores
  the compact provider cumulative counters when session-delta accounting is
  required;
- `runtime_execution_session_id` identifies one controller-owned AgentTeam
  execution context for one real provider call and is allocated independently
  of provider resume behavior. Worker calls use the scheduler session;
  author, diagnostic, development-smoke, and acceptance controllers allocate
  their own non-empty opaque session identity before durable start
  publication;
- `provider_session_id` comes only from provider output or an explicit,
  previously observed provider session selected for resume;
- `provider_predecessor_invocation_id` identifies the one authoritative prior
  invocation used for session-delta accounting and is otherwise null;
- `provider_turn_id` and `provider_predecessor_turn_id` retain provider-issued
  sequence/turn linkage when the provider exposes it; AgentTeam must not invent
  either value;
- `lifecycle_owner_token` binds the invocation to its worker lease, author
  process owner, or durable controller claim/lease; terminal recovery must cite
  the revoked token;
- `terminal_writer` is `worker`, `taskpack_author`,
  `runtime_diagnostic_controller`, `development_smoke_controller`,
  `recovery_controller`, or `acceptance_controller`;
- `usage_status: not_applicable` requires
  `coverage_class: not_applicable_adapter`; a supported start cannot be removed
  from coverage by its terminal writer;
- `unavailable_reason` is required for unavailable and partial records;
- the source artifact path is normalized relative to the configured project
  work root, remains inside a registered authority root, and is evidence
  metadata rather than record identity.

Correlation resolution is deterministic:

- `project` uses the loaded profile's `project_key`, falling back to the
  existing `default_project_key(project_root)` rule for lower-level commands;
- ordinary author and scheduler invocations use the resolved taskpack ID for
  both `run_id` and `taskpack_id`;
- `acceptance_live_smoke` is the one deliberate exception: `run_id` is the
  immutable attempt-scoped acceptance run ID, while `taskpack_id` and
  `implementation_run_id` remain the logical implementation taskpack ID. The
  controller claim, run identity, lifecycle records, projection rows, fixed
  artifact, and gate receipt must preserve that same mapping;
- worker task, attempt, agent, role, and stage values come from the dispatch
  payload;
- author pursue and round values come from the caller's structured submit
  context, not from parsing the goal text;
- `model` comes from the effective runtime profile, explicit command setting,
  or provider event; it remains null when none reports it;
- `runtime_execution_session_id` comes from the controller that owns the
  provider launch. The scheduler supplies it for worker calls; taskpack author,
  diagnostic, development-smoke, and acceptance controllers allocate and
  durably retain one before launch. It is never synthesized from task,
  attempt, or provider-session identity;
- `provider_session_id` comes only from provider session metadata and must not
  be synthesized from task or attempt IDs;
- `resume_last` may be reported when the provider supplies invocation-scoped
  usage. Session-cumulative output from `resume_last` is always partial or
  unavailable in Phase 1 because the predecessor cannot be authoritatively
  selected before launch.

### Stage Vocabulary

The Phase 1 stage vocabulary is closed:

```text
taskpack_author
planner_or_task_slicer
repo_map
implementation_worker
review_or_repair
follow_up_author
semantic_architecture
runtime_diagnostic
development_smoke
acceptance_live_smoke
```

The caller sets the stage before invoking the adapter. The token parser must
not infer semantic roles from prompt text.

| Invocation path | Stage rule |
| --- | --- |
| Ordinary `start` or `submit` author call | `taskpack_author`, `round_index = null` |
| Pursue round 1 author call | `taskpack_author`, `round_index = 1` |
| Pursue round 2 or later author call | `follow_up_author`, with its positive `round_index` |
| Author call created by `next` | `follow_up_author` |
| `task_kind == decompose_backlog` or role `task_planner` | `planner_or_task_slicer` |
| Role `repo_map_agent` | `repo_map` |
| Role `semantic_architecture_agent` | `semantic_architecture` |
| Explicit review/repair work type or a retry caused by validation/integration rejection | `review_or_repair` |
| Normal code or documentation execution | `implementation_worker` |
| `agentteam chat --interactive` | `runtime_diagnostic` |
| Standalone `live_codex_*_smoke` development entry point | `development_smoke` |
| Controller-owned candidate-runtime smoke | `acceptance_live_smoke` |

A timeout-only retry remains attributable through its new invocation and
attempt metadata. It is not relabeled as semantic repair unless the scheduler
records an explicit repair reason.

The sole closed real-provider inventory is the
[Supported Invocation Inventory](#supported-invocation-inventory) below. Each
inventory entry must cross the shared lifecycle primitive with an explicit
stage and non-empty runtime execution session identity. A new tracked
real-provider entry point is unsupported until that table and its deterministic
coverage fixture are updated. Test fakes using the same boundary remain
`not_applicable_adapter`; an external Codex process launched outside AgentTeam
is outside this contract.

### Token Counting

- Provider-reported values are authoritative.
- `input_tokens` is stored as the provider reports it. Do not subtract
  `cached_input_tokens`.
- `cached_input_tokens` is a subset/diagnostic field unless the provider
  explicitly documents otherwise. Do not add it to `total_tokens`.
- `reasoning_tokens` is displayed separately. Do not add it to total when the
  provider already includes it in output or total usage.
- Prefer the final cumulative terminal usage payload for one invocation.
- Do not sum repeated cumulative JSONL snapshots.
- Verify whether a provider payload is scoped to the current invocation or the
  whole resumed provider session. Do not assume those scopes are equivalent.
- If the provider reports a session-cumulative snapshot across resumed
  invocations, derive the current invocation delta only when the previous
  authoritative snapshot for the same `provider_session_id` and declared
  `provider_predecessor_invocation_id` exists, every counter is monotonic, an
  AgentTeam cross-run lock owns the explicit provider session, and
  provider-issued turn/sequence linkage proves that no unseen turn occurred.
  Mark the accounting method `session_delta` and retain the current compact
  provider snapshot and turn linkage in the record.
- If a resumed session's prior cumulative snapshot is missing or counters move
  backwards, mark usage partial or unavailable rather than treating the full
  session total as the current invocation. Preserve that cumulative payload
  only in `provider_usage_snapshot`; all per-invocation token fields remain
  null because a session total is neither an attributable value nor a lower
  bound for the current invocation.
- A provider session using cumulative accounting has one active writer.
  Calls explicitly resuming the same provider session use a cross-process,
  cross-run lock in the user-level AgentTeam runtime root. The lock key is a
  canonical digest of backend, credential/account namespace, provider session
  store identity, and provider session ID; raw credentials never enter a path
  or record. On first use, that namespace is durably bound to one project
  identity under the same lock. A later cross-project resume is rejected
  before provider launch. The authoritative predecessor is selected only by
  scanning lifecycle files and canonical invocation events in the bound
  project's registered authority roots; the SQLite projection and current-run
  dispatch order are never predecessor authority.
- `resume_last` is single-flight in a domain formed from the bound project,
  backend, credential/account namespace, and provider session store identity.
  It may retain invocation-scoped usage, but local serialization does not make
  a session-cumulative delta authoritative because the concrete predecessor is
  unknown before provider output identifies the session. Concurrent, forked,
  or unseen provider turns make cumulative records partial or unavailable with
  `provider_session_lineage_ambiguous`; no branch is silently chosen.
- Invocation-scoped provider usage does not require session-delta subtraction,
  but provider-session identity is still retained when available.
- Do not sum ambiguous `usage_delta` payloads unless the provider event
  contract explicitly identifies them as non-overlapping deltas.
- Prompt byte counts and `ceil(count/4)` diagnostics remain estimates and never
  enter provider token totals.
- A record is `reported` when the provider supplies the final input, output,
  and total fields required for scoring. Missing optional cached or reasoning
  fields do not make it partial.
- A record is `partial` when a provider payload exists but the scoring fields
  are incomplete.
- A supported Codex invocation with no interpretable provider payload is
  `unavailable`.
- Fake and shell paths crossing the same invocation boundary are
  `not_applicable` and excluded from model-usage coverage.

Exact totals and partial evidence remain separate:

- `reported_token_totals` sum token fields only from `usage_status: reported`;
- when token coverage is below 100%, those values are exact for the reported
  subset but must not be labeled the full-run token cost;
- partial records never enter an exact total, even when some token fields are
  present;
- known fields from partial records are aggregated separately as
  `partial_known_token_lower_bounds` only when the provider contract proves
  those fields are scoped to the current invocation. Session-cumulative or
  lineage-ambiguous snapshots never contribute to these lower bounds and keep
  per-invocation token fields null;
- `observed_token_lower_bound` may add reported totals and known partial fields
  for diagnostics, but is labeled a lower bound and is not a benchmark score,
  budget truth, or substitute for provider-complete usage.

Phase 1 exposes two coverage measures:

```text
lifecycle_terminal_coverage =
unique supported starts with any valid terminal record
/
all unique supported starts

token_usage_coverage =
unique supported starts with a reported terminal usage record
/
all unique supported starts
```

Both denominators contain starts whose `coverage_class` is
`supported_model_invocation`. Partial and unavailable invocations satisfy
lifecycle coverage but not token coverage. Open invocations satisfy neither
numerator and remain in both denominators. Not-applicable operations are
excluded from both. Reports also
expose `open_invocations` separately so active work is not mislabeled as
unavailable. Positive deterministic and live acceptance require both coverage
measures to be 100% and `open_invocations == 0`; dedicated negative recovery
fixtures assert their own lower expected token coverage rather than being mixed
into the positive acceptance run.

### Bootstrap Boundary

The AgentTeam run that implements Phase 1 starts under the pre-Phase-1 runtime.
Its taskpack-author and early worker calls cannot be made retroactively complete
under `model_invocation_usage.v1`. Therefore:

- the implementation run's legacy usage is useful diagnostic evidence but is
  not the Phase 1 acceptance fixture;
- after P1-05 is integrated and verified, P1-06A must launch a new bounded
  fixture with the candidate runtime from the Phase 1 integration baseline;
- the candidate positive fixture, not the bootstrap implementation run, must
  reach 100% lifecycle and token coverage;
- the gated live smoke must also use the candidate runtime;
- running the candidate from the integration worktree does not authorize source
  merge or release activation.

This prevents Phase 1 from failing for impossible historical coverage while
also preventing old incomplete usage from being presented as new evidence.

### Authority And Storage

Files remain authoritative; SQLite remains rebuildable.

1. Per-invocation `started.json` and `terminal.json` lifecycle files are the
   primary capture authority. Their location is runtime-owned and outside the
   target source tree.
2. The scheduler imports worker and scheduler-owned development-smoke
   lifecycle files as canonical
   `model_invocation_started` and `model_invocation_usage_recorded` events.
   Events are replayable run authority; duplicate imports preserve IDs and
   content.
3. Taskpack-author lifecycle files are stored under the author context.
   `author_result.json` carries the terminal record or a reference for
   compatibility, but it does not replace a missing start record. This
   lifecycle subtree is non-rebuildable accounting authority and is excluded
   from ordinary draft/taskpack garbage collection. Destructive removal
   requires an explicit operator action that reports canonical-import status
   and any accounting evidence that would be lost.
4. When an authored taskpack starts a run, its start and terminal records are
   referenced by one bounded
   `<run_dir>/state/author_lifecycle_bootstrap.v1.json` whose paths are
   normalized relative to the configured work root and whose record digests are
   fixed before scheduler launch. The scheduler validates that bootstrap and
   imports the records into the run event log with the same IDs before worker
   execution. This keeps successful-run accounting durable even if draft
   diagnostics are later removed; the bootstrap is transport metadata, not a
   second token authority.
5. Author records use the planned `taskpack_id` as `run_id`, because the
   current runtime uses the frozen taskpack ID as the run directory ID. A
   failed author still remains queryable as a planned run that never started.
6. Runtime-diagnostic and controller-owned development-smoke lifecycle files
   live only below
   `<run_dir>/state/controller_invocations/<stage>/<runtime_execution_session_id>/model_invocations/`.
   The run identity and a durable controller claim register that fixed root
   before provider launch. Normal completion imports the records through the
   same locked, conflict-detecting append primitive into that run's canonical
   event log; a crash may leave unresolved files for recovery and projection.
   Arbitrary external directories are not authority.
7. Pursue context is passed explicitly when available. Projection may enrich
   old or initially incomplete records from pursue recap files, but enrichment
   must not change record identity or token counts.
8. Legacy `token_usage` summaries remain readable for compatibility, but they
   are not benchmark-counted invocation records.
9. Projection scans canonical lifecycle events, unresolved worker lifecycle
   files, author lifecycle directories, registered unresolved diagnostic and
   development-smoke controller roots, and controller-owned acceptance
   artifacts. It deduplicates starts by `invocation_id`, terminal records by
   `usage_event_id`, and raises on conflicting content.
10. The controller-owned live-smoke artifact is authoritative for its one
   acceptance invocation only after controller validation passes and the final
   artifact is atomically published. It is projected under a dedicated
   acceptance run ID.
11. No prose trace file is created per invocation.

## Supported Invocation Inventory

Phase 1 must maintain this inventory in code tests. Adding a new live model
entry point later requires adding it to the inventory and either capturing its
usage or failing validation.

| Entry point | Current implementation | Required terminal record |
| --- | --- | --- |
| Taskpack author | `_run_codex_author_command()` | Durable start plus terminal author record for success, failure, timeout, and accepted timeout salvage. |
| Initial implementation worker | `CodexRuntimeAdapter.run()` | Durable start plus terminal record with task, attempt, agent, role, stage, backend, and model. |
| Planner/task slicer | Planner prompt branch in `CodexRuntimeAdapter` | Same event contract with planner stage. |
| Repo-map worker | Role-routed `CodexRuntimeAdapter` | Same event contract with repo-map stage. |
| Semantic architecture worker | Role-routed `CodexRuntimeAdapter` | Same event contract with semantic architecture stage. |
| Review or repair attempt | Role/work-type/retry-routed adapter call | New invocation record with repair stage and original task lineage. |
| Follow-up author | `_run_pursue_loop()` and follow-up submit path | Author result record with pursue and round correlation. |
| Runtime diagnostic | `agentteam chat --interactive` / `run_runtime_diagnostic_chat()` | Registered controller lifecycle root, `runtime_diagnostic` stage, durable controller owner, and canonical import into the selected run; usage may be unavailable when the interactive provider surface emits no machine-readable total. |
| Development smoke: basic | `live_codex_smoke.py` | Registered lifecycle plus canonical run events with `development_smoke` stage. |
| Development smoke: scheduler | `live_codex_scheduler_smoke.py` | Registered lifecycle plus canonical run events with `development_smoke` stage. |
| Development smoke: repo context | `live_codex_repo_context_smoke.py` | Registered lifecycle plus canonical run events with `development_smoke` stage. |
| Development smoke: pipeline | `live_codex_pipeline_smoke.py` | Registered lifecycle plus canonical run events with `development_smoke` stage. |
| Development smoke: multifile pipeline | `live_codex_multifile_pipeline_smoke.py` | Registered lifecycle plus canonical run events with `development_smoke` stage. |
| Development smoke: CLI | `live_codex_cli_smoke.py` | Registered lifecycle plus canonical run events with `development_smoke` stage. |
| Candidate-runtime live smoke | `usage_live_smoke.py` through `CodexRuntimeAdapter` | Controller artifact with `acceptance_live_smoke` stage and dedicated acceptance run ID. |
| Fake/shell adapter at model boundary | Runtime adapter alternatives | `not_applicable`; excluded from coverage. |

Notifications, Git operations, verification commands, SQLite projection, repo
grounding, and deterministic taskpack skeleton generation are not model calls.

## Task Graph

```text
PRE-00/PRE-01/PRE-02/PRE-03A-D/PRE-04 runtime prerequisites, operator merge, and active release
  -> P0-A readiness contract and fail-closed pilot guard
  -> P1-00 contract review and operator approval (before freeze)
  -> clean tracked source commit and G0-G5 verification
  -> P1-01 schema and parser
  -> P1-02A worker adapter terminal capture
  -> P1-02B mailbox, stage, and scheduler propagation
  -> P1-03 author and follow-up capture
  -> P1-04A canonical event and replay
  -> P1-04B report and notification aggregation
  -> P1-05 projection and stats
  -> P1-06A deterministic end-to-end verification
  -> P1-06B acceptance helper and deterministic controller
  -> P1-06C milestone report renderer and finalizer
  -> P1-06D operator documentation
  -> P1-LIVE controller-run candidate live gate (outside backlog)
  -> P1-06E controller-generated milestone report (outside backlog)
  -> operator integration review
```

Phase 1A contains the pre-freeze P1-00 review and P1-01 through P1-04B. It is
the capture, identity, authority, and replay gate. Phase 1B contains P1-05
through P1-06E and is the projection, query, and end-to-end verification gate.
Phase 1B must consume a verified Phase 1A integration baseline; it must not
compensate for incomplete capture inside SQL or report rendering.

Use one current integration baseline during frozen-backlog execution. Every
later task starts from the verified integration head containing its
dependencies, not from the original source HEAD. A post-backlog target
divergence does not mutate that history: PRE-03D creates a new gate-epoch
branch from the prior `validated_code_sha`, verifies it, and atomically makes
that branch the one current integration baseline.

## Execution Preflight

The executable taskpack must not be frozen or run until all gates pass. PRE-02
may perform a non-freezable dry materialization in an isolated temporary root
to verify G3 before the operator approval record exists.

### G0: Tracked Clean Source And Host Capability

- this plan, its blueprint, the execution-preflight plan, approved research
  authority, umbrella route, roadmap, README links, review schema, and approved
  review record are tracked in the source commit used by attempt worktrees;
- `git status --short` is clean before retained materialization and freeze;
- the Phase 1 source commit Git OID and object format, active preflight runtime
  release ID, and that
  release's `source_commit` are recorded in taskpack context;
- the active pre-Phase-1 release ID and `source_commit` exactly match the
  release reviewed in the approval record, and that source commit is an
  ancestor of the Phase 1 source commit;
- the reviewed PRE-04 integration commit is an ancestor of that active release
  source commit. P0-A and later accepted readiness corrections may therefore
  sit between PRE-04 and the approval-reviewed release.
- `agentteam doctor --invocation-supervision-probe` from the reviewed PRE-00
  release confirms, without a provider call, Linux `pidfd_open`, `loginctl`
  linger enabled, a usable systemd user manager, its enclosing system-level
  `user@<uid>.service` identity and control-group kill semantics, and
  creation/query/stop of a unique transient oneshot service with
  `RemainAfterExit=yes` and `KillMode=control-group`, stable
  `InvocationID`/`ControlGroup` reporting, and bounded cleanup. Phase 1 does
  not fall back to PGID-only live execution when this probe fails.

The current author/materializer paths reject a dirty target repository, and
untracked plans are invisible in new Git worktrees. Do not work around this by
copying untracked authority files into attempt directories.

### G1: Contract Review Before Freeze

P1-00 is a pre-freeze semantic architecture and operator review, not a frozen
backlog item. Any accepted contract correction must be committed before
materializing the execution taskpack. Frozen taskpacks remain immutable; a
post-freeze review cannot silently rewrite the contract consumed by later
workers.

The fixed approval record is:

`experiments/native_agentteam_runtime/implementation_artifacts/reviews/phase1-model-invocation-usage-contract-review.v1.json`

It conforms to:

`experiments/native_agentteam_runtime/schemas/phase1_contract_review.schema.json`

It records at least:

```text
schema_version
decision
reviewer
reviewed_at
git_object_format
preflight_release_id
preflight_release_source_commit
pre04_integration_commit
plan_sha256
blueprint_sha256
research_authority_sha256
review_schema_sha256
stage_vocabulary_sha256
contract_decisions_sha256
resolved_contradictions
remaining_escalations
```

File digests, including `review_schema_sha256`, are SHA-256 over exact tracked
bytes. The stage-vocabulary and
contract-decision digests are SHA-256 over the corresponding blueprint value
serialized as UTF-8 JSON with sorted keys, no insignificant whitespace, and
ASCII escaping. The blueprint's `contract` object is the machine-readable
value being approved; this Markdown section is its human-readable explanation.

Only the operator can set `decision` to `approved`. G1 fails when the record is
missing, pending, rejected, contains an escalation, or its recorded digests do
not match the tracked source. The release source commit must match the OID
length declared by `git_object_format`, which is read from
`git rev-parse --show-object-format`; it is not a SHA-256 file digest. G1 also
fails when the active release ID or `source_commit` differs from the bound
preflight release, when the bound release source is not an ancestor of the
materialization source commit, when `pre04_integration_commit` is not a valid
commit, or when it is not an ancestor of the bound release source. A semantic
architecture agent may prepare the review and proposed resolutions, but cannot
approve its own contract.

### G2: Verified Dependency Dispatch

The current scheduler can accept a worker result, fail integration
verification, roll back the integration baseline, and still mark the backlog
task `done`. Before using one multi-task frozen package, verify that the active
runtime release blocks downstream dependency dispatch unless the required patch
is present in a verified integration-baseline commit.

This repository is known to fail that check. Complete PRE-01 in
[`2026-07-24-phase1-execution-preflight.md`](2026-07-24-phase1-execution-preflight.md),
then operator-review, merge, and activate the repaired runtime release before
G0 records the clean Phase 1 source commit.

The final active preflight release, not merely an earlier intermediate release,
must be used for this check.

Do not substitute separate single-item packages. Their internal `depends_on`
values cannot reference tasks outside the package, and the public
`agentteam run` surface does not currently expose the lower-level initial
integration-base parameter. Phase 1 execution is blocked until the scheduler
prerequisite is verified in the active release.

### G3: Deterministic Multi-Item Materialization

The current semantic materializer consumes one semantic object and replaces the
backlog with `items[0]`. It cannot faithfully produce this dependency graph.
Complete PRE-02 in the execution-preflight plan before freezing Phase 1.

The tracked machine source is:

`2026-07-23-phase1-model-invocation-usage.blueprint.json`

G3 passes only when a deterministic materializer:

- validates the blueprint schema, exact task IDs, dependency DAG, roles, risk
  levels, relative scopes, deliverables, and verification command;
- writes all five runtime taskpack files without a model call;
- preserves every declared task and dependency;
- records source-plan and blueprint SHA-256 values in the generated context;
- records the approval-bound runtime release ID, source commit, and Git object
  format in retained and frozen context;
- validates the blueprint-bound approval record and all declared digests before
  any retained materialization or freeze;
- validates the generated package with the normal taskpack validator;
- emits a materialization manifest whose task IDs and edges exactly match the
  blueprint;
- fails closed on unknown fields, absolute source paths, missing dependencies,
  cycles, or one-item truncation.

PRE-02 is a prerequisite capability, not permission to freeze this taskpack.
The operator must still approve G1 and the exact generated package.

### G4: Enforced Post-Backlog Gates

The current `agentteam integrate` path checks idle/completed run state but does
not enforce blueprint gates outside the backlog. Complete PRE-03A through
PRE-03D in the
execution-preflight plan before freezing Phase 1.

G4 passes only when the active runtime:

- preserves blueprint `post_backlog_gates` in the generated taskpack and run
  state;
- initializes `awaiting_validated_baseline`, then seals gate epoch 1 with
  P1-LIVE and P1-06E pending only after verified-idle full verification passes;
- supports immutable gate-evidence registration without allowing registration
  itself to claim success;
- derives gate status by validating the registered controller artifact,
  schema, status field, and integration-head relation;
- requires a separate immutable, interactive, digest-bound operator approval
  before any gate declared `operator_review_required` can pass;
- loads gate schema bytes from the freshly resolved integration commit with
  `git show`, binds their digest, and rejects dirty schema paths;
- distinguishes verified backlog completion from milestone completion and
  emits `run_completed` only after all gates pass;
- keeps terminal, Feishu, status, report, paths, diff, and review commands on
  one freshly resolved integration branch head and prevents them from
  recommending integration while either gate is pending, awaiting operator
  review, or failed;
- makes `agentteam integrate` fail closed until every required gate passes;
- rejects `agentteam integrate --rebase` for a commit-bound gated run because
  rebase would invalidate the accepted commit evidence;
- can recover from target-branch divergence by creating a new immutable gate
  epoch and integration branch from the prior epoch's `validated_code_sha`,
  merging the freshly resolved target head, rerunning the frozen full
  verification, and resetting both gates to pending without rewriting or
  deleting prior epoch evidence;
- requires every controller claim, artifact, and receipt to name the current
  gate epoch, and rejects stale-epoch publication or registration;
- has no ordinary `--force` path that bypasses a declared post-backlog gate.

P1-LIVE must prove its `validated_code_sha` is an ancestor of the eventual
integration head. P1-06E must prove its `final_report_sha` equals that head and
retain `awaiting_operator_review` until the PRE-03A approval record matches.
Manual Git operations outside AgentTeam remain operator responsibility, but the
framework must not suggest or perform an integration that violates these
declared gates.

### G5: Immutable Runtime Release Binding

The active release used for freeze is not enough: the current launcher can
resolve a different active release on `continue`, and existing run state records
the release only after execution. Complete PRE-04 before freezing Phase 1.

G5 passes only when:

- the frozen taskpack carries the approval-bound release ID, source commit, and
  Git object format;
- the launcher exports the identity of the runtime it actually loaded;
- a new run atomically writes an immutable release binding before its first
  scheduler/runtime subprocess;
- every run writes a schema-valid `run_identity.v1.json`; default latest-run
  selection includes `run_kind: implementation`, excludes controller evidence
  runs, and uses the unique project creation sequence rather than mtimes or
  names;
- the launcher alone resolves implicit latest before runtime import and passes
  the selected identity digest for runtime validation without reselection;
- the launcher resolves an existing run's binding before importing runtime code
  for `continue`;
- manifest or source-commit mismatch fails before any worker/provider action;
- release garbage collection protects every valid bound release;
- changing the active release affects future runs only;
- the reviewed launcher is installed into the actual machine command path and
  its digest plus bound-continuation behavior are verified there.

An approved Phase 1 run cannot use a legacy release-adoption fallback.

## Runtime Taskpack Materialization

The logical L2 milestone is deliberately materialized as bounded L0/L1 runtime
items. This avoids inventing a repo-map artifact for a repository whose exact
entry points are already frozen above, avoids the current `.agentteam/`
cross-worktree propagation risk, and follows the established policy that the
milestone controller owns cross-task evidence.

After PRE-02 is active, materialize this blueprint into one runtime five-file
package:

- preserve the blueprint taskpack ID `phase1-model-invocation-usage`; do not
  apply a taskpack-ID override to this approved milestone;
- include `implementation_worker` in the execution agent pool with a Codex role
  profile;
- materialize P1-01, P1-02A, P1-02B, P1-03, P1-04A, P1-04B, P1-05, P1-06A,
  P1-06B, P1-06C, and P1-06D in one frozen package with the dependency chain
  declared above;
- use the repository grounding section of this tracked plan as bounded read
  context instead of dispatching another repo-map model call;
- preserve each blueprint task's acceptance criteria, stop condition, and
  blueprint `input_artifacts` reference so the existing worker prompt requires
  the machine contract to be read;
- preserve both blueprint `post_backlog_gates` and initialize their run-state
  receipts as pending;
- keep execution items within three write-scope entries except these
  digest-bound exact-path cases:
  - P1-01 uses seven entries so schema and parser changes remain explicit
    instead of granting directory-wide access over approved schemas;
  - P1-03 uses eleven entries because the author, diagnostic, and six tracked
    development-smoke callers must cross one shared lifecycle boundary in the
    same verified change, with two focused test files;
  - P1-04A uses five entries because the shared importer, scheduler,
    diagnostic caller, and their two focused test files must adopt one
    conflict-detection rule atomically;
- these exceptions remain `risk_target: L1` because every production path is
  enumerated, only the existing test-only directory scope is allowed, no
  production directory or wildcard scope is granted, and integration requires
  the full verification command; all other tasks retain the three-entry
  maximum and their specified L0/L1 risk;
- give each item bounded read access to its files, direct callers, tests, and
  the two authority documents;
- keep `policy.allow_merge` false;
- preserve the approval-bound release identity so PRE-04 can bind it before
  runtime launch;
- encode verification commands as JSON string arrays; do not put `env` or a
  shell pipeline in `verification.command`;
- let the integration runner supply the existing native-runtime `PYTHONPATH`;
- require P1-LIVE and P1-06E after the backlog reaches verified idle and before
  any source integration command is suggested.

Generated packages are execution artifacts under the configured work root; do
not commit packages containing absolute project or worktree paths into this
repository.

The operator compares the generated materialization manifest to the tracked
blueprint before freeze. Markdown section parsing is not part of
materialization; the JSON blueprint contains the complete executable fields,
while this document remains the human-readable semantic authority.

## P1-00 Contract Review

**Risk:** L3
**Role:** `semantic_architecture_agent`
**Write scope:** this plan when a contradiction is found; the fixed review
record after operator decision
**Execution location:** pre-freeze review, outside the frozen runtime taskpack

### Objective

Check that invocation identity, authority, stage routing, token semantics,
coverage, and projection rules are internally consistent with the approved
research document and the file-authoritative runtime.

### Required Deliverables

- `contract_review_summary`
- `resolved_or_escalated_contradictions`
- `frozen_stage_vocabulary`
- `authority_and_identity_confirmation`
- `implementation_handoff`
- `digest_bound_operator_decision`

### Acceptance

- no unresolved ambiguity remains about cumulative versus delta counting;
- author records and worker records have one declared authority path each;
- repeated provider execution and repeated artifact replay are distinguished;
- benchmark coverage denominator is mechanically defined;
- runtime and provider sessions are distinct and resumed-session lineage has a
  single-writer rule;
- the review record digests match the tracked plan, blueprint, stage
  vocabulary, contract decisions, and research authority;
- the review record binds the active pre-Phase-1 release ID, Git object format,
  and source commit, and records that PRE-04 is its verified ancestor;
- the operator, not the reviewing agent, records `decision: approved`;
- any change to the approved research claims is escalated to the operator.

### Stop Condition

Stop before runtime edits if the reviewer concludes that the proposed record
authority conflicts with the approved research authority or existing immutable
event semantics. This is the only architecture gate inside Phase 1.

## P1-01 Schema And Parser

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-00 approval and G0-G5 preflight

### Objective

Freeze the machine-valid record shape and parse provider JSONL without
double-counting cumulative payloads or guessing ambiguous values.

### Files

- Add:
  `experiments/native_agentteam_runtime/schemas/model_invocation_started.schema.json`
- Add:
  `experiments/native_agentteam_runtime/schemas/model_invocation_writer_revoked.schema.json`
- Add: `experiments/native_agentteam_runtime/schemas/model_invocation_usage.schema.json`
- Add:
  `experiments/native_agentteam_runtime/schemas/model_invocation_live_smoke.schema.json`
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/token_usage.py`
- Test scope: `experiments/native_agentteam_runtime/m0_runtime/tests/`,
  including `test_m0_runtime.py` and sanitized fixtures

Materialize these as seven exact write-scope entries: the four new schema files,
the exact `event.schema.json` and `token_usage.py` paths, and
`m0_runtime/tests/`. Do not grant P1-01
write access to the whole `schemas/` directory.

### Required Deliverables

- `model_invocation_started_v1_schema`
- `model_invocation_writer_revoked_v1_schema`
- `model_invocation_usage_v1_schema`
- `model_invocation_live_smoke_v1_schema`
- `canonical_invocation_event_types`
- `terminal_usage_parser_contract`
- `provider_session_lineage_contract`
- `execution_group_identity_contract`
- `parser_no_double_count_evidence`
- `verification_summary`

### Steps

- [ ] Add failing tests for stable invocation/usage IDs, valid start and
  terminal records, exact-owner revocations, invalid enums, negative token
  counts, missing unavailable reasons, immutable coverage class, and UTC times.
- [ ] Require every supported live start to bind a systemd transient-service
  unit, `InvocationID`, user-manager identity, and `ControlGroup`; fake and
  shell adapters remain not applicable.
- [ ] Define the fixed controller-owned live-smoke result shape used by the
  final acceptance gate.
- [ ] Require acceptance start, terminal, and fixed live-smoke records to carry
  the logical implementation run ID and positive gate epoch while ordinary
  invocation records keep those fields null.
- [ ] Add `model_invocation_started`, `model_invocation_writer_revoked`, and
  `model_invocation_usage_recorded` to the canonical event enum before any
  later scheduler task emits those events.
- [ ] Add sanitized JSONL fixtures representing the installed Codex terminal
  usage shape, repeated cumulative snapshots, malformed lines, missing usage,
  and partial usage.
- [ ] Replace the "last recursive candidate wins" behavior with an explicit
  terminal usage selection contract.
- [ ] Add resumed-session fixtures proving that per-invocation payloads and
  session-cumulative snapshots cannot be confused.
- [ ] Keep runtime execution session, provider session, and provider
  predecessor identity separate in schemas and parser inputs.
- [ ] Reject cumulative session-delta accounting when predecessor lineage is
  missing, concurrent, forked, non-monotonic, lacks provider-issued turn
  continuity, or comes from `resume_last`.
- [ ] Preserve the existing `normalize_token_usage()` and
  `aggregate_token_usage()` compatibility surface where practical.
- [ ] Add record construction and validation helpers without introducing a
  prose artifact.
- [ ] Prove cached and reasoning fields do not inflate total usage.

### Acceptance

- a repeated cumulative fixture yields one final usage total;
- a start record validates without token fields and joins to its terminal
  record by `invocation_id`;
- a supported live start cannot validate without its exact execution-group
  identity, while a not-applicable adapter does not invent one;
- a revocation record binds one invocation and exact owner token and cannot be
  rewritten for a different owner;
- live-smoke commit fields validate as Git OIDs for their declared
  `git_object_format`, not as fixed 64-character file digests;
- an ambiguous delta-only fixture is partial or unavailable rather than
  guessed;
- a lineage-ambiguous session-cumulative snapshot keeps per-invocation token
  fields null and cannot enter partial lower-bound aggregates;
- resumed-session fixtures either use provider invocation totals or a validated
  monotonic session delta without assigning prior session cost to a new call;
- an unresolved `resume_last` and two concurrent cumulative invocations for one
  provider session cannot produce `reported` session-delta records;
- explicit resumed session deltas require a matching provider-issued
  predecessor turn; a local dispatch order alone is insufficient;
- replaying the same JSONL produces byte-equivalent normalized usage;
- malformed unrelated output does not crash parsing;
- legacy unit tests continue to pass.

### Task-Local Stop Condition

Stop before adapter changes if the installed provider fixture cannot identify a
terminal cumulative payload without guessing.

## P1-02A Worker Adapter Lifecycle Capture

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-01

### Objective

Make the Codex adapter persist one correlated start record before every
provider subprocess launch attempt and produce one terminal usage record for
every controlled terminal outcome.

### Files

- Add:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/model_invocation.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

### Required Deliverables

- `worker_terminal_usage_capture`
- `worker_durable_start_capture`
- `shared_supervisor_and_terminalization_primitive`
- `terminal_path_coverage`
- `systemd_execution_group_fencing_evidence`
- `resumed_session_delta_evidence`
- `verification_summary`

### Steps

- [ ] Allocate invocation identity, create a gated supervisor that cannot yet
  launch the provider, and atomically persist `started.json` before granting
  its one-shot launch permit.
- [ ] Launch that supervisor only through the uniquely named systemd user
  transient-service contract, verify its `InvocationID`, manager identity, and
  `ControlGroup` plus the lingering enclosing user-service identity, and reject
  PGID-only fallback.
- [ ] Bind the start to the current worker/author ownership token and persist
  the supervisor PID/PGID, host boot ID, process start ticks, and launch nonce
  digest. Flush that identity before the supervisor may launch the provider in
  its owned process group.
- [ ] Accept explicit project/run/taskpack/pursue/round/task/attempt/agent/
  role/stage/backend/model context, `runtime_execution_session_id`, explicit
  provider-resume metadata, and an optional authoritative predecessor
  invocation and provider snapshot.
- [ ] Set coverage class before launch from the structured adapter boundary;
  terminal outcome cannot reclassify a supported call as not applicable.
- [ ] Route every completed, failed, blocked, timeout, cancelled,
  launch-failed, missing-result, and invalid-result return through one
  shared terminal finalizer that owns `terminal.lock`, checks owner revocation,
  and performs exclusive publication.
- [ ] Parse bounded JSONL output on every terminal path and retain usage emitted
  before a later failure or timeout.
- [ ] Preserve stdout/stderr evidence paths and bounded excerpts; do not embed
  unbounded logs in returned state.
- [ ] Apply session-delta accounting only when the supplied prior snapshot is
  authoritative, belongs to the declared provider predecessor, and is
  monotonic.
- [ ] Add inventory tests that fail when a controlled supported adapter path
  returns without a terminal record.
- [ ] Add a crash-boundary test proving that a failure after `started.json` is
  published but before terminal finalization leaves a discoverable open
  invocation rather than erasing the call.
- [ ] Add exact launch-handshake crash tests: parent death before durable start
  closes the permit channel and launches no provider; death after durable start
  but before permit becomes one recoverable `launch_failed`; death after permit
  leaves enough process identity to fence the provider without signaling a
  reused PID.
- [ ] Add recovery-boundary tests for a live pidfd, `ESRCH` after supervisor
  reap with the exact service group empty, a populated service group requiring
  exact-unit stop, changed host boot ID, a proved enclosing user-service
  restart, and ambiguous/mismatched inner-manager identity.
- [ ] Add a terminal race test in which normal finalization and recovery
  contend: either the normal terminal commits before revocation and is retained,
  or recovery revokes first and the old writer cannot publish.

### Acceptance

- every provider launch attempt has exactly one durable start record;
- no provider can launch between supervisor creation and durable publication
  of its PID/PGID, boot/start identity, and transient-service identity;
- every controlled return has exactly one model-invocation terminal record;
- terminal records identify their writer and owner token;
- normal and recovery finalizers share one lock/revocation primitive and cannot
  create competing terminal authority;
- a timeout after a valid usage event preserves usage and reports timeout
  independently;
- a timeout before usage reports unavailable with a reason;
- retries create distinct invocation IDs;
- simulated crashes on both sides of the launch permit either prove that no
  provider launched and close once as `launch_failed`, or preserve a fenceable
  start for scheduler recovery and coverage;
- a reaped supervisor can terminalize only from changed-boot proof or an exact
  verified transient service whose control group is empty; ambiguous group
  state remains open;
- logout does not end the lingering manager, while a completed enclosing
  user-service restart proves the prior manager lifetime dead without
  signaling any process in the new lifetime;
- a resumed invocation without a trustworthy prior session snapshot is partial
  or unavailable rather than overcounted;
- fake/shell adapters remain explicitly not applicable when tested at the same
  invocation boundary.

### Task-Local Stop Condition

Stop if invocation and execution-group identity cannot be made durable before
launch, if the host lacks the required pidfd/systemd authority, or if terminal
usage capture requires storing unbounded subprocess logs.

## P1-02B Mailbox, Stage, And Scheduler Propagation

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-02A

### Objective

Propagate invocation context and records through mailbox workers and scheduler
collection without changing identity during replay.

### Files

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

### Required Deliverables

- `supported_worker_invocation_inventory`
- `role_and_stage_routing_evidence`
- `outbox_identity_replay_evidence`
- `orphaned_invocation_reconciliation`
- `provider_session_single_writer_evidence`
- `verification_summary`

### Steps

- [ ] Put explicit project/run/taskpack/pursue/round/task/attempt/agent/role/
  stage/backend/model and runtime execution session context in the mailbox
  payload.
- [ ] Route stages from structured task, role, and retry fields, never prompt
  text.
- [ ] Resolve explicit provider resume IDs and `resume_last` into a concrete
  provider session and predecessor invocation when possible; never substitute
  the AgentTeam runtime execution session.
- [ ] Propagate an optional prior provider snapshot and predecessor from
  structured scheduler context to the adapter without changing either value.
- [ ] Serialize explicit cumulative-accounting invocations with the user-level
  provider-session namespace lock, atomically establish or verify its immutable
  project binding, and reject cross-project or known concurrent/forked writers
  before dispatch.
- [ ] Resolve explicit predecessors from lifecycle files and canonical events
  in the bound project's registered authority roots. Keep unresolved
  `resume_last` calls single-flight per provider resume domain without treating
  that local order, the projection DB, or current-run event order as
  authoritative delta lineage.
- [ ] Propagate the structured record through mailbox outbox and scheduler
  collection.
- [ ] Reconcile lifecycle directories during collection and recovery. Once the
  owner is durably revoked and the owned worker/provider process group is
  confirmed dead, reread terminal evidence and close a still-orphaned start
  with one exclusive recovery terminal. Preserve valid bounded-spool usage;
  otherwise use unavailable with a bounded reason.
- [ ] Emit the P1-01-declared writer-revocation event before recovery
  terminalization. Under the shared terminal lock, publish `revoked.json`,
  apply the changed-boot/live-pidfd/reaped-supervisor decision tree, and act
  only on the pinned pidfd or exact verified transient service; direct numeric
  PID/PGID signaling is forbidden.
- [ ] Treat lease expiry as a liveness signal only. It may trigger stop and
  fencing, but cannot by itself authorize terminal recovery.
- [ ] Preserve the same invocation and usage event IDs when an outbox result is
  replayed.
- [ ] Cover planner, repo-map, implementation, semantic architecture, and
  review/repair worker routes in a mechanically checked inventory.

### Acceptance

- every supported worker route supplies a declared stage and terminal record;
- an injected prior snapshot and predecessor reach the adapter unchanged;
- replay preserves the original invocation and usage event IDs;
- a worker death between start and terminal becomes one recovered terminal
  invocation, reported only when bounded provider evidence supports it, and
  does not disappear from the denominator;
- an invocation remains open, rather than falsely unavailable, while its
  process or lease is still demonstrably live;
- lease expiry with a live provider process remains open until durable owner
  revocation, process-group termination, and wait complete;
- a real terminal racing with revocation is reread and retained rather than
  overwritten by recovery;
- a supervisor that exits after identity comparison but before signaling cannot
  redirect recovery onto a reused PID, and a mismatched pidfd identity is never
  signaled;
- `ESRCH` alone never proves death: same-boot recovery requires the exact
  transient service group to be empty, while changed-boot recovery sends no
  signal;
- a changed enclosing user-service `InvocationID` proves death only when its
  completed restart and persisted `KillMode` guarantee old control-group
  cleanup; an unexplained inner-manager replacement remains open;
- two cumulative invocations cannot concurrently claim the same provider
  session lineage;
- two unresolved `resume_last` calls in one provider resume domain cannot launch
  concurrently;
- session-cumulative `resume_last` output remains partial/unavailable, and an
  explicit session delta is reported only with provider-issued turn continuity;
- an unknown role/stage mapping fails explicitly rather than falling back from
  prompt text;
- existing mailbox recovery and scheduler replay tests continue to pass.

### Task-Local Stop Condition

Stop if an optional prior snapshot/predecessor cannot be propagated
byte-equivalently, provider-session serialization cannot be enforced, process
death cannot be fenced and proven for recovery, or mailbox replay would
require minting a new invocation identity.

## P1-03 Supported Non-Worker Invocation Capture

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-02B

### Objective

Capture real provider usage for taskpack authoring, runtime diagnostic chat,
and tracked standalone development-smoke entry points, including failed and
timed-out processes.

### Files

- Read: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/model_invocation.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/diagnostic_chat.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_*.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`

### Required Deliverables

- `taskpack_author_usage_capture`
- `taskpack_author_durable_lifecycle`
- `follow_up_author_stage_and_round_attribution`
- `runtime_diagnostic_lifecycle_capture`
- `development_smoke_lifecycle_capture`
- `closed_supported_invocation_inventory`
- `author_failure_and_timeout_coverage`
- `estimate_separation_evidence`
- `verification_summary`

### Steps

- [ ] Make the Codex author path request machine-readable JSONL while
  preserving custom test command support.
- [ ] Allocate author invocation identity and use the same gated-supervisor
  and transient-service handshake as worker calls: persist its full
  `started.json` process and execution-group identity before issuing the
  provider launch permit; mirror the active ID in `author_state.json` for
  liveness and recovery.
- [ ] Bind author ownership, boot/start process identity, pidfd/systemd
  fencing, and terminal exclusive creation to the same rules as worker
  invocations.
- [ ] Parse the spooled `author_stdout.log` after every terminal outcome.
- [ ] Store one structured record in `author_result.json`; estimates under
  `input_metrics` remain diagnostics only.
- [ ] Preserve usage for normal success, process failure, timeout, and accepted
  timeout salvage.
- [ ] Pass project, planned run/taskpack, effective model, and initial/follow-up
  stage explicitly.
- [ ] Pass pursue ID and round index when known; keep fields null when they
  genuinely do not apply.
- [ ] Assign pursue round 1 to `taskpack_author`, pursue round 2 and later to
  `follow_up_author`, and every `next` author call to `follow_up_author`.
- [ ] Route `agentteam chat --interactive` through the shared lifecycle
  primitive as `runtime_diagnostic`; allocate a controller-owned runtime
  execution session and retain unavailable usage when the interactive provider
  surface emits no machine-readable total.
- [ ] Route every tracked standalone `live_codex_*_smoke` provider launch
  through the same primitive as `development_smoke`; do not let a legacy smoke
  bypass the supported invocation inventory.
- [ ] Make the accepted author record available to runtime startup for
  idempotent import by writing the bounded digest-bound
  `author_lifecycle_bootstrap.v1.json` below the selected run before scheduler
  launch. Paths must be relative to the configured work root and remain inside
  the author context authority root.
- [ ] Ensure failed author attempts remain discoverable by project stats even
  when no frozen taskpack or run directory exists.
- [ ] Keep author lifecycle authority out of ordinary draft/taskpack deletion;
  destructive cleanup must be explicit, report canonical-import status, and
  never silently change project token history.
- [ ] On author restart, retain a still-live invocation as open; when the
  owner is revoked and process death is demonstrable, salvage bounded usage or
  close its orphaned start once as unavailable.

### Acceptance

- author-reported provider usage is not replaced by
  `prompt_estimated_tokens`;
- initial and follow-up author calls have different stages;
- a two-round deterministic pursue fixture attributes round 1 to
  `taskpack_author` and round 2 to `follow_up_author`;
- diagnostic and every tracked development-smoke entry point have lifecycle
  coverage, explicit stage, and a controller-owned runtime execution session;
- a source scan and deterministic fixture agree on the closed supported
  invocation inventory;
- a crash between author start publication and terminal result remains visible
  and is deterministically reconciled;
- author recovery uses the same changed-boot, live-pidfd, and exact
  transient-service proof as worker recovery;
- author timeout diagnostics remain compact;
- ordinary taskpack/draft cleanup preserves author lifecycle authority;
- old author contexts without usage continue to load.

### Task-Local Stop Condition

Stop if a supported non-worker provider entry point cannot cross the shared
lifecycle boundary without breaking its interactive, custom-command, or
timeout-salvage contract.

## P1-04A Canonical Lifecycle Events And Replay

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-03

### Objective

Make invocation usage replayable authority without counting repeated artifacts
as repeated model calls.

### Files

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/model_invocation.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/diagnostic_chat.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Required Deliverables

- `canonical_invocation_started_event`
- `canonical_usage_event`
- `idempotent_replay_accounting`
- `author_record_import_evidence`
- `controller_record_import_evidence`
- `authoritative_snapshot_lookup`
- `verification_summary`

### Steps

- [ ] Use the P1-01-declared `model_invocation_started`,
  `model_invocation_writer_revoked`, and `model_invocation_usage_recorded`
  event types for canonical import.
- [ ] Before worker dispatch, validate the P1-03 author bootstrap's contained
  relative paths and record digests, then import the successful taskpack
  author's start and terminal records into the run event log while preserving
  their original IDs.
- [ ] Import worker start and terminal lifecycle files idempotently during
  normal collection and recovery.
- [ ] Import registered runtime-diagnostic and development-smoke controller
  lifecycle files into their run's canonical event log with the same
  idempotency and content-conflict rules. Preserve unresolved controller files
  after a crash for recovery and direct file projection.
- [ ] Use `model-invocation-started:<invocation_id>` and
  `model-invocation-writer-revoked:<invocation_id>:<owner_token>` as explicit
  replay idempotency keys; terminal events remain keyed by `usage_event_id`.
- [ ] Append one event per collected worker invocation with an idempotency key
  derived from `usage_event_id`.
- [ ] Keep legacy task result `token_usage` fields for compatible readers, but
  treat invocation events as the new accounting source.
- [ ] Deduplicate identical usage records during replay and flag conflicting
  duplicate IDs.
- [ ] Reconstruct the latest compact provider snapshot and unique predecessor
  for a resumed `provider_session_id` from canonical usage events and supply
  both to the next scheduler dispatch.
- [ ] Carry provider-issued turn linkage and refuse to infer a missing
  predecessor turn from event order.
- [ ] Treat concurrent or forked cumulative provider-session lineage as an
  integrity condition that cannot produce a reported session delta.

### Acceptance

- replaying `events.jsonl` twice does not double invocation count or totals;
- replaying a start without a terminal preserves one open invocation;
- replaying start or writer-revocation events is idempotent;
- one task with two real model retries records two invocations;
- one duplicated event record reports one invocation;
- a conflicting duplicate fails a deterministic integrity check;
- importing an author result twice records one canonical event;
- importing diagnostic or development-smoke controller records twice preserves
  one invocation, and a conflicting duplicate fails deterministically;
- author bootstrap replay is idempotent, and a path/digest mismatch fails
  before worker dispatch;
- two resumed invocations use the prior canonical snapshot and replay derives
  the same per-invocation delta;
- execution-session IDs never serve as provider-session IDs, and unresolved
  `resume_last` cannot select a predecessor by dispatch order alone.

### Task-Local Stop Condition

Stop if adding the event requires rewriting historical event logs or replacing
file authority with mutable database state.

## P1-04B Report And Notification Aggregation

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-04A

### Objective

Derive full-run usage summaries from canonical invocation records and expose a
compact, compatible operator view.

### Files

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Required Deliverables

- `full_run_usage_aggregation`
- `legacy_summary_compatibility`
- `partial_unavailable_and_open_rendering`
- `exact_and_partial_lower_bound_aggregation`
- `verification_summary`

### Steps

- [ ] Include every supported author, worker, runtime-diagnostic,
  development-smoke, and acceptance invocation in full-run summaries where
  its run context is available.
- [ ] Preserve reported, partial, unavailable, and not-applicable counts and
  compact reason summaries.
- [ ] Sum exact reported tokens and known partial-token lower bounds into
  separate labeled aggregates.
- [ ] Report open invocation count separately and prevent an open lifecycle
  from being rendered as completed or 100% covered.
- [ ] Label legacy task-result aggregates explicitly instead of mixing them
  into benchmark-counted invocation totals.
- [ ] Keep Feishu and terminal summaries compact by rendering aggregate fields,
  not raw invocation rows.

### Acceptance

- completion reports distinguish full-run usage from legacy task-only usage;
- diagnostic and development-smoke stages contribute to invocation counts,
  lifecycle/token coverage, and stage breakdowns without being mislabeled as
  workers;
- a retrying task reports two invocations but one completed task;
- partial and unavailable usage reduce token coverage, preserve lifecycle
  coverage after terminalization, and retain a bounded reason;
- partial token fields never inflate exact reported totals;
- open invocations reduce coverage, expose liveness separately, and block a
  completion summary;
- terminal and Feishu render the same aggregate accounting values;
- existing Chinese report and notification tests remain valid.

### Task-Local Stop Condition

Stop if a compact summary cannot be derived without mutating canonical events
or treating missing historical records as zero usage.

## P1-05 Invocation Projection And Stats

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-04B

### Objective

Project authoritative invocation records into rebuildable SQLite rows and make
coverage and cost attributable through the existing stats command family.

### Files

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test scope: `experiments/native_agentteam_runtime/m0_runtime/tests/`

### Required Deliverables

- `invocation_projection_schema`
- `file_scan_and_db_reconciliation`
- `usage_coverage_breakdowns`
- `exact_and_lower_bound_projection`
- `stats_filter_surface`
- `projection_rebuild_stability`
- `isolated_acceptance_projection`
- `verification_summary`

### Projection Schema

Add an invocation table with at least:

```text
invocation_id primary key
usage_event_id unique nullable
start_schema_version
usage_schema_version nullable
project
run_id
run_kind
implementation_run_id
gate_epoch
pursue_id
round_index
taskpack_id
task_id
attempt_id
runtime_execution_session_id
provider_session_id
provider_predecessor_invocation_id
provider_turn_id
provider_predecessor_turn_id
agent_id
role
usage_stage
backend
model
coverage_class
lifecycle_status
terminal_status
usage_status
usage_source
provider_usage_scope
accounting_method
provider_usage_snapshot_json
unavailable_reason
input_tokens
cached_input_tokens
output_tokens
reasoning_tokens
total_tokens
started_at
finished_at
wall_time_seconds
source_artifact_path
start_record_sha256
terminal_record_sha256
record_sha256
```

`lifecycle_status` is `open` or `terminal`. Terminal and token fields are null
for an open row. Add indices for the declared query dimensions,
acceptance-to-implementation lineage, gate epoch, and provider session
lineage. Bump the projection schema version; do not implement in-place
migrations because the DB is rebuildable.

### Steps

- [ ] Scan canonical lifecycle events, unresolved worker/author lifecycle
  directories, registered unresolved runtime-diagnostic and development-smoke
  controller roots, and controller-owned final live-smoke artifacts whose
  `controller_validation_status` is `passed`.
- [ ] Discover controller artifacts only below the configured project
  `work_root/runs/` tree; arbitrary external directories are not projection
  authority.
- [ ] Expose a deterministic isolated-projection API that accepts an explicit
  staged acceptance artifact and temporary DB path for controller validation;
  it must not register or publish that staged artifact as authority.
- [ ] Deduplicate starts by invocation ID and terminals by usage event ID;
  reject conflicting starts, terminals, or start/terminal correlation.
- [ ] Left-join terminal records onto starts so an orphaned/open invocation
  remains visible without synthesizing provider usage.
- [ ] Add invocation count and digest to projection freshness checks.
- [ ] Add file-backed fallback aggregation with the same semantics.
- [ ] Extend `agentteam stats --json` with invocation totals, coverage,
  open count, unavailable/partial reasons, exact reported totals, partial known
  lower bounds, and breakdowns by run kind, implementation run, gate epoch,
  stage, role, round, model, task, and attempt.
- [ ] Add optional `stats` filters for run, run kind, implementation run, gate
  epoch, pursue, round, stage, role, task, attempt, backend, and model instead
  of creating a new command family.
- [ ] Keep compact text to exact total, lifecycle/token coverage, partial lower
  bound when present, largest stage, and a next action when coverage is
  incomplete.
- [ ] Mark legacy aggregates as non-benchmark-counted and explain their source.

### Acceptance

- fresh DB totals equal authoritative file-scan totals;
- deleting and rebuilding the DB preserves invocation count, both coverage
  measures, exact reported totals, and partial lower bounds;
- a passed live-smoke artifact projects as one invocation in its dedicated
  acceptance run;
- unresolved or canonically imported diagnostic and development-smoke records
  project exactly once with their controller owner, runtime session, and stage;
- acceptance rows retain their attempt-scoped run ID, logical implementation
  run ID, `run_kind: acceptance_evidence`, and gate epoch;
- stale, missing, or corrupt DB falls back to files with an explicit warning;
- each filter produces the expected subset without changing authority files;
- both coverage denominators are based on unique supported start records, not
  task or terminal-record count;
- a start without a terminal is counted once in the denominator, shown as
  open while live, and blocks benchmark completion;
- recovery of a dead orphan changes it from open to unavailable without
  changing the denominator;
- a project containing only fake invocations reports not-applicable rather than
  unavailable;
- old runs remain visible and do not silently pass the 100% coverage gate.

### Task-Local Stop Condition

Stop if SQL aggregation would need to repair missing capture records or if file
fallback and fresh-DB results cannot be made equivalent.

## P1-06A Deterministic End-To-End Verification

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-05

### Objective

Prove the complete deterministic Phase 1 accounting path with fixed fixtures.

### Files

- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/`

### Required Deliverables

- `deterministic_end_to_end_usage_fixture`
- `core_supported_path_coverage_matrix`
- `projection_rebuild_reconciliation`
- `open_and_orphan_lifecycle_evidence`
- `positive_and_negative_fixture_separation`
- `verification_summary`

### Positive End-To-End Fixture

The positive fixture must exercise:

1. one ordinary taskpack author invocation;
2. one planner/task-slicer invocation;
3. one repo-map invocation;
4. one implementation invocation;
5. one explicit review/repair invocation;
6. one semantic-architecture invocation;
7. one failed or timed-out invocation after a terminal usage payload, proving
   that non-success status can still retain reported usage;
8. two invocations sharing a resumed provider session while retaining
   independent runtime execution sessions, using either confirmed
   invocation-scoped usage or authoritative session-delta accounting;
9. one follow-up author invocation;
10. one runtime-diagnostic invocation;
11. every tracked standalone development-smoke entry point;
12. projection rebuild;
13. filtered and unfiltered stats;
14. a second replay/rebuild.

Expected results are predeclared in the fixture. The test must compare exact
invocation count, both coverage numerators/denominators, stage totals, exact
reported token fields, and zero partial lower-bound contribution.

### Negative Recovery Fixture

A separate fixture and run ID must exercise:

1. one crash after durable start but before terminal finalization;
2. lease expiry while the worker/provider process remains live, proving
   recovery does not terminalize it;
3. durable owner revocation followed by supervisor reap and exact transient
   service control-group empty proof, then one unavailable recovery terminal;
4. a second recovery/replay pass proving no duplicate terminal.

Before recovery, expected lifecycle and token coverage are both `0/1` with one
open invocation. After safe recovery, lifecycle coverage is `1/1`, token
coverage remains `0/1`, and open count is zero. These expected negative metrics
are evidence for recovery correctness and are never combined with the positive
fixture's 100% token-coverage result.

### Acceptance

- the positive fixture has 100% lifecycle coverage, 100% token coverage, and
  zero open invocations;
- the negative recovery fixture matches its declared `0/1` then
  lifecycle-`1/1`/token-`0/1` transition without changing invocation count;
- separate recovery tests cover a changed host boot ID without signaling,
  same-boot `ESRCH` with populated and empty exact service groups, and
  proved enclosing user-service restart, with ambiguous inner-manager or
  service identity remaining open;
- projection rebuild is stable;
- all supported pre-controller invocation paths are tested; P1-06B owns the
  acceptance-live-smoke path and completes the inventory;
- focused and full deterministic tests pass.

### Task-Local Stop Condition

Do not pass this task when positive coverage is below 100%, negative fixture
metrics differ from their declared values, projection totals drift, positive
open invocations remain, or recovery can erase or prematurely close a start.
Passing P1-06A does not pass the live gate or complete Phase 1.

## P1-06B Acceptance Helper And Deterministic Controller

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-06A

### Objective

Implement the bounded live-smoke helper and the deterministic controller that
independently validates provider evidence and is the only component allowed to
publish the fixed P1-LIVE success artifact.

### Files

- Add:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/usage_live_smoke.py`
- Add:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/phase1_usage_acceptance.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`

### Required Deliverables

- `bounded_live_smoke_helper`
- `deterministic_acceptance_controller`
- `completed_supported_path_coverage_matrix`
- `independent_provider_reconciliation`
- `failure_artifact_and_atomic_publish_evidence`
- `verification_summary`

### Controller Contract

The implemented command is:

```text
env PYTHONPATH=<candidate-worktree>/experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.phase1_usage_acceptance \
  --profile-project-root <source-checkout-with-agentteam-profile> \
  --candidate-project-root <clean-integration-worktree> \
  --implementation-run-id phase1-model-invocation-usage \
  --gate-epoch <current-epoch> \
  --acceptance-series-id <phase1-run-id>-acceptance \
  --attempt-id <attempt-id> \
  --work-root <configured-project-work-root> \
  --expected-commit <verified-integration-sha> \
  --authorize-live-call
```

The controller loads `.agentteam/profile.json` only from
`--profile-project-root` and requires `--work-root` to equal that profile's
resolved work root. It treats `--candidate-project-root` solely as the clean
integration worktree under test. Both roots must resolve to the same Git common
directory, and the candidate branch/head must match the implementation run's
integration baseline. The candidate is expected not to contain `.agentteam`.
The controller derives
`run_id = <acceptance-series-id>-<attempt-id>` and
`run_dir = <work-root>/runs/<run-id>`. Series and attempt IDs must be bounded
safe slugs; path separators and traversal are rejected.
Each real call requires a fresh run ID. The controller builds a unique staging
directory under `<work-root>/run-staging/` on the same filesystem as `runs/`,
writes and flushes immutable
`state/controller_claim.v1.json` containing the logical taskpack and
implementation run IDs, run kind, current gate epoch, expected commit, series,
attempt, owner token, controller boot/start process identity, and nonce digest.
It also writes and flushes schema-valid
`state/run_identity.v1.json` with `run_kind: acceptance_evidence`, the
attempt-scoped `run_id`, and the same logical taskpack and implementation run
IDs and current gate epoch. It claims `run_dir` with one Linux no-replace
directory rename and flushes the `runs/` parent; concurrent attempts with the
same ID can have staging directories, but only one rename can win. Neither
record may be replaced. It may not register the gate or launch a provider
before the complete claimed directory is durable. A check-then-create sequence
is forbidden, and failed staging directories are bounded cleanup state rather
than run candidates.

If the same run ID already has a passed artifact for the same validated commit,
the command returns that result idempotently without another provider call. An
existing directory with an active claim reports in progress; a directory
without a complete claim, or with a failed/terminal claim, enters recovery-only
mode and never launches a second provider call under that run ID. Retries
allocate a new attempt ID. All attempts remain visible in project stats and the
milestone report. Passed artifacts and bounded failure diagnostics record the
explicit acceptance series ID; report code must not infer a series by parsing
run names.
Before provider launch, the controller registers this evidence run for the
implementation run's declared P1-LIVE gate in the current gate epoch.
Registration remains pending until the final artifact exists and validates.
The controller holds the epoch/gate execution lock from before registration
and the run-level gate-state lock through success or bounded failure
publication. After acquiring both, it freshly rereads the epoch, claim/open
invocation set, expected commit, and integration worktree before launch. It
fails when its expected epoch is stale, another attempt holds that lock, or a
prior attempt in the same epoch/gate still has an open invocation.

The controller:

- resolves runtime modules from the supplied candidate worktree;
- verifies its own module path and every imported Phase 1 runtime module resolve
  below the supplied candidate worktree, never the installed active release;
- invokes `usage_live_smoke` with a built-in minimal prompt, a read-only Codex
  sandbox, and output rooted below `<run-dir>/acceptance/`;
- validates the bounded provider spool independently of the production token
  parser;
- enforces the candidate SHA and clean/read-only worktree rules;
- writes bounded structured diagnostics on failure but does not publish the
  fixed success artifact;
- atomically publishes
  `<run-dir>/acceptance/model-invocation-live-smoke.v1.json` only after all
  checks pass.

### Acceptance

- fake-command tests cover pass, malformed provider event, token mismatch,
  hash mismatch, oversized spool, dirty candidate, changed HEAD/status,
  incorrect expected SHA, missing authorization, projection rejection,
  interrupted publication, recovery-only incomplete run, fresh retry ID, and
  idempotent passed-run lookup;
- a concurrent same-series/same-attempt test proves exactly one controller
  claim succeeds and exactly one fake provider launch occurs;
- a concurrent different-attempt test for one epoch/gate also permits exactly
  one controller execution; a crashed prior attempt must be terminalized by
  safe recovery before a replacement can launch;
- the durable claim, `run_identity.v1.json`, lifecycle records, projection
  rows, fixed artifact, and receipt agree on the attempt-scoped run ID, logical
  implementation run ID, and current gate epoch;
- helper arguments include `--project-root`, `--run-id`, `--output-dir`, and
  `--output`, and it rejects output inside the target project;
- controller rejects a work root that differs from the project profile and
  writes only below its derived registered run directory;
- controller succeeds when the clean integration worktree has no `.agentteam`
  directory, because profile and candidate roots are explicit and separately
  validated;
- gate registration is pending before publication and resolves P1-LIVE to
  passed only when the fixed artifact independently validates;
- controller totals and event hash come from an independent bounded-spool
  decoder, not `token_usage.py` or helper-normalized totals;
- controller verifies the helper's durable start and terminal records share one
  invocation ID before publishing success;
- fake controller tests complete the supported invocation inventory begun in
  P1-06A;
- no prompt, credential, webhook value, or unbounded JSONL enters the fixed
  artifact;
- normal unit tests never make a live provider call.

### Task-Local Stop Condition

Stop if a worker/helper claim can publish authoritative success, if controller
validation depends on the same production parser being tested, or if the live
call can write into the candidate source worktree.

## P1-06C Milestone Report Renderer And Finalizer

**Risk:** L1
**Role:** `implementation_worker`
**Depends on:** P1-06B

### Objective

Implement deterministic post-live report rendering and commit-lineage
validation without placing a commit SHA inside the commit whose SHA is being
computed.

### Files

- Add:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/phase1_usage_report.py`
- Add:
  `experiments/native_agentteam_runtime/schemas/phase1_usage_finalization.schema.json`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_projection_db_operator_paths.py`

### Required Deliverables

- `deterministic_milestone_report_renderer`
- `report_only_commit_lineage_validator`
- `external_finalization_artifact`
- `report_tamper_and_scope_evidence`
- `verification_summary`

### Controller Contract

The authoritative gate command is one deterministic transaction:

```text
env PYTHONPATH=<candidate-worktree>/experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.phase1_usage_report complete \
  --profile-project-root <source-checkout-with-agentteam-profile> \
  --candidate-project-root <clean-integration-worktree> \
  --implementation-run-id phase1-model-invocation-usage \
  --gate-epoch <current-epoch> \
  --work-root <configured-project-work-root> \
  --acceptance-series-id <phase1-run-id>-acceptance \
  --run-id <selected-passed-acceptance-run-id> \
  --validated-code-sha <P1-LIVE-validated-sha>
```

The command loads the configured work root from `--profile-project-root`, uses
`--candidate-project-root` for Git and module-origin validation, requires both
roots to share one Git common directory, and derives the same registered run
directory and current gate epoch as P1-LIVE. It does not require `.agentteam`
inside an integration worktree. It acquires the P1-06E epoch/gate execution
lock and run-level gate-state lock before evidence registration and holds both
through bounded failure or final artifact publication. After acquisition it
freshly rereads the epoch, receipts, evidence digests, integration ref, and
worktree status. It fails before source-tree mutation when the expected epoch
is no longer current.

The internal render phase reads canonical structured evidence, every attempt carrying the
explicit acceptance series ID, and the selected accepted P1-LIVE artifact. It
requires a clean integration worktree whose head is `validated_code_sha`,
writes only the fixed milestone report and roadmap status, verifies that exact
two-path diff, and creates one report-only integration commit with
`validated_code_sha` as its sole parent. It returns the resulting
`final_report_sha`. It does not merge that branch into source, push, or activate
a release.

Before that commit, `complete` registers a pending P1-06E receipt for the same
epoch and rechecks P1-LIVE. After the render phase creates the report-only
integration commit, the internal finalization phase verifies:

- `final_report_sha` is the current clean integration head;
- it has exactly one parent and that parent is `validated_code_sha`;
- the parent-to-head diff contains only
  `experiments/native_agentteam_runtime/implementation_artifacts/reports/phase1-model-invocation-usage.md`
  and
  `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`;
- the source report records `validated_code_sha` and accepted evidence digests,
  but does not attempt to contain its own commit SHA.

Only then may `complete` atomically publish:

`<run_dir>/acceptance/phase1-usage-finalization.v1.json`

The external artifact records `validated_code_sha`, `final_report_sha`, the
parent Git OID, `git_object_format`, `implementation_run_id`, `gate_epoch`,
changed paths, evidence digests, verification summary,
`merge_recommendation: ready_for_operator_review`, and
`controller_validation_status`.

If the controller crashes after the report-only commit but before final
publication, rerunning the same command under the same epoch/gate lock may
resume only when HEAD is already the exact clean single-parent child of
`validated_code_sha`, its two files equal the byte-stable render output for the
same evidence digests, and the pending receipt matches. It then skips commit
creation and runs finalization. Any other pre-existing child or receipt fails
closed. Final artifact publication remains the last acceptance-critical write.
A byte-equivalent rerun after the fixed passed artifact exists returns that
result idempotently without creating another commit or notification.

### Acceptance

- renderer output is byte-stable for identical canonical evidence;
- the complete controller starts at `validated_code_sha`, commits only the fixed report and
  roadmap paths, and returns a single-parent `final_report_sha`;
- the complete controller rejects a stale gate epoch before committing or
  publishing, and all P1-LIVE/P1-06E evidence belongs to one current epoch;
- renderer/finalizer function origins resolve below the Phase 1 integration
  worktree rather than the previously active release;
- the rendered report includes every live attempt and its usage, not only the selected
  passed attempt;
- the complete controller rejects work roots outside the project profile's
  configured discovery root;
- provisional, failed, or worker-authored live claims are rejected;
- the finalization phase requires the registered P1-LIVE dependency to be passed;
- finalization rejects wrong parent, dirty worktree, extra changed path,
  mismatched evidence digest, and report tampering;
- P1-06E gate registration remains pending until the external finalization
  artifact validates against the current integration head, then becomes
  `awaiting_operator_review` rather than passed;
- the finalization schema validates and contains no prompt, secret, or
  unbounded log;
- finalization commit fields validate as Git OIDs for the declared repository
  object format;
- fake deterministic tests exercise fresh completion, crash-after-commit
  resumption, passed-result idempotence, and finalization without a live call.

### Task-Local Stop Condition

Stop if report claims cannot be derived from canonical evidence, if
finalization would require a self-referential source SHA, or if the controller
would need authority to merge the integration branch into the source branch.

## P1-06D Operator Documentation

**Risk:** L0
**Role:** `implementation_worker`
**Depends on:** P1-06C

### Objective

Document the verified accounting behavior and operator command surface without
changing semantic authority.

### Files

- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/README.md`

### Required Deliverables

- `operator_documentation`
- `documentation_verification_summary`

### Steps

- [ ] Document that token units are provider-reported tokens, not bytes,
  seconds, or monetary cost.
- [ ] Explain cached and reasoning fields, invocation coverage, and unavailable
  legacy runs.
- [ ] Document the implemented `agentteam stats` filters and compact output.
- [ ] Explain that SQLite is a rebuildable projection and cannot alter
  authoritative totals.
- [ ] Document the gated live-smoke, report completion, digest-bound operator
  approval, and candidate-runtime boundaries.

### Acceptance

- command examples match the implemented CLI;
- documentation and operator summaries use the same counting contract;
- no document claims Phase 1 complete unless the required live smoke and
  finalization evidence plus operator-review gate passed;
- links and referenced paths resolve.

### Task-Local Stop Condition

Stop if implementation evidence and the requested documentation disagree; do
not resolve that conflict by weakening the documented contract.

## P1-LIVE Candidate Runtime Gate

**Risk:** L1 acceptance gate
**Role:** deterministic controller, with prior operator authorization
**Depends on:** P1-06D and verified-idle integration baseline
**Execution location:** outside the frozen backlog

### Objective

Run one bounded real Codex invocation from the candidate integration worktree
and mechanically prove that its terminal provider usage is captured correctly.
A worker deliverable name or prose claim cannot satisfy this gate.

### Fixed Artifact

The controller writes:

`<run_dir>/acceptance/model-invocation-live-smoke.v1.json`

Here `<run_dir>` is exactly
`<configured-project-work-root>/runs/<phase1-run-id>-acceptance-<attempt-id>`;
it is outside the source worktree but inside the projection's declared
discovery root.

The artifact conforms to
`schemas/model_invocation_live_smoke.schema.json` and contains at least:

```text
schema_version
project
run_id
run_kind
taskpack_id
implementation_run_id
gate_epoch
acceptance_series_id
acceptance_attempt_id
git_object_format
candidate_commit_sha
validated_code_sha
candidate_runtime_root
terminal_status
invocation_start_record
invocation_usage_record
provider_terminal_snapshot_sha256
provider_totals
reconciliation_status
controller_validation_status
started_at
finished_at
bounded_raw_spool_path
```

It must not contain the raw prompt, credentials, webhook values, or an
unbounded JSONL copy.

### Controller Procedure

The operator invokes the implemented P1-06B command once with the verified
integration head as `--expected-commit` and explicit `--authorize-live-call`.
The command, not an ad hoc shell sequence or worker prose, performs this
procedure:

1. Load the profile from the explicit source checkout, resolve the
   implementation run below its configured work root, and resolve the verified
   integration-baseline worktree, current gate epoch, and head commit. Require
   profile and candidate roots to share one Git common directory. Require a
   clean ordinary candidate status, then record HEAD and
   `git status --short --untracked-files=all --ignored`.
2. Durably build a same-filesystem `<work-root>/run-staging/` directory
   containing `state/controller_claim.v1.json` and schema-valid
   `state/run_identity.v1.json`, require both to map the attempt-scoped
   `run_id` to `run_kind: acceptance_evidence`, the logical
   `taskpack_id`/`implementation_run_id`, and the current `gate_epoch`, then
   claim the run with one no-replace directory rename. Reject stale epochs
   before registering evidence or launching a provider.
3. Launch `python3 -m agentteam_runtime.usage_live_smoke` with `PYTHONPATH`
   rooted in that worktree's `m0_runtime`, the integration worktree as
   `--project-root`, the already derived attempt-scoped `<run-id>` as
   `--run-id`,
   `<run_dir>/acceptance/runtime-output/provisional.json` as `--output`,
   `<run_dir>/acceptance/runtime-output` as `--output-dir`, a forced read-only
   sandbox, and the helper's built-in minimal prompt. The helper cannot write
   the fixed authoritative artifact path.
4. Read the provisional helper result. Do not publish, project, or report it as
   accepted evidence.
5. Check that `candidate_commit_sha` equals the verified integration head and
   `candidate_runtime_root` resolves inside its worktree. Require
   `bounded_raw_spool_path` to resolve inside the declared external
   `--output-dir` and reject a spool larger than 1 MiB.
6. Independently read the bounded raw spool. Using the frozen P1-01 provider
   event contract, locate the terminal provider event and decode its direct
   token fields without calling the production `token_usage` parser or trusting
   helper-derived totals. Canonicalize that raw event and recompute its SHA-256.
7. Validate one start and one terminal usage record with the same invocation
   ID. Compare the independently derived hash and totals with both the
   provisional result's provider fields and normalized terminal record. Require
   exact equality and one reported invocation.
8. Require the integration worktree's HEAD and full recorded status, including
   ignored paths, to be byte-equivalent to their pre-call values. All runtime
   output must remain under the external acceptance output directory.
9. Construct a bounded staged candidate below
   `<run_dir>/acceptance/staging/` without `passed` authority. Do not place it
   at the fixed path.
10. Use the P1-05 isolated-projection API and a temporary DB below the external
   acceptance directory to project all normal file authority plus the explicit
   staged candidate. Require one matching invocation in the dedicated
   acceptance run, both coverage measures at `1/1`, stable replay, and exact
   totals.
11. Recheck candidate HEAD/status, current gate epoch, and every prior
    condition. Only then add
    `controller_validation_status: passed` and atomically publish the final
    artifact to the fixed authoritative path as the controller's last
    acceptance-critical operation. A crash or failure before this step leaves
    no authoritative success artifact.

If the provider format differs, stop and update parser fixtures and contract
tests. Do not make a guessed parser pass. If authorization, provider access, or
the live call is unavailable, leave Phase 1 `live_validation_pending` with a
structured non-authoritative failure record. Projection and P1-06E must ignore
provisional or failed controller artifacts as success evidence, while still
projecting the attempt's ordinary start/terminal lifecycle files for cost and
coverage.

### Acceptance

- the fixed artifact exists and passes schema validation;
- `controller_validation_status` is `passed`;
- candidate commit and module root match the verified integration baseline;
- commit fields match the declared repository Git object format;
- profile and candidate roots share one Git repository while the candidate
  remains valid without a copied `.agentteam` directory;
- `validated_code_sha` equals the candidate commit and the expected verified
  integration head;
- terminal status is completed and usage status is reported;
- one start and one terminal record share an invocation ID and reconcile
  exactly with provider totals;
- lifecycle and token coverage are both `1/1`;
- the acceptance run has zero open invocations;
- an independent bounded-spool check reproduces the terminal event hash and
  totals without the production parser;
- the integration worktree HEAD and status are unchanged by the live call;
- the isolated prepublication projection records that invocation under the
  dedicated acceptance run ID and is replay-stable;
- `run_identity.v1.json` prevents the acceptance run from becoming the default
  latest implementation run, while explicit milestone aggregation still
  includes every acceptance attempt;
- controller claim, run identity, lifecycle records, projection row, fixed
  artifact, and receipt agree on `implementation_run_id` and `gate_epoch`;
- replaying or rereading the artifact does not create another model invocation.
- failed acceptance attempts remain queryable and are included in milestone
  cost reporting even though only a passed attempt satisfies P1-LIVE.
- the implementation run's registered P1-LIVE gate resolves to passed.

## P1-06E Milestone Report

**Risk:** L1
**Role:** deterministic controller, followed by operator review
**Depends on:** P1-LIVE
**Execution location:** outside the frozen backlog

### Objective

Render the single bounded Phase 1 implementation report from canonical events,
task results, projection reconciliation, and the controller-owned live artifact.

### Files

- Add:
  `experiments/native_agentteam_runtime/implementation_artifacts/reports/phase1-model-invocation-usage.md`
- Modify:
  `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

### Required Content

```text
implemented invocation paths
unsupported or unavailable paths
coverage result
exact deterministic fixture totals
projection replay/rebuild result
focused and full test results
candidate-runtime live smoke result
validated code commit SHA
changed files
remaining risks
conditional merge recommendation
```

### Acceptance

- every claim cites a structured result, canonical artifact, or command result;
- the report contains no raw prompt, secret, or unbounded JSONL;
- the live result is read from the fixed controller artifact, not worker prose;
- the roadmap status matches the mechanically validated live and deterministic
  evidence and remains `finalization_pending` before the report-only commit is
  validated;
- the source report records `validated_code_sha` from P1-LIVE and accepted
  evidence digests, but does not contain a self-referential final commit SHA;
- the P1-06C `complete` controller creates or safely resumes the exact
  report-only integration commit and publishes a schema-valid external
  finalization artifact containing
  `final_report_sha` and the final operator-review recommendation;
- that artifact proves `final_report_sha` has exactly one parent,
  `validated_code_sha`, and the intervening diff is limited to the fixed
  milestone report and roadmap status;
- the controller artifact moves P1-06E to `awaiting_operator_review`; the gate
  resolves to passed and exposes standard `agentteam integrate` only after the
  operator's immutable approval matches the epoch, evidence digest, report
  diff, and current integration head;
- the source report's merge recommendation is explicitly conditional on the
  external finalization gate and does not claim that gate has already passed;
- the final external recommendation is review-only and does not merge, push,
  or activate a release;
- the operator can reproduce the acceptance decision from the listed evidence.

### Task-Local Stop Condition

Report `live_validation_pending` and recommend against merge when P1-LIVE or
any deterministic completion gate is missing or failed. Report
`finalization_pending` and recommend against merge until the external
finalization artifact validates; then report `awaiting_operator_review` and
continue to block merge until PRE-03A operator approval passes.

## Verification Matrix

| Case | Expected result |
| --- | --- |
| One terminal cumulative payload | One reported invocation with exact provider totals. |
| Multiple cumulative snapshots | Last terminal cumulative payload counted once. |
| Two invocations in one resumed provider session | Per-invocation reports sum directly, or serialized session-cumulative reports use one monotonic delta per invocation. |
| Runtime execution session differs from provider session | Both identities remain distinct; no attempt-derived ID is used for provider deltas. |
| Concurrent cumulative calls in one provider session | Dispatch rejects the known conflict, or accounting is partial/unavailable if discovered later. |
| Resumed session missing prior snapshot | Partial or unavailable; prior session cost is not assigned to the current invocation. |
| Crash after durable start and before terminal write | Start remains in the denominator; recovery closes it once as unavailable after process death is established. |
| Start whose process is still live | Invocation remains open and blocks completion; it is not prematurely terminalized. |
| Ambiguous delta-only payload | Partial or unavailable; no guessed sum. |
| Cached input present | Stored separately; total unchanged. |
| Reasoning present | Stored separately; total unchanged. |
| Invalid JSONL lines | Ignored with parser diagnostics; no crash. |
| Failure after usage emission | Failed terminal status with reported usage. |
| Timeout before usage | Timed-out terminal status with unavailable reason. |
| Retry launches provider again | Two invocation IDs and two real costs. |
| Same result replayed | One usage event ID and one cost. |
| Conflicting duplicate ID | Integrity failure, not last-write-wins. |
| Fake/shell path | Not applicable and excluded from coverage. |
| Initial and follow-up author | Different stages with correct round metadata. |
| Legacy run | Readable, explicitly non-benchmark-counted or unavailable. |
| DB missing/stale/corrupt | File fallback with warning and identical authoritative totals. |
| Projection rebuild twice | Identical count, digest, coverage, and totals. |
| Worker claims live success without controller artifact | P1-LIVE remains pending. |
| Live artifact candidate SHA differs from integration head | Gate fails. |
| Valid live artifact projected twice | One acceptance invocation and one cost. |

## Verification Commands

Focused parser, adapter, author, projection, and CLI tests should be run during
their owning tasks. The final deterministic gate uses test discovery so newly
added test modules cannot be omitted. For a developer shell, run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
  python3 -m unittest discover \
  -s experiments/native_agentteam_runtime/m0_runtime/tests \
  -p 'test*.py'
```

Validate schemas and diff hygiene:

```bash
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/model_invocation_started.schema.json
```

```bash
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/model_invocation_usage.schema.json
```

```bash
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/event.schema.json
```

```bash
python3 -m json.tool \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-07-23-phase1-model-invocation-usage.blueprint.json
```

```bash
git diff --check
```

The runtime taskpack must encode the full deterministic suite as a JSON string
array. The integration runner supplies the native-runtime `PYTHONPATH`:

```json
[
  "python3",
  "-m",
  "unittest",
  "discover",
  "-s",
  "experiments/native_agentteam_runtime/m0_runtime/tests",
  "-p",
  "test*.py"
]
```

Schema validation commands must likewise be arrays such as:

```json
[
  "python3",
  "-m",
  "json.tool",
  "experiments/native_agentteam_runtime/schemas/model_invocation_usage.schema.json"
]
```

Run the same command for
`experiments/native_agentteam_runtime/schemas/model_invocation_started.schema.json`,
`experiments/native_agentteam_runtime/schemas/model_invocation_writer_revoked.schema.json`,
`experiments/native_agentteam_runtime/schemas/model_invocation_live_smoke.schema.json`,
`experiments/native_agentteam_runtime/schemas/phase1_usage_finalization.schema.json`,
`experiments/native_agentteam_runtime/schemas/taskpack_blueprint.schema.json`,
`experiments/native_agentteam_runtime/schemas/post_backlog_gate_receipt.schema.json`,
`experiments/native_agentteam_runtime/schemas/post_backlog_gate_epoch.schema.json`,
`experiments/native_agentteam_runtime/schemas/post_backlog_gate_approval.schema.json`,
`experiments/native_agentteam_runtime/schemas/runtime_release_binding.schema.json`,
`experiments/native_agentteam_runtime/schemas/run_identity.schema.json`,
and
`experiments/native_agentteam_runtime/schemas/phase1_contract_review.schema.json`.

The gated live smoke and finalization commands must be documented by P1-06D
after their helpers exist. The live command must not be included in normal
unit-test or CI execution, but its successful candidate-runtime result remains
mandatory for Phase 1 completion.

## Evidence Policy

This taskpack uses risk-proportional evidence:

- P1-00 keeps one L3 contract review artifact because it freezes authority and
  counting semantics.
- P1-01 through P1-06D return compact structured task results, changed-file
  lists, and verification additions. They do not write separate prose traces.
- P1-LIVE writes one controller-owned structured acceptance artifact outside
  the source tree.
- P1-06E deterministically renders the milestone's only implementation report,
  updates roadmap status from accepted evidence, and publishes one external
  finalization artifact after the report-only commit.
- PRE-03A publishes one compact operator-approval record only after review; it
  is gate authority, not a chronological trace.
- No task writes a long chronological trace.
- Raw Codex JSONL remains bounded test evidence or existing spooled runtime
  output; it is not copied into reports or the database.

## Integration Policy

- Each task runs in an attempt worktree.
- The implementation run is bound to the approval-reviewed active
  pre-Phase-1 runtime release before its first scheduler subprocess; all
  continuations use that immutable binding. The bound release source must
  descend from the reviewed PRE-04 integration commit.
- Accepted task patches are applied to the Phase 1 integration baseline in
  dependency order.
- Every task starts from the latest verified integration baseline.
- Intermediate task success does not authorize source merge.
- Backlog verified-idle does not authorize source merge; P1-LIVE and P1-06E
  remain required post-backlog gates enforced by status and integrate.
- Run the deterministic full verification after the frozen backlog reaches
  verified idle.
- Commit the verified code-only integration baseline and record its immutable
  head as `validated_code_sha` by running PRE-03A `gate seal-baseline`; no live
  evidence may register before epoch 1 is published.
- Run P1-LIVE from that exact baseline; do not accept a worker-authored claim
  as live evidence, and require the artifact's `validated_code_sha` to match.
- After P1-LIVE passes, run the P1-06C `complete` controller for P1-06E in the
  clean integration worktree. Under one epoch/gate execution lock it registers
  pending evidence, reruns document/diff checks, creates or safely resumes the
  report-only integration commit, and records `final_report_sha` outside the
  source tree only after verifying that the commit has exactly one parent,
  `validated_code_sha`, and the intervening diff contains only the fixed Phase
  1 milestone report and roadmap status update.
- The operator reviews the final diff, report, coverage, schemas, epoch, and
  evidence digests, then runs the digest-bound interactive
  `agentteam gate approve` command. Controller evidence alone cannot approve
  P1-06E.
- Require both registered post-backlog gates to resolve passed before
  `agentteam integrate` is displayed or accepted.
- Require a fast-forward source integration. Do not use `--rebase` after
  commit-bound gates pass; target divergence invalidates their commit evidence
  and requires the PRE-03D gate-epoch refresh command. That command creates a
  new integration epoch branch from the prior `validated_code_sha`, merges the
  current target head, reruns frozen verification, and publishes a new current
  epoch with both gates pending. It never rewrites the prior epoch branch,
  receipts, approvals, artifacts, or cost records.
- Push and release activation are separate operator actions after source merge.

## Stop Conditions

Stop and request operator or architecture review when:

- provider JSONL semantics cannot distinguish cumulative and delta usage;
- completing the task would require estimating provider token usage;
- a new canonical authority location conflicts with the approved file-backed
  authority rule;
- a stable invocation identity cannot be allocated before process launch;
- existing event replay semantics cannot represent one terminal record without
  destructive migration;
- a supported live invocation path cannot expose provider usage and the
  research coverage gate would need to be weakened;
- implementation would require changing the approved research claim,
  benchmark scoring rule, or DB authority model.

Ordinary implementation failures, test failures, fixture corrections, and
bounded refactors are not architecture stops. Workers should repair them inside
the task scope.

## Explicit Non-Goals

Phase 1 does not include:

- token budgets or scheduler budget stops;
- the three-mode experiment harness;
- SWE-EVO or another standard benchmark run;
- a broad fault-injection or crash-recovery experiment campaign; the bounded
  lifecycle fencing regressions defined in P1-02 and P1-06A remain in scope;
- long-running semantic feedback experiments;
- dollar-cost estimation;
- new model providers or M68 adapters;
- DB-primary authority;
- a web dashboard;
- per-invocation prose traces;
- broad CLI, scheduler, or projection refactors unrelated to usage capture;
- automatic source merge, push, or release activation.

Those items remain in later phases of the approved research route.

## Required Operator Review

Before execution, the operator reviews:

- record identity and authority;
- stage vocabulary;
- coverage denominator;
- task ordering and write scope;
- live-smoke boundary.

After execution, the operator reviews:

- schema and compatibility behavior;
- exact deterministic totals;
- projection stability;
- provider live-smoke evidence;
- final source diff and merge recommendation.

Approval of this taskpack authorizes bounded implementation and integration
baseline work only. It does not authorize source merge, push, release
activation, or changes to semantic authority.

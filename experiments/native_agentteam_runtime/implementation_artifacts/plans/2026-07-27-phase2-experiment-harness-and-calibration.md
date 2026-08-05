# Phase 2 Experiment Harness And Short Calibration Taskpack Set

Status: the baseline P2-01 through P2-07B implementation exists. The formal
v6 review-and-repair execution accepted P2-MAP through P2-03B, then stopped at
P2-04 after a scheduler recovery defect. P2-04 through P2-07B remain subject
to the recovery taskpack and operator review v4. P2-08 remains blocked until
that recovery taskpack completes and its integration commit is installed as
the candidate runtime release.

## Execution Recovery On 2026-07-29

The frozen v6 taskpack and its event log remain immutable execution history.
The accepted prefix produced reviewed commits for P2-MAP, P2-01, P2-02A,
P2-02B, and P2-03B. The P2-04 worker correctly reported no current diff, but
its stale historical `changed_files` declaration caused `diff_mismatch`.
The scheduler then projected the event-log task as blocked while retaining a
running task in SQLite, so the original frozen run could not safely redispatch.

Recovery does not edit the frozen v6 taskpack or its database. The source
branch integrates the accepted prefix and separately records these repairs:

- `0cdd355` derives worker `changed_files` from the attempt worktree and emits
  a canonical backlog event for validation rejection;
- `8e6d9fb` atomically publishes immutable JSON authority, restores root-owned
  privileged-system-tree enforcement, and rejects nested prior `.agentteam`
  state;
- `0a3f29f` preserves only taskpack-declared runtime artifacts during worktree
  reconciliation and keeps scheduler projection consistent with terminal
  retry availability.

The recovery blueprint
`2026-07-29-phase2-experiment-harness-recovery.blueprint.json` starts at P2-04
against this committed prefix after a bounded P2-MAP-R refresh. It adds P2-05C as an explicit production
cleanup task. P2-05C must seal and validate the terminal result before removing
the disposable snapshot, publish an immutable cleanup receipt bound to the
sealed result, retain cleanup failure as evidence, and make projection/show
report the receipt-derived status. A permanently pending bundle without a
durable receipt is not accepted.

Review v4 binds the recovery blueprint and this amended plan to runtime release
`phase2-recovery-0a3f29f`. Approval of v4 authorizes only P2-MAP-R through
P2-07B-R. It does not authorize P2-08, live provider calibration, branch
promotion, merge to the source branch, or push.

## Purpose

This taskpack implements research Phase 2 and the remaining P0-B experiment
readiness work. It turns the Phase 1 invocation-usage records into a
reproducible three-mode experiment runner, then proves the accounting path on
bounded deterministic fixtures.

The milestone closes only the infrastructure and short-calibration gate. It
does not make a research claim and does not run a scored benchmark pilot.

## Authority

- Research questions, modes, metrics, intervention classes, and phase order
  remain governed by
  `experiments/native_agentteam_runtime/research/agentteam_research_positioning.md`.
- Usage identity, lifecycle authority, terminal cardinality, and token
  reconciliation remain governed by
  `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-07-23-phase1-model-invocation-usage.md`.
- The seven P0 readiness capability IDs remain governed by
  `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-07-25-p0a-experiment-readiness-guard.md`.
- This document is implementation authority for P0-B and research Phase 2.

If this plan conflicts with the research authority or the Phase 1 usage
contract, stop and request semantic-architecture review. An implementation
worker must not silently redefine either authority.

## Milestone Boundary

P0-B includes:

1. immutable manifest publication and experiment identity;
2. clean source reset and blind-gold isolation checks;
3. optional token and wall-time experiment budgets;
4. the `single_codex`, `agentteam_direct`, and `agentteam_full` execution
   adapters;
5. normalized operator action ledger;
6. immutable machine-readable result bundle and concise comparison report;
7. deterministic L1 and bounded L2 calibration fixtures;
8. readiness promotion only after all acceptance evidence passes.

P0-B excludes:

- a scored SWE-EVO, FeatureBench, RoadmapBench, or other benchmark run;
- fault injection beyond deterministic budget-stop and ordinary resume tests;
- multi-host scheduling;
- non-Codex model adapters;
- provider billing estimates or currency conversion;
- automatic source-branch merge, push, release activation, or semantic
  authority mutation;
- a claim that one mode is better based on calibration fixtures.

## Phase 1 Completion Precondition

Phase 1 implementation, live validation, P1-06E finalization, and operator
approval are complete at `ec64cd89e25d88a6a47231f4654ae70c5e7ea148`.
The generated milestone report intentionally remains
`finalization_pending` because changing it after approval would invalidate the
approved report commit and digest.

P0-B consumes a separate tracked promotion chain:

```text
phase1-usage-finalization.v1.json
  + phase1-usage-finalization-approval.v1.json
  -> phase1-completion-promotion.v1.json
  -> invocation_level_real_usage = passed
```

The runtime validates the source artifacts, byte digests, gate epoch, final
report commit, approval decision, and promotion schema. It must not infer
completion from branch names, current HEAD, or prose.

## Fixed Contract

### Experiment Series, Instance, And Run Identity

Phase 2 uses two immutable levels:

- `experiment_protocol.v1` identifies the experiment series and benchmark
  instance and fixes all common inputs, all three modes, mode order,
  repetition policy, environment, budgets, and evaluator.
- `experiment_run_manifest.v2` identifies one execution and binds the protocol
  digest, mode, repetition index, and stable request key.

The P0-A `agentteam_experiment_manifest.v1` remains readable for validation
compatibility but is not executable by the Phase 2 harness.

One execution creates an `experiment_run_id` derived from:

```text
protocol_sha256
mode
repetition_index
stable_request_key
```

There is no second execution identifier. `experiment_run_id` is the only
execution identity used by bindings, events, snapshots, leases, and result
bundles.

The stable request key is persisted before provider launch. Repeating the same
request returns the existing run rather than consuming the provider twice. The
protocol seed is the only task-level deterministic seed.

One result bundle belongs to exactly one `experiment_run_id`. Re-running a
terminal experiment requires a new request key and preserves the old bundle.
Resuming an interrupted experiment keeps the same run ID. A scored
budget-stopped run is terminal and cannot receive a larger budget.

### Immutable Manifest Publication

Before source snapshot, provider launch, or budget consumption, the controller
must:

1. validate the protocol and run-manifest schemas;
2. verify the run manifest binds the canonical protocol digest;
3. verify the protocol contains all three modes and a complete environment
   contract;
4. verify the repository commit and object format;
5. canonicalize and publish both artifacts with create-if-absent semantics;
6. write a binding containing both digests, runtime release identity, source
   repository identity, stable request key, and run ID;
7. acquire a single-writer controller lease;
8. reject later resume when any bound identity differs.

Published artifacts are authoritative input. Mutable source paths are never
reread after publication.

### Equal-Input Three-Mode Contract

The three required modes are:

- `single_codex`: one fresh Codex invocation receives the protocol goal,
  constraints, acceptance-command text, and direct access to the sanitized
  repository. It does not receive an AgentTeam repo map, taskpack, handoff, or
  prior mode output.
- `agentteam_direct`: the runtime executes a reviewed, pre-authored frozen
  taskpack bound by digest in the protocol.
- `agentteam_full`: the runtime authors, freezes, routes, executes, verifies,
  and follows up under the normal control plane.

All modes use the same:

- source commit and Git object format;
- goal and constraints;
- acceptance command and evaluation environment;
- Codex CLI version, model, reasoning profile, service configuration, sandbox,
  permission policy, network policy, tool allowlist, host class, CPU/memory
  limits, and dependency-cache policy;
- token and wall-time budget;
- deterministic seed;
- blind-gold policy;
- evaluator version.

Initial Phase 2 comparisons use one model and reasoning profile for every role
and set `max_inflight_model_invocations` to `1`. A protocol-global,
cross-run single-writer provider lease serializes every counted invocation in
the experiment series, including concurrent controller processes. A run-local
counter is insufficient. Role-specific model routing and concurrent provider
lanes are later ablations, not P0-B behavior.

Mode order is preregistered from the protocol seed using a deterministic
counterbalanced rotation. The controller records the selected order and cannot
reorder modes after observing a result.

Mode-specific orchestration instructions are allowed, but they must be retained
in the result bundle and must not add task facts unavailable to another mode.
`agentteam_direct` may consume only the taskpack digest declared before the
experiment series starts. `agentteam_full` may not consume that direct-mode
taskpack.

### Clean Reset And Workspace Isolation

Every experiment run starts in a sanitized repository snapshot with its own Git
object store. A shared-common-directory worktree is insufficient because it
exposes unrelated refs, reflogs, objects, and prior mode history.

The controller creates a new repository, fetches only the exact source commit
without local alternates, detaches HEAD, removes remotes, and verifies before
provider launch:

- `HEAD` equals the protocol commit and the tree OID matches the source tree;
- the worktree is clean;
- no branch is checked out;
- the object format matches and its common directory differs from the source;
- no remotes, alternates, extra refs, or symlink escape exist;
- the snapshot path did not exist before allocation;
- no prior result, patch, taskpack, repo-map handoff, provider session, or
  `.agentteam` run state is copied into the new workspace.

Runtime state, immutable inputs, and result artifacts live outside the target
worktree. A mode may create its own state below the experiment-run directory.
Cleanup removes the disposable snapshot only after
the result bundle is durably finalized. Failed cleanup is recorded and does not
delete the result.

### Blind-Gold Isolation

The runtime contract is `unavailable_to_runtime`. P0-B uses the installed
`bwrap` and unprivileged user namespaces to mount only the sanitized snapshot,
required runtime binaries and libraries, and a bounded read-only Codex
credential view. Evaluator and gold state stay outside the provider namespace.

The protocol network policy is closed to `disabled` or `provider_access`.
Deterministic fixtures use `disabled`. An explicitly authorized live Codex
calibration uses `provider_access`: the provider launch retains all file,
credential, capability, PID, IPC, and mount isolation but adds `--share-net`
after `--unshare-all` so the Codex client can reach its provider. This does not
mount any additional host path. The pre-launch canary denial probe and trusted
post-model evaluator always keep the isolated network namespace; the evaluator
also receives no credential mounts.

Before launch, a random gold canary is stored in evaluator-only state. A probe
inside the exact worker sandbox must return denied or not found. After the run,
bounded prompt, taskpack, context, and artifact scans must not contain the
canary or its digest.

In addition:

- no gold patch, gold answer, hidden expected diff, or path to such material
  may appear in the manifest, task prompt, environment allowlist, taskpack,
  repo context, workspace, or runtime state;
- acceptance commands are preregistered and executed by the controller after a
  mode reaches a terminal implementation boundary;
- a benchmark adapter may hold evaluator-only data outside the runtime-visible
  input set, but P0-B fixtures use deterministic acceptance commands and no
  gold patch;
- result scoring may expose pass/fail and bounded diagnostics only after the
  model invocation terminates.

If the namespace probe is unavailable or inconclusive, blind-gold remains
`partial` and pilot authorization remains blocked. Directory convention alone
cannot pass this capability.

### Acceptance Command Contract

The protocol stores one argv array, not a shell string. The runner:

- executes without `shell=True`;
- resolves the executable through the approved verification-command policy;
- runs from the experiment workspace with a bounded environment;
- records start, finish, exit code, timeout, and bounded stdout/stderr digests;
- never changes the command across modes;
- does not count evaluator work as a model invocation;
- marks infrastructure failure separately from task acceptance failure.

### Budget Contract

Budgets are optional outside experiments and mandatory in an experiment
protocol. P0-B enforces:

- one wall-time deadline measured from the durable `running` transition;
- one total-token ceiling over benchmark-counted terminal invocation records;
- a soft warning threshold fixed in the protocol;
- a hard stop at a scheduler-safe or invocation-terminal boundary;
- no new model invocation after the hard budget is exhausted;
- durable `budget_stopped` state for inspection and evidence preservation;
- one durable `experiment_budget_warning` or
  `experiment_budget_exhausted` event per threshold crossing.

`total_tokens` uses the provider total after Phase 1 validation. Input, cached
input, output, and reasoning remain separately reported; unavailable or partial
usage cannot pass calibration. Provider usage may become authoritative only at
invocation terminalization. A ceiling can therefore be exceeded by one
already-started invocation because Phase 2 fixes one provider lane. The exact
overshoot remains visible and is never clamped. P0-B does not claim a strict
zero-overshoot token cap when Codex exposes no authoritative streaming counter.

Wall time records both controller execution time and total harness time, with
fixed start and end transitions. Enforcement may request termination of an
active provider process,
but it must use the Phase 1 durable invocation supervisor and preserve any
salvageable terminal usage. It must never interrupt a Git integration
transaction. The controller checks the deadline before provider launch, after
invocation terminalization, before integration begins, and after integration
commits.

A scored run cannot resume with an increased budget because that would mutate
the preregistered comparison. An increased budget creates a new unscored run
or an approved protocol lineage. Interruption recovery may resume only inside
the original remaining budget.

### Operator Action Ledger

Every operator input during an experiment becomes one append-only normalized
entry with:

```text
ledger_schema_version
action_id
experiment_run_id
mode
action_class
requested_at
answered_at
request_source
request_digest
response_digest
reason
related_task_id
related_attempt_id
counts_as_intervention
```

The closed action classes are:

- `expected_operator_action`
- `corrective_intervention`
- `decision_escalation`

Secrets and full free-form messages are not duplicated into the ledger.
Authoritative source events retain the original content where existing policy
allows it; the ledger stores digests and bounded reasons. Replay is idempotent
by `action_id`. All modes receive the same preregistered expected-input and
intervention limits.

### Result Bundle

Mutable controller snapshots and sealed terminal result bundles are separate.
Interrupted in-progress state writes versioned resumable snapshots. Every
completed, failed, or budget-stopped run creates one sealed terminal result
bundle. The final bundle contains:

```text
schema_version
experiment_run_id
protocol_sha256
run_manifest_sha256
runtime_release_identity
mode
repetition_index
source_commit
started_at
finished_at
terminal_status
acceptance_result
usage_totals
usage_coverage
budget_result
attempt_counts
verified_milestones
operator_action_counts
retry_and_repair_counts
changed_files
regressions
artifact_bytes_written
raw_spool_bytes_written
workspace_diff_sha256
result_evidence
projection_reconciliation
cleanup_status
```

The bundle references bounded evidence by digest and relative path. It does not
embed raw provider logs or unbounded test output. The canonical bundle is
written create-if-absent and sealed with a digest sidecar. Acceptance, budget,
ledger, and finalization writes are idempotent. A terminal bundle is never
mutated; a post-terminal correction creates a new unscored lineage.

`artifact_bytes_written` counts authoritative experiment artifacts below the
run root, excluding sanitized repository Git objects, dependency caches,
projection databases, and raw provider spools. Raw spool bytes are reported
separately as `raw_spool_bytes_written`.

Terminal publication uses a staging directory on the same filesystem. The
controller writes the canonical result and digest sidecar, fsyncs both files
and the staging directory, then atomically renames the directory to its final
create-if-absent path and fsyncs the parent. If the destination exists, an
identical digest is an idempotent replay and a different digest is a terminal
conflict. Recovery never edits or reseals the published directory.

Failed and interrupted runs remain in comparison inputs. Reports may filter
them only through an explicit status field, never by deleting them.

### Projection And Reconciliation

Authoritative files and canonical events remain primary. SQLite remains a
rebuildable projection. The experiment projection stores manifest identity,
mode, status, budgets, usage, acceptance, intervention counts, and bundle
digest.

Short calibration passes only when:

- benchmark-counted invocation lifecycle coverage is 100%;
- benchmark-counted token usage coverage is 100%;
- file replay and projection totals match;
- rebuilding the projection leaves totals unchanged;
- no invocation, action, attempt, or result bundle is counted twice;
- cached input remains distinct from non-cached input;
- every unavailable usage record is excluded from a passing calibration.

## Runtime Layout

The default project experiment root is outside the target worktree:

```text
<project-work-root>/experiments/
  protocols/<protocol-sha256>.json
  run-manifests/<run-manifest-sha256>.json
  requests/<stable-request-key>.json
  runs/<experiment-run-id>/
    binding.json
    controller-lease.json
    state.json
    snapshots/
    events.jsonl
    operator_actions.jsonl
    repository/
    mode/
    evaluation/
    results/result.v1.json
    results/result.v1.sha256
```

`state.json` and `snapshots/` are mutable/versioned recovery state. Protocols,
run manifests, request bindings, canonical events, ledger entries, usage
records, and sealed terminal result bundles are authoritative. The projection
database is not authority.

## State Machine

```text
prepared
  -> resetting
  -> ready
  -> running
  -> evaluating
  -> finalizing
  -> completed | failed | interrupted | budget_stopped
```

An interruption first creates a recoverable snapshot while the run remains
nonterminal. Resume is allowed only from that snapshot and inside the original
budget. If recovery is abandoned or cannot be validated, the controller
finalizes an immutable terminal `interrupted` bundle; that bundle is not
resumable. A scored `budget_stopped` run is also terminal; an unscored
replacement requires a new request key and protocol lineage. Resume reacquires
the controller lease and protocol-global provider lease and revalidates both
digests, sanitized repository identity, runtime release, budget state, and
terminal invocation inventory before any provider call.

An experiment reaching `completed` may still have `acceptance_passed: false`.
`completed` means the harness completed normally, not that the task succeeded.

## Ordered Tasks

Phase 2 uses two frozen taskpacks because the preflight release cannot hot-load
the gate runtime that P2-07B is implementing:

1. `phase2-experiment-harness-implementation` runs P2-MAP through P2-07B and
   stops at a normal operator review boundary.
2. After that verified baseline is reviewed and installed as a candidate
   release, a separately approved controller-only promotion taskpack runs
   P2-08 through P2-10. It has no model backlog and uses the candidate
   release's deterministic gate controller.

This is a release handoff, not a semantic replan. The second blueprint remains
bound to this plan, the candidate release commit, the promotion schemas, and a
new operator approval digest.

```text
P2-00A Phase 1 completion promotion
  -> P2-00B contract and adversarial review
      -> P2-MAP deterministic repository map handoff
      -> P2-01 protocol/run schemas, immutable publication, and idempotent binding
           -> P2-02A sanitized repository snapshot and clean-reset attestation
P2-MAP + P2-02A
  -> P2-02B bwrap evaluator/gold isolation
  -> P2-03A pure global budget controller
  -> P2-03B safe-boundary scheduler integration
  -> P2-04 operator action ledger
  -> P2-05 result bundle and recovery snapshots
  -> P2-06 three execution-mode adapters
  -> P2-07A deterministic L1/L2 calibration
  -> P2-07B executable gate controllers and relation validators
  -> P2-08 readiness promotion and pilot guard
  -> P2-09 bounded live calibration
  -> P2-10 report, roadmap, and operator merge gate
```

### P2-00A Phase 1 Completion Promotion

**Risk:** L2

Track the accepted P1-06E finalization and operator approval byte-equivalently,
validate their schemas and relational bindings, then publish a completion
promotion receipt. Update only the invocation readiness capability from that
receipt. Do not rewrite the approved Phase 1 report commit.

### P2-00B Contract And Adversarial Review

**Risk:** L3

Freeze this document, the deterministic blueprint, the new schema inventory,
and the exact capability-promotion rules. Review must explicitly decide:

- equal-input mode fairness;
- taskpack binding for direct mode;
- clean reset and blind-gold claim boundary;
- one-invocation token overshoot semantics;
- result-bundle authority and terminal immutability;
- calibration versus scored-pilot boundary.
- two-level protocol/run identity and stable request idempotency;
- independent object store and bwrap canary enforcement;
- one global provider lane and no scored budget extension;
- readiness promotion before live calibration.

No implementation task may start before an approved review artifact binds the
plan and blueprint digests.

### P2-MAP Deterministic Repository Map Handoff

**Risk:** L0

Generate a bounded repository map from committed source, symbols, tests, and
the approved Phase 2 contract. The handoff is shared by L2 implementation
workers to avoid repeated broad discovery. It is not supplied to the
`single_codex` experiment mode and is never benchmark evidence.

### P2-01 Schemas, Immutable Publication, And Run Binding

**Risk:** L1

Add `experiment_protocol.v1` and `experiment_run_manifest.v2` schemas plus
run-binding and state schemas. P2-03, P2-04, and P2-05 own the budget-event,
ledger, snapshot, and result schemas respectively. Keep the P0-A v1 manifest
validation-only.

Implement immutable publication, stable-request idempotency, single-writer
lease, collision-safe run allocation, and resume binding validation. Tests
cover digest tampering, duplicate requests, competing controllers, wrong
release, wrong repository, and object-format mismatch without provider calls
or target mutation.

### P2-02A Sanitized Repository Snapshot And Clean-Reset Attestation

**Risk:** L1

Create a new object store containing only the exact source commit. Remove
remote and alternate references, detach HEAD, verify the source tree and file
inventory, reject symlink escapes, and publish a clean-reset attestation.
The resulting experiment workspace is always a standalone repository with an
in-workspace `.git` directory. Linked-worktree `.git` files fail closed.

### P2-02B Bwrap Evaluator And Gold Isolation

**Risk:** L2

Build the exact provider sandbox, evaluator-only namespace, gold canary probe,
bounded environment, and preregistered argv acceptance path. Reject gold-like
protocol fields, unsafe mounts or environment additions, reused paths, and
canary leakage. Run acceptance only after model invocations terminate.

Sandbox certification and leak-scan scope are immutable controller-owned
references under the run authority, not taskpack fields supplied by a worker.
The controller publishes a multi-root invocation manifest that binds every
authoring, worker, and follow-up lifecycle root to its taskpack and exact
canary-probed sandbox reference. Each root is sealed before evaluation, and the
evaluator must be the exact digest-bound executable from the protocol, running
in a fixed-system-binary systemd cgroup.
The systemd service starts a fixed source guard as its stable main process. The
guard revalidates mutable launch authority, starts the certified bwrap command,
and waits for its real result. Inside that namespace, a fixed runtime loader
receives the already digest-checked evaluator bytes over a bounded pipe,
materializes them in namespace-private tmpfs, and executes it as a contract
check. Only after that check succeeds does the fixed loader independently
execute the preregistered acceptance argv, so evaluator code cannot silently
skip acceptance. The guard's command result and bounded timeout are authoritative
for evaluation; host cgroup-inotify capacity is diagnostic evidence only and
cannot turn a completed zero-exit acceptance into a failed calibration. Neither
a stale evaluator path nor evaluator/candidate code can bypass the namespace to
read evaluator-only host state. The protocol itself is loaded through an immutable authority reference
and must match the sandbox's certified Git baseline. Evaluation rechecks that
the actual candidate HEAD descends from that baseline, records HEAD/tree/status
plus bounded workspace-structure and Git-control inventories, overlays `.git`
read-only inside the candidate namespace, and rejects workspace mutation during
acceptance. Every mount source is canonicalized and bound to its root object
identity so a post-probe source replacement fails before launch.
Before any host Git probe, the controller parses the standalone `.git/config`
against a closed safe-key set and runs a fixed Git binary with sanitized
environment and dangerous extension points disabled.
Mutable directory mount sources additionally carry a bounded tree digest;
privileged system trees are accepted only through their root-owned,
non-group/other-writable permission boundary.
Evaluation evidence records the run, complete taskpack and invocation
identities, authority-reference digest, and aggregate seal digest. P2-05
publishes the complete retained scan roots and validates those expected
bindings when bundling results. P2-06 injects the sandbox reference into every
model-using role and returns all lifecycle roots to the common evaluator; the
capability remains incomplete until both integrations pass.

Tests use temporary Git repositories and prove cleanup preserves sealed result
artifacts.

Provider isolation is applied by one launch-policy hook inside
`ModelInvocationCall`, after supported-Codex detection and before process
spawn. Direct worker, authoring, repo-map, follow-up, and future model
invocation paths must use this hook; wrapping only a scheduler command is not
sufficient.

### P2-03A Pure Global Budget Controller

**Risk:** L1

Implement a deterministic budget controller over canonical Phase 1 usage and a
fakeable monotonic clock. Prove warning, exhaustion, one-lane overshoot,
unavailable-usage rejection, replay idempotency, and original-budget recovery.

### P2-03B Safe-Boundary Scheduler Integration

**Risk:** L2

Integrate checks at prelaunch, post-invocation, pre-integration, and
post-integration boundaries without terminating an integration transaction.
The prelaunch admission check lives on the common `ModelInvocationCall` path so
authoring, repo-map, worker, follow-up, and direct modes cannot bypass the
protocol-global lease or budget.

Fake fixtures prove wall-time stop, drain-before-stop, accepted-patch
preservation, and interruption resume without resetting usage. No scored run
supports a budget-extension artifact.

### P2-04 Operator Action Ledger

**Risk:** L1

Normalize existing manual-gate, permission, stop/resume, and corrective
guidance events into the closed action vocabulary. Enforce mode-level action
budgets and idempotent replay. Retain only digests and bounded reasons in the
ledger.

Tests prove that deterministic system actions and ordinary status inspection
do not count as interventions.

### P2-05 Result Bundle And Recovery Snapshots

**Risk:** L1

Implement versioned resumable snapshots and create-if-absent sealed terminal
bundles before the real mode adapters. Add projection and concise show/compare
rendering. Failed and budget-stopped results remain visible.

The result authority also publishes the complete retained prompt, context,
taskpack, and artifact roots used by the immutable evaluation scan-scope
reference. Raw caller-selected subsets are not accepted as complete scope.
Before sealing a result, it validates the evaluation record against the
expected run, taskpacks, protocol, acceptance command, evaluator artifact, and
multi-root invocation-manifest reference.

### P2-06 Three Execution-Mode Adapters

**Risk:** L2

Implement one controller interface with three adapters:

- single Codex invocation in the clean workspace;
- reviewed frozen taskpack execution without authoring;
- full start/pursue execution with authoring and follow-up.

Each adapter emits the same mode lifecycle result and returns to the common
evaluator/finalizer. The direct adapter verifies the declared taskpack digest;
the full adapter rejects access to it. Every path uses the protocol's one
model/reasoning profile and global provider lane.

After each author, worker-attempt, or candidate workspace exists, the
controller publishes and probes a workspace-specific immutable sandbox
reference, then injects that reference, its independent run authority root,
and `experiment_sandbox_required=true` before provider launch. These
host-specific references are runtime state and never enter a frozen taskpack.
Missing or stale references fail before launch. Before any provider launch, the
controller allocates every workspace, sandbox reference, and distinct lifecycle
authority under a fixed controller-trusted append-only run registry outside
provider-visible mounts. This is an `O_EXCL`/authority trust boundary, not
same-UID tamper-proof storage. `ModelInvocationCall` rejects unregistered
roots. Provider processes
cannot modify registry facts, and adapters do not self-report the authoritative
final root set. The controller builds the
manifest only by enumerating the complete registry and rejects missing, extra,
unconsumed, or non-terminal registrations. It reloads each exact sandbox
reference and binds its reference digest, derived policy digest, and canary
probe through lifecycle records, seals, the immutable multi-root manifest, and
common evaluation evidence.
Each allocation also publishes an immutable registration record; manifest
construction requires exact equality among that ledger, the registry
directories, and the declared lifecycle roots.
All author, worker-attempt, and candidate workspaces are standalone sanitized
repositories; the experiment adapter does not reuse AgentTeam's ordinary
linked worktrees.

Deterministic fake adapters prove orchestration before any live Codex
calibration.

Add concise CLI output:

```text
agentteam experiment run --protocol <path> --run-manifest <path>
agentteam experiment resume --run <id>
agentteam experiment show --run <id> [--json]
agentteam experiment compare --experiment <id> [--json]
```

Text output reports mode, status, acceptance, tokens and coverage, wall time,
interventions, retries, changed files, and bundle digest without raw logs.
JSON preserves the complete schema.

### P2-07A Deterministic L1/L2 Calibration

**Risk:** L1

Create a small deterministic repository fixture with a mechanical source
change and acceptance command. Execute all three modes through deterministic
adapters, then repeat one mode. Prove equal source/acceptance inputs, 100%
usage coverage, no double counting, projection rebuild equality, result
retention for `failed`, `infrastructure_failed`, `interrupted`, and
`budget_stopped`, and concise comparison output.

Add a bounded multi-file fixture with one repair opportunity. Exercise clean
snapshot, canary denial, budget stop, intervention ledger, and all bundle
statuses. This validates the harness and is not benchmark evidence.

The canonical calibration report keeps two source identities separate:

- `target_source_commit` identifies the repository revision modified and
  evaluated by every mode.
- `runtime_source_commit` and `runtime_release_identity` identify the exact
  AgentTeam implementation executing those modes.

P2-08 binds readiness promotion to the runtime source commit. It does not
require an arbitrary benchmark target to share the AgentTeam repository
commit. A deterministic calibration request may publish the report only from
already sealed runs; it cannot start a provider invocation. The retained
request bytes are digest-bound by the report. Before promotion, P2-08 also
recomputes the Git-installed release inventory, file modes, and blob OIDs
against that runtime source commit.

### P2-07B Executable Gates And Relation Validators

**Risk:** L2

Implement dedicated relation validators and deterministic controller
entrypoints for P2-08, P2-09, and P2-10. They recompute evidence and Git
relations instead of trusting schema-valid strings. Post-backlog progression
is automatic until P2-09, where the controller pauses for an epoch-bound
operator authorization and resumes after it is registered.

Also extend blueprint materialization with a controller-only taskpack form:
an empty model backlog is permitted only when deterministic post-backlog gates
are non-empty, every gate has a committed schema, relation validator, and
controller entrypoint, and the approved blueprint is bound to the candidate
release. The implementation taskpack itself completes under the preflight
release; it never attempts to execute these newly integrated controllers.

### P2-08 Readiness Promotion And Pilot Guard

**Risk:** L3

After P2-07B passes at integration commit `C`, the implementation taskpack
stops for operator review. The operator installs `C` as the candidate release
and approves/freezes the controller-only promotion taskpack. Its deterministic
controller then:

1. recomputes all capability evidence, validates the canonical deterministic
   calibration report against `C`, and creates a single-child commit `R` from
   `C` that changes only the packaged readiness record;
2. builds a candidate runtime release from `R`;
3. runs `check-pilot` against a newly bound protocol/run pair using the
   candidate release and proves zero provider calls and zero target mutations;
4. durably journals the complete P2-08 action, advances the integration branch
   to `R`, refreshes the gate epoch to `R`, and registers the complete P2-08
   evidence against that epoch.

The readiness evidence binds exact artifact digests and test IDs. A
schema-valid assertion without recomputation cannot pass. No partial P2-08
receipt is published before the pilot guard completes; recovery resumes from
the create-if-absent action journal.

### P2-09 Bounded Live Calibration

**Risk:** L2

Run the L1 fixture through all three real Codex-backed modes with one provider
lane and the same approved budget. Repeat one mode for drift. This gate starts
only after P2-08 and an explicit `phase2_live_authorization.v1` artifact binds
the current epoch, protocol, readiness receipt, model, reasoning profile,
three modes, and immutable token and wall-time budgets. Live calibration makes
no source commit.

Host resource diagnostics, including cgroup-inotify watch pressure, are retained
as operator warnings but are not calibration acceptance criteria. P2-09 still
fails closed for a nonzero evaluator or acceptance result, a real timeout,
incomplete isolation, leaked descendants, or incomplete evidence.

### P2-10 Report, Roadmap, And Operator Merge Gate

After P2-09 passes, render the Phase 2 report, update the roadmap toward
research Phase 3, and create a single-child final commit `F` from `R` changing
exactly those two paths. Refresh the gate epoch, re-register the still-valid
P2-08 and P2-09 evidence against it, but retain the P2-09 authorization's
binding to the `R` epoch where provider calls occurred. The relation validator
accepts that historical authorization only when `F` is a single-child,
report-only descendant of `R`; it never re-runs the provider or requests a
second authorization. Run full verification, validate the single-parent Git
relation and changed-path set, and stop for operator review before merge,
push, or release activation.

## Capability Promotion Rules

- `invocation_level_real_usage` passes only from the validated Phase 1
  completion promotion chain.
- `three_mode_experiment_harness` passes only after P2-07 executes all modes
  from one protocol family and publishes a canonical calibration report bound
  to the validated code commit.
- `immutable_experiment_manifest` passes only after protocol/run publication,
  stable-request binding,
  tamper, and resume tests.
- `clean_reset_and_blind_gold_isolation` passes only after independent object
  store, no-prior-state, bwrap canary denial, and evaluator separation tests.
- `actual_budget_enforcement` passes only after warning, exhaustion, overshoot,
  accepted-patch preservation, and resume tests.
- `operator_action_ledger` passes only after all supported operator input paths
  normalize and replay idempotently.
- `machine_readable_result_bundle` passes only after success, task failure,
  infrastructure failure, interruption, and budget-stop bundles reconcile
  before and after projection rebuild. The P2-08 relation validator parses the
  calibration report and rejects a digest-bound file that does not satisfy
  this terminal-status inventory.

No worker may promote a capability from prose confidence. Every promotion
requires deterministic evidence paths in the readiness record.

## Verification

Every implementation task runs focused tests for its write scope. P2-07,
P2-08, and P2-10 run:

```text
python3 -m unittest discover \
  -s experiments/native_agentteam_runtime/m0_runtime/tests \
  -p 'test*.py'
git diff --check
```

All JSON schemas, manifests, review records, readiness records, bindings,
events, ledger entries, and result bundles must validate with the packaged
runtime from the candidate integration commit.

Live provider calls are forbidden in ordinary unit tests.

## Milestone Acceptance

Phase 2 is complete only when:

- the approved blueprint materializes the exact ordered task graph;
- all three modes execute through one protocol/controller contract;
- every run starts from an independently verified clean source snapshot;
- no runtime-visible input contains gold material;
- token and wall-time budgets stop new work at safe boundaries and preserve
  resumability;
- operator actions are normalized and comparable;
- every outcome has a sealed result bundle;
- deterministic L1 and bounded L2 calibration reach 100% counted usage
  coverage and projection reconciliation;
- the readiness record truthfully reports seven passed capabilities;
- a newly digest-bound protocol/run pair passes the pilot guard before live
  calibration;
- the Phase 2 report and roadmap are updated from verified evidence;
- the full test suite passes;
- no source merge, push, or release activation occurs without operator review.

## Stop Conditions

Stop and request operator review if implementation would:

- weaken equal-input mode fairness;
- expose a direct-mode taskpack to full mode;
- claim secret-host-file isolation without an enforcing namespace or remote
  evaluator;
- infer unavailable provider token usage;
- clamp or hide budget overshoot;
- interrupt an integration transaction to enforce a budget;
- make SQLite or a mutable report the experiment authority;
- discard failed or interrupted experiments;
- change the Phase 1 invocation identity/counting contract;
- begin a scored pilot before all seven readiness capabilities pass;
- change research claims or benchmark-selection policy.

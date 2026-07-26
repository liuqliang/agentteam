# Phase 1 Execution Preflight

Status: implemented and verified; retained as Phase 1 prerequisite authority.

This plan removed five known blockers before the executable package described
by
[`2026-07-23-phase1-model-invocation-usage.md`](2026-07-23-phase1-model-invocation-usage.md)
is retained, frozen, or executed:

1. integration verification failure could leave a task marked `done` and allow
   invalid downstream dispatch;
2. semantic taskpack materialization retained only the first backlog item and
   could not faithfully construct the Phase 1 dependency graph;
3. `agentteam integrate` could merge an idle integration baseline without
   enforcing required gates that run after the backlog, while existing
   completion/report surfaces could call that intermediate state complete;
4. a run recorded its active runtime release only after execution and the
   launcher resolved the current active release on every command, so
   `continue` could silently switch a reviewed run to different code;
5. crash-safe invocation recovery needed Linux pidfd plus a persistent,
   queryable systemd user transient-service control group, while the runtime
   lacked a deterministic no-provider host capability probe.

These are runtime correctness prerequisites, not part of the token-attribution
experiment. They must be implemented, reviewed, merged, and activated before
the Phase 1 clean-source commit is recorded.

## Execution Strategy

At the start of this preflight, the active runtime did not support
deterministic multi-item blueprint materialization. PRE-00, PRE-01, PRE-02A,
PRE-02B, PRE-03A, PRE-03B, PRE-03C, PRE-03D, and PRE-04 were therefore
executed as ordered, bounded single-item taskpacks against successive verified
source heads:

```text
PRE-00 invocation-supervision host probe
  -> operator review, source merge, and active release
  -> PRE-01 verified dependency dispatch
  -> operator review, source merge, and active release
  -> PRE-02A blueprint validator and core materializer
  -> operator review, source merge, and active release
  -> PRE-02B materializer CLI and documentation
  -> operator review, source merge, and active release
  -> PRE-03A post-backlog gate authority and integrate enforcement
  -> operator review, source merge, and active release
  -> PRE-03B backlog-versus-milestone completion semantics
  -> operator review, source merge, and active release
  -> PRE-03C fresh-head operator and review surfaces
  -> operator review, source merge, and active release
  -> PRE-03D target-divergence gate-epoch recovery
  -> operator review, source merge, and active release
  -> PRE-04 immutable per-run runtime release binding
  -> full verification, operator review, and active release
  -> Phase 1 G0-G5 preflight
```

The separate packages do not express cross-package `depends_on`. The operator
enforces the sequence by requiring each package's source base to equal the
verified head produced by the preceding step. After every source merge, the
operator creates and activates a release whose manifest `source_commit` equals
that merge before authoring or running the next package; no preflight package
may execute through a stale active release. This exception ends after PRE-04;
the Phase 1 package itself must use the new multi-item path.

All tasks use:

```text
runtime_backend: codex
required_role: implementation_worker
allow_source_merge: false
allow_push: false
allow_release_activation: false
```

The worker may prepare integration-baseline commits. Source merge and release
activation remain operator actions.

## PRE-00 Invocation-Supervision Host Probe

**Risk:** L1

### Objective

Provide one bounded, explicit, no-provider probe for the Linux process
authority required by Phase 1 lifecycle recovery.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify:
  `docs/agentteam-command-reference.md`

### Command Contract

```text
agentteam doctor --invocation-supervision-probe [--json]
```

The explicit flag, not ordinary read-only `doctor`, may create one uniquely
named systemd user transient oneshot service running a built-in inert gated
helper. It must not invoke Codex or any model adapter. Within a bounded timeout
the probe:

1. verifies Linux `pidfd_open`, `loginctl` reports `Linger=yes`, and records
   the host boot ID;
2. queries the enclosing system-level `user@<uid>.service` and requires a
   stable `InvocationID`, `ControlGroup`, and `KillMode` whose completed
   stop/restart cleans all child processes;
3. creates the transient service with `RemainAfterExit=yes` and
   `KillMode=control-group`;
4. obtains and cross-checks its unit name, systemd `InvocationID`, user-manager
   identity, enclosing user-service identity, `MainPID`, process start ticks,
   and exact `ControlGroup`;
5. opens a pidfd for the inert helper, releases it, verifies the service group
   reaches `cgroup.events: populated 0` at the exact persisted `ControlGroup`
   while the unit identity remains queryable, then explicitly stops/resets the
   unit;
6. reports a compact pass/fail result and removes all probe state.

Failure, timeout, linger disabled, unsuitable enclosing `KillMode`, unexpected
unit reuse, missing identity, or incomplete cleanup exits nonzero. JSON output
contains only bounded capability fields and no environment secrets. Mocked
tests cover logout persistence, an enclosing user-service restart, an
unprovable inner-manager replacement, and all other failure branches; one
operator-run host probe is required because unit tests cannot prove the actual
user manager.

### Acceptance

- the focused mocked tests and full native-runtime discovery suite pass;
- the reviewed release is activated before later preflight tasks;
- the actual Phase 1 host probe passes once with no provider call and leaves no
  active probe unit;
- G0 records the compact probe result and release ID.

## PRE-01 Verified Dependency Dispatch

**Risk:** L1

### Objective

Make backlog completion mean that the accepted worker patch is present in a
successfully verified integration-baseline commit. A worker-level accepted
result whose integration verification fails must not satisfy dependencies.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

### Required Behavior

- compute the final backlog transition from both worker validation and
  integration outcome;
- emit `backlog_updated` with `task_status: done` only after integration
  succeeds and the verified integration head contains the accepted patch;
- on integration apply or verification failure, roll back the baseline,
  preserve the task as retryable or blocked according to existing attempt
  policy, and keep dependents undispatchable;
- preserve idempotency and replay behavior when the same result is collected
  again;
- do not weaken worker validation, integration verification, or dependency
  checks.

### Regression Cases

1. worker validation accepted, patch applies, integration verification fails:
   task is not done and dependent is not dispatched;
2. retry later passes integration verification: task becomes done once and
   dependent becomes dispatchable;
3. result replay after success: no duplicate completion or integration commit;
4. integration apply failure: baseline head is unchanged and dependency
   remains unsatisfied;
5. documentation-only accepted result with no patch follows the existing
   verified no-op policy explicitly rather than falling through by accident.

### Acceptance

- no `done` event is emitted on a failed integration outcome;
- a task cannot be reported done unless its accepted result is represented by
  the recorded verified integration head;
- all regression cases pass;
- the full native-runtime discovery suite passes;
- the operator reviews the state transition and activates the repaired release
  before PRE-02 work starts.

## PRE-02A Blueprint Validator And Core Materializer

**Risk:** L1

### Objective

Add a strict, deterministic API that converts a tracked multi-item blueprint
into the existing five-file taskpack format without invoking a model or
truncating the backlog.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Add:
  `experiments/native_agentteam_runtime/schemas/taskpack_blueprint.schema.json`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Required API

```text
materialize_taskpack_blueprint(
    project_root,
    blueprint_path,
    output_root,
    taskpack_id=None,
    dry_run=False,
) -> materialization_summary
```

The implementation must:

- validate `agentteam_taskpack_blueprint.v1` before creating output;
- reject unknown top-level and task fields;
- require non-empty unique task IDs, known dependency IDs, an acyclic graph,
  declared roles, supported risk levels, non-empty objectives and
  deliverables, non-empty acceptance criteria and stop conditions, valid input
  artifacts, explicit string-array blockers, and non-empty JSON string-array
  verification commands;
- reject absolute paths, `..` traversal, and scopes outside the target
  repository;
- generate the existing five-file format: `taskpack.yaml`,
  `agent_pool.json`, `backlog.json`, `verification.json`, and `README.md`,
  using existing structured writers; blueprint policy is stored in the
  existing `taskpack.yaml` policy object;
- construct scheduler/integrator entries and role runtime profiles through the
  existing agent-pool helpers; the blueprint `agents` list declares execution
  workers and must not duplicate scheduler control-plane records;
- preserve the blueprint's supported runtime-profile fields and let the active
  project/runtime defaults resolve model and Codex settings; do not invent a
  model or unsupported abstract capability field;
- preserve task IDs, order, dependencies, scopes, roles, risk, objective,
  goal alignment, deliverables, acceptance criteria, stop conditions, and input
  artifacts and blockers exactly;
- preserve validated `post_backlog_gates` declarations in `taskpack.yaml`;
- preserve and validate `operator_review_required`, approval schema, and
  required decision for review-gated declarations;
- render declared post-backlog gate IDs and dependencies into generated
  `README.md` without claiming they have passed;
- allow a gate schema to be a declared future task output only when its path is
  inside an ancestor task's write scope; otherwise require it to exist at
  materialization time;
- record source-plan and blueprint SHA-256 values in taskpack context;
- copy the approval-bound runtime release ID, release source commit, and Git
  object format into retained draft and frozen taskpack context;
- validate a blueprint approval block, its review-record schema, required
  decision, and declared source/contract digests before any non-dry output or
  freeze;
- read `git rev-parse --show-object-format`, validate the approval record's
  source commit as a Git OID of that format rather than as a 64-character file
  SHA-256, and require it to equal the release manifest source commit;
- validate the generated package using `validate_taskpack()`;
- write a bounded `materialization_manifest.json` beside the draft containing
  blueprint digest, generated artifact digests, task IDs, dependency edges,
  and validation status;
- before freeze, require the external materialization provenance, reject
  draft-side provenance downgrade, deterministically regenerate the five
  blueprint artifacts from the currently approved tracked blueprint, reject
  byte drift, and atomically publish the regenerated staging snapshot;
- remove partial output on failure.

`dry_run=True` may validate and generate into a controller-owned temporary
directory without an approved review record. It returns the manifest content
with `freeze_eligible: false` and removes the generated package before
returning. It cannot freeze, register, or execute a taskpack.

The blueprint and source plan are tracked inputs. Generated five-file packages
and manifests remain execution artifacts under the configured work root and
must not embed absolute project/worktree paths in committed source.

### Regression Cases

1. a three-task blueprint materializes all three tasks and two dependency
   edges;
2. output preserves input order but scheduling depends on explicit edges;
3. unknown dependency, duplicate ID, cycle, unknown field, absolute scope, and
   path traversal each fail before output is committed;
4. a materializer implementation that writes only `items[0]` fails the exact
   manifest comparison;
5. repeated materialization into separate empty output roots is byte-stable
   except for explicitly allowed generated timestamps, which should be avoided
   where possible;
6. existing one-task semantic materialization remains compatible;
7. a missing, stale-digest, pending, rejected, or escalated approval record can
   produce dry-run diagnostics but cannot produce a retained draft or frozen
   package.

Every generated backlog item uses the tracked blueprint itself as an
`input_artifacts` entry. The existing worker prompt therefore requires the
worker to read the blueprint and select the object matching its dispatched
`task_id`; no Markdown heading parser or second semantic author call is needed.

### Acceptance

- the tracked Phase 1 blueprint validates;
- after the recovered P1-01 seed is committed, its v2 dry materialization
  contains exactly 10 remaining tasks, P1-02A through P1-06D, and 9 declared
  dependency edges;
- all negative cases fail closed;
- package validation succeeds;
- focused tests and the full native-runtime discovery suite pass.

## PRE-02B Blueprint Materializer CLI

**Risk:** L1

### Objective

Expose PRE-02A through the existing taskpack command family while preserving
the current semantic-skeleton interface.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify:
  `docs/agentteam-command-reference.md`

### Command Contract

Add `--blueprint-file` as a mutually exclusive materialization source:

```text
agentteam taskpack materialize \
  --blueprint-file <tracked-blueprint.json> \
  [--project-root <target-repository>] \
  --output-root <draft-root> \
  [--taskpack-id <id>] \
  [--dry-run] \
  [--freeze --frozen-root <root>] \
  [--json]
```

`--project-root` defaults to the current directory. `skeleton_taskpack_dir` is
required only for `--semantic-json` and
`--semantic-json-file`. Blueprint materialization does not require a skeleton
because the blueprint contains every executable field. Existing invocations
remain valid. `--dry-run` and `--freeze` are mutually exclusive.
For blueprint mode, `--taskpack-id` may be omitted or must equal the declared
blueprint ID; an approved blueprint cannot be renamed during materialization.

### Acceptance

- text output is compact and includes taskpack ID, task count, edge count,
  validation status, blueprint digest, draft path, and optional frozen path;
- JSON output exposes the same structured values and manifest path;
- freeze occurs only after blueprint and generated-package validation pass;
- retained materialization and freeze require the current active release ID and
  `source_commit` to match the blueprint approval record and its declared Git
  object format;
- dry-run output is explicitly non-freezable, reports the exact in-memory
  manifest, and leaves no draft/frozen package;
- CLI failures leave no frozen package;
- existing semantic materialization CLI tests remain green;
- documentation examples match the implemented parser;
- the full native-runtime discovery suite passes.

## PRE-03A Post-Backlog Gate Authority And Integrate Enforcement

**Risk:** L1

### Objective

Make backlog-external controller gates part of the executable integration
contract so `agentteam integrate` cannot bypass P1-LIVE or P1-06E.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Add:
  `experiments/native_agentteam_runtime/schemas/post_backlog_gate_receipt.schema.json`
- Add:
  `experiments/native_agentteam_runtime/schemas/post_backlog_gate_epoch.schema.json`
- Add:
  `experiments/native_agentteam_runtime/schemas/post_backlog_gate_approval.schema.json`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Gate Contract

When a frozen taskpack declares `post_backlog_gates`, run initialization creates
an `awaiting_validated_baseline` gate state. After the frozen backlog reaches
verified idle, the operator runs:

```text
agentteam gate seal-baseline \
  --taskpack <implementation-run-id> \
  --expected-integration-head <sha> \
  --authorize-revalidation
```

After acquiring and retaining the locks defined below, the command freshly
requires no existing epoch, a clean current integration worktree, the expected
head still resolved from Git, zero open controller invocations, and the frozen
full-verification command. It reruns that command and atomically publishes
epoch 1 with both gates pending. No live controller may register or launch
before this seal succeeds.

Epoch authority is stored below:

```text
<implementation-run-dir>/state/post_backlog_gates/epochs/<N>/epoch.v1.json
<implementation-run-dir>/state/post_backlog_gates/epochs/<N>/receipts/
<implementation-run-dir>/state/post_backlog_gates/epochs/<N>/approvals/
```

The epoch record conforms to
`post_backlog_gate_epoch.schema.json` and binds the implementation run, epoch
number, prior epoch digest or null, gate-declaration digest, Git object format,
target branch/head, integration branch/head, `validated_code_sha`, frozen
verification-command digest, verification-result digest, and creation time.
The current epoch is the highest contiguous, digest-linked, schema-valid
numeric epoch. The controller builds it outside `epochs/`, flushes its files,
and uses one no-replace directory rename plus parent-directory flush to publish
it. A run-level gate lock serializes seal, refresh, register, approval, and
integrate mutation; gaps, conflicting epoch directories, or an invalid highest
record fail closed.
Each epoch/gate also has one OS-backed controller-execution lock. A controller
holds it from immediately before evidence registration through success or
bounded failure publication. The lock is coordination, not evidence, and is
never projected. A crash releases it, but a new attempt still cannot launch
until every prior acceptance invocation for that epoch/gate is terminal or
safely recovered.

All mutating paths follow one lock-and-revalidate protocol:

1. acquire the run-level gate-state lock;
2. acquire the required epoch/gate execution locks in sorted gate-ID order
   without blocking; on any failure, release all locks and report the active
   controller;
3. while those locks are held, freshly reread the current epoch, every
   controller claim/open invocation, Git refs and worktree status, expected
   digests, and command preconditions;
4. perform the bounded mutation and atomic publication without releasing the
   locks, then release in reverse order.

Seal and refresh take all declared gate locks. A gate controller takes its own
gate lock and retains both it and the run-level lock through bounded failure or
success publication. Read-only status may inspect atomic records without the
mutator lock and reports an active controller from its claim. A read-only gate
evaluation captures the current epoch digest before reading, rereads it after
validation, and retries or fails closed if it changed. Integrate and operator
approval fail fast when the mutator lock is held. A pre-lock check is only a
hint and can never authorize mutation.

After an epoch exists, a controller registers where evidence will appear:

```text
agentteam gate register \
  --taskpack <implementation-run-id> \
  --gate <gate-id> \
  --gate-epoch <current-epoch> \
  --evidence-run <registered-evidence-run-id> \
  --expected-integration-head <sha>
```

Project root and work root follow the existing default/override conventions.
Registration:

- writes one atomic structured receipt under the implementation run's
  epoch-scoped `state/` directory;
- proves the evidence run is under the same configured project work root;
- binds the repository Git object format, expected integration-head Git OID,
  current gate epoch, and relative artifact declaration;
- does not mark the gate passed;
- may replace a pending/failed evidence run while retaining bounded attempt
  history; byte-equivalent registration of a passed receipt is idempotent, and
  a conflicting passed receipt cannot be replaced.

Gate status is derived on every status/integrate check by validating:

```text
declared gate dependency
registered evidence path
artifact schema and digest
required controller status field/value
repository Git object format
declared commit relation to current integration baseline head
operator approval when the gate declaration requires review
```

The gate evaluator resolves the integration branch head from Git on every
check, using the branch named by the current immutable epoch record; it must
not trust a stale head cached before the report-only commit. It loads the
evidence and operator-approval schema bytes from that exact commit with
`git show <resolved-head>:<schema-path>`, validates those bytes, and retains
their digest in the derived decision. It rejects a missing committed schema and
rejects staged, unstaged, or untracked changes at either schema path in the
integration worktree. It must not load a schema from the working-tree
filesystem, older installed release, or target source checkout.

For Phase 1, P1-LIVE requires `validated_code_sha` to be an ancestor of the
current integration head. P1-06E requires `final_report_sha` to equal that head
and depends on P1-LIVE. A valid P1-06E controller artifact changes its derived
state only to `awaiting_operator_review`; it cannot pass the gate by itself.

After inspecting the final report, diff, coverage, evidence digests, and
integration head, the operator runs:

```text
agentteam gate approve \
  --taskpack <implementation-run-id> \
  --gate P1-06E \
  --gate-epoch <current-epoch> \
  --expected-evidence-sha256 <sha256> \
  --expected-integration-head <final-report-sha> \
  --approve
```

This command is excluded from every worker/controller command allowlist,
rejects scheduler/worker execution context, requires an interactive TTY and
literal confirmation, and has no `--force` or non-interactive approval path.
Under the lock-and-revalidate protocol it rereads the current epoch, fixed
controller artifact, evidence and epoch digests, clean integration head, and
two-path report diff. It then atomically publishes one immutable
`post_backlog_gate_approval.v1` record containing the operator identity,
decision `approved`, epoch and gate IDs, epoch/evidence/diff digests,
`final_report_sha`, Git object format, and review timestamp. A conflicting
approval cannot replace it. The evaluator passes P1-06E only when this record
conforms to `post_backlog_gate_approval.schema.json` and exactly matches the
still-current evidence and head.

### Required Behavior

- `agentteam status`, `report`, and review hints show each declared gate as
  pending, failed, or passed with one bounded next action;
- verified-idle state without epoch 1 recommends `gate seal-baseline`, not a
  live call or integration;
- integration review commands omit `agentteam integrate` while a required gate
  is not passed;
- a controller-valid P1-06E artifact reports `awaiting_operator_review` and
  exposes the bounded `gate approve` command; it does not emit
  `run_completed` or expose integration before approval validates;
- normal and `--record-only` integrate paths fail closed before
  any branch/status mutation when a required gate is pending, failed, missing,
  stale, schema-invalid, or commit-mismatched;
- `agentteam integrate --rebase` is rejected before mutation whenever the
  taskpack declares a commit-bound post-backlog gate, even after all gates
  passed. Rebase would rewrite the commits the evidence validates; target
  divergence remains blocked until PRE-03D provides `gate refresh-baseline`;
- a stale epoch, controller claim, artifact, or receipt fails closed;
- different evidence attempts for one epoch/gate cannot run concurrently;
  seal and registration require every relevant gate-execution lock to be
  acquirable and zero
  open controller invocations;
- gate receipts and attempts remain file authority and are included in
  projection/replay later without making SQLite authoritative;
- no standard `--force` option bypasses a declared gate.

### Regression Cases

1. idle verified backlog with P1-LIVE pending: status recommends the live gate
   and integrate is rejected;
2. valid P1-LIVE but P1-06E pending: integration remains rejected;
3. both artifacts valid and commit relations match: normal fast-forward
   integrate remains blocked until matching operator approval, then may
   proceed;
4. wrong evidence root, path traversal, schema mismatch, failed status, stale
   digest, dirty schema path, wrong ancestor, wrong final head, or tampered
   receipt: gate fails;
5. a 40-character SHA-1 or 64-character SHA-256 Git OID validates only when it
   matches the repository's declared object format;
6. registering a retry preserves failed attempt history and cannot replace a
   passed receipt;
7. `--rebase` with a commit-bound gate fails before branch mutation even when
   both receipts currently pass;
8. verified-idle backlog without an epoch cannot register evidence; successful
   seal reruns full verification and publishes exactly one epoch 1;
9. concurrent seal/register operations serialize, and a staged or
   invalid epoch cannot become current; changing a ref or starting a controller
   between a preliminary check and lock acquisition is caught by the mandatory
   in-lock reread;
10. concurrent same-ID or different-ID attempts for one epoch/gate permit only
    one controller execution; a crashed attempt's open invocation blocks the
    next attempt until safe recovery;
11. a stale epoch controller cannot launch a provider, create a report commit,
    register evidence, or satisfy a current gate;
12. missing, non-interactive, stale-epoch, wrong-head, wrong-evidence,
    tampered, or conflicting operator approval leaves P1-06E awaiting review;
13. worker/controller context cannot invoke the operator approval path;
14. a taskpack without post-backlog gates retains existing integration
    behavior.

### Acceptance

- the tracked Phase 1 blueprint's two gate declarations validate and survive
  dry materialization;
- all regression cases pass before any source-merge command is exposed;
- P1-06E can pass only from matching controller evidence plus immutable
  interactive operator approval;
- focused tests and the full native-runtime discovery suite pass.

## PRE-03B Backlog And Milestone Completion Semantics

**Risk:** L1

### Objective

Prevent a verified-idle backlog from being reported or notified as a completed
run while required post-backlog gates are still pending.

### Files

- Modify:
  `experiments/native_agentteam_runtime/schemas/event.schema.json`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/`

### Required Behavior

- a run with required post-backlog gates emits `backlog_completed` when its
  backlog reaches verified idle and enters
  `awaiting_post_backlog_gates`, not `completed`;
- before epoch 1 is sealed, its bounded next action is
  `gate seal-baseline`; afterward the next action comes from the first pending
  gate;
- `backlog_completed` is added to the canonical event contract before the
  scheduler emits it;
- gated taskpacks are rejected by `--one-shot` and lower-level simulation
  taskpack launch paths because those paths cannot remain alive for external
  controller gates;
- scheduler completion summaries and Feishu notifications state which gate is
  next and never recommend integration in that state;
- after the scheduler has exited, the gate command path emits
  `run_completed` exactly once only when the gate evaluator proves every
  required gate, including any declared operator approval, passed against the
  fresh integration head;
- failed or stale gates keep the milestone incomplete and expose one bounded
  recovery action;
- taskpacks without post-backlog gates preserve current completion behavior.

### Regression Cases

1. backlog idle with P1-LIVE pending emits `backlog_completed` and no
   `run_completed` notification;
2. P1-LIVE passed with P1-06E pending remains
   `awaiting_post_backlog_gates`;
3. controller-valid P1-06E emits review-required output but no completion; its
   matching operator approval emits one replay-idempotent `run_completed`
   event and one completion notification;
4. replay and repeated status do not resend completion;
5. a later stale/tampered receipt cannot leave a cached completed status.
6. `--one-shot` refuses a taskpack with post-backlog gates before dispatch but
   remains compatible for a taskpack without them.

### Acceptance

- backlog completion and milestone completion are distinct structured states;
- no scheduler or notification surface claims success before both Phase 1
  gates pass; PRE-03C applies the same rule to operator report/review surfaces;
- focused tests and the full native-runtime discovery suite pass.

## PRE-03C Fresh-Head Operator And Review Surfaces

**Risk:** L1

### Objective

Make every operator-facing integration view use the same current Git branch
head as gate evaluation after controller-created commits.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Required Behavior

- status, report, paths, review hints, diff commands, and integrate resolve the
  current gate epoch and its integration branch ref from Git for each request;
- cached scheduler-state heads remain historical diagnostics and cannot drive
  a merge recommendation or diff range;
- after the report-only commit, all surfaces show that commit, its two changed
  report paths, the P1-06E gate relation, and either the digest-bound operator
  approval command or the validated approval identity;
- after an epoch refresh, no surface reads a prior epoch branch or recommends
  its otherwise-valid receipts;
- a missing branch, detached/deleted worktree, or mismatch between branch ref
  and recorded baseline fails closed with one repair action.

### Acceptance

- operator review cannot omit the report-only commit because of a stale cached
  head;
- every integration-facing surface agrees on one freshly resolved head;
- focused tests and the full native-runtime discovery suite pass.

## PRE-03D Target-Divergence Gate-Epoch Recovery

**Risk:** L1

### Objective

Recover a commit-bound gated run when the target branch advances, without
rebasing validated commits, rewriting prior evidence, or restarting the frozen
implementation backlog.

### Files

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Command Contract

```text
agentteam gate refresh-baseline \
  --taskpack <implementation-run-id> \
  --expected-gate-epoch <current-epoch> \
  --expected-target-head <fresh-target-head> \
  --authorize-revalidation
```

Using the PRE-03A protocol, the command first acquires the run-level lock and
all gate-execution locks. While they remain held, it freshly requires the
current implementation run and controllers to be idle, no controller
invocation to remain open, clean target and current integration worktrees,
expected epoch and target head to still match newly resolved refs, and the
frozen taskpack to expose a deterministic full-verification command. Only then
does it:

1. creates a new candidate epoch branch and external worktree from the prior
   epoch's recorded `validated_code_sha`, never from its report-only commit;
2. merges the freshly resolved target head into that candidate without rebase;
3. runs the frozen full-verification command in the candidate;
4. on conflict or verification failure, removes the candidate and leaves the
   current epoch, branch, receipts, and artifacts unchanged;
5. on success, atomically publishes the next digest-linked immutable epoch
   directory naming the new branch, target head, merge head as the new
   `validated_code_sha`, parent epoch, verification digest, and both
   post-backlog gates as pending.

The new branch name includes the monotonically increasing epoch number.
Operator surfaces and controllers resolve only the current published epoch.
Every controller must supply the expected epoch and record it in its claim,
artifact, and receipt; stale controllers fail before provider launch, report
commit, or receipt publication. Prior epoch branches, receipts, approvals,
artifacts, and invocation-cost records remain immutable and queryable, but
cannot satisfy a gate in the new epoch. P1-LIVE must run as a fresh acceptance
attempt, and the P1-06E `complete` controller must create a fresh report-only
commit whose parent is the new `validated_code_sha`.

### Regression Cases

1. target divergence followed by successful refresh creates epoch N+1 from the
   prior `validated_code_sha`, includes the target head, passes full
   verification, preserves old evidence, and resets both gates pending;
2. the refreshed integration head is a descendant of the target head, so a
   later accepted batch remains fast-forward integrable;
3. merge conflict, failed verification, stale expected target/epoch, active
   controller, open controller invocation, or crash before epoch publication
   leaves epoch N authoritative;
4. concurrent refresh attempts serialize and only one can publish epoch N+1;
5. a controller/ref/worktree change between a preliminary check and lock
   acquisition fails the in-lock reread without creating a candidate epoch;
6. prior passed receipts cannot satisfy the new epoch, and old cost records
   remain explicitly queryable;
7. a stale epoch controller cannot launch a provider, create a report commit,
   register evidence, or satisfy a current gate;
8. a second target advance repeats the process as epoch N+2 without rewriting
   either earlier epoch.

### Acceptance

- successful refresh is transactional, preserves prior evidence, and exposes
  both gates as pending on the new branch;
- all failure cases leave the prior epoch and its operator surfaces unchanged;
- the refreshed epoch can complete both gates and fast-forward from its bound
  target head without rewriting a prior validated commit;
- focused tests and the full native-runtime discovery suite pass.

## PRE-04 Immutable Run Runtime Release Binding

**Risk:** L1

### Objective

Bind each launcher-managed implementation run to the exact reviewed runtime
release before its first runtime subprocess and make all continuations execute
that release rather than whatever release is currently active.

### Files

- Modify: `agentteam`
- Modify: `scripts/install-local.sh`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/release_manager.py`
- Add:
  `experiments/native_agentteam_runtime/schemas/runtime_release_binding.schema.json`
- Add:
  `experiments/native_agentteam_runtime/schemas/run_identity.schema.json`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

### Binding Contract

Before the first scheduler/runtime subprocess of a launcher-managed
implementation run, build a staged run directory containing both immutable
records:

```text
<run_dir>/state/runtime_release_binding.v1.json
<run_dir>/state/run_identity.v1.json
```

Flush both records and the staged directories, then publish the entire run
directory from same-filesystem `<work_root>/run-staging/` with one Linux
no-replace rename into `<work_root>/runs/` and flush that parent. Identity and
release binding therefore become visible together; a crash leaves only a
non-candidate staged directory outside `runs/`. Status/doctor reports stale
staging separately; bounded garbage collection may remove it only after its
owner is proven dead.

The record contains the release ID, release root, runtime root, release
manifest digest, source commit Git OID, Git object format, binding timestamp,
and the approval-bound expected release fields copied from the frozen
taskpack. The executing launcher exports its selected release identity to the
runtime; binding uses that already-loaded identity rather than rereading a
possibly changed active pointer.

`run_identity.v1.json` records at least `project_key`, `run_id`, logical
`taskpack_id`, `run_kind`, diagnostic `created_at`, and optional
`implementation_run_id`, `gate_epoch`, and `creation_sequence`. Normal
scheduler runs use `run_kind: implementation` and require a positive,
project-unique `creation_sequence`; controller evidence runs use
`run_kind: acceptance_evidence` and require a logical implementation run ID
plus positive gate epoch, but never participate in implementation sequencing.
Acceptance controllers atomically publish this marker immediately after
claiming their run directory and before gate registration or provider launch.
They do not adopt the active release: they load the commit-verified candidate
worktree explicitly and bind its module root, commit, implementation run, and
gate epoch in `controller_claim.v1.json`.
Legacy directories without the marker remain explicitly readable but are
excluded from implicit latest selection. An explicit operator adoption may
write a release binding and allocate an immutable identity/sequence before
continuation; approved Phase 1 runs cannot use that path.

### Deterministic Latest-Run Selection

All implementation-run creation paths hold
`<work_root>/state/run_creation.lock`, scan paired schema-valid identities and
release bindings in direct, non-symlink children of `<work_root>/runs/`,
allocate
`creation_sequence = max(valid implementation sequences) + 1`, atomically
stage the run directory with identity and release binding, publish it with the
paired-record rename above, and flush both parent directories before execution.
A schema-valid `acceptance_evidence` identity is a valid excluded child and
does not require a release binding or creation sequence. An implementation
child missing either complete paired record, or any direct child with missing
or invalid identity, blocks implicit selection and cannot later acquire or
repair identity automatically. Duplicate sequences, identity/directory name
mismatch, wrong project key, symlinks, or invalid records fail closed.

Implicit latest selection considers every paired schema-valid
`run_kind: implementation` candidate, including failed, terminal, and
nonterminal runs, and chooses the unique greatest `creation_sequence`.
Command-specific eligibility is checked only after selection: for example,
`continue` fails explicitly when that newest run is not resumable and never
silently falls back to an older run. Operators select an older run only by
explicit taskpack/run-dir identity. File mtime, directory mtime, lexical run
ID, and evidence-run creation time never affect ordering.

The launcher is the sole authority for implicit selection before runtime
import. It passes the resolved run path, identity digest, and selection mode to
the bound runtime, which validates but does not reselect. Lower-level direct
module execution must provide an explicit run/taskpack and cannot perform
implicit latest selection. Thus launcher and CLI cannot disagree on two
independent implementations of "latest."

For `continue` and every other execution-resuming command, the launcher locates
the selected run before importing `agentteam_runtime`, honoring explicit
`--run-dir`/`--taskpack` or the sequence rule above. It validates the immutable
binding and release manifest, and imports from the bound runtime root. A
concurrent `agentteam update` may change future runs but cannot change this
selection. Release garbage collection treats a valid binding as a protection
reference.

For an initial `agentteam run`, which currently has no `--project-root`, the
launcher derives the work root from the frozen taskpack path and `--run-root`,
reads the frozen approval-bound release identity directly as bounded JSON, and
selects that release before importing runtime code. It fails closed when those
locations disagree.

The reviewed PRE-04 launcher must be installed through
`scripts/install-local.sh` after source merge. Verification invokes the actual
`command -v agentteam` path, not only the release checkout's module or launcher.
The installer reports the installed launcher digest so the operator can compare
it with the reviewed PRE-04 source.

Existing unbound legacy runs remain readable. Continuing one requires an
explicit operator adoption path that records and displays a binding before any
worker/provider action. An approved Phase 1 taskpack cannot use legacy
adoption: its frozen expected release ID, source commit, and Git object format
must exactly match the loaded release and its new binding.

### Regression Cases

1. active release changes after a run starts: `continue` still imports the
   bound release;
2. active pointer changes between launcher selection and run setup: binding
   records the already-loaded release, or execution fails before subprocess
   launch;
3. missing release root, manifest mismatch, source-commit mismatch, or tampered
   binding blocks continuation;
4. a bound release is protected from garbage collection;
5. a new Phase 1 run rejects a release different from its approval-bound
   frozen context;
6. an unbound legacy run is readable but cannot silently adopt the active
   release or enter implicit latest selection;
7. explicit run-dir, explicit taskpack, and implicit latest-run continuation
   aimed at the same run select the same binding, and runtime validates the
   launcher selection rather than reselecting;
8. initial `agentteam run` resolves the approval-bound release from frozen
   taskpack plus run-root without requiring `--project-root`;
9. a newer `acceptance_evidence` directory does not replace the latest
   implementation run selected by status, report, or continue, and its identity
   requires an implementation run ID plus positive gate epoch without a
   release binding or creation sequence;
10. a temporary installed launcher selected through `PATH` passes the active
    release switch and bound-continuation test.
11. concurrent implementation-run creation allocates distinct monotonic
    sequences; duplicate sequence, wrong project, symlink, incomplete identity,
    missing paired binding, and directory-name mismatch fail closed;
12. touching directories or creating newer evidence runs does not change the
    selected implementation run;
13. the newest failed or non-resumable implementation run remains latest:
    implicit `continue` reports that state and never falls back to an older
    resumable run;
14. an explicitly selected older implementation run remains available when its
    identity and release binding validate.

### Acceptance

- the binding exists and validates before the first runtime child starts;
- initial execution and continuation report the same release ID, source
  commit, and runtime module root;
- changing the active release affects only unstarted runs;
- the installed machine launcher digest matches the reviewed PRE-04 launcher
  and the no-provider continuation probe uses that installed path;
- run-kind filtering keeps evidence runs out of default implementation-run
  selection;
- unique creation sequences, not mtimes or lexical names, determine latest;
- launcher-selected identity and runtime-validated identity are identical;
- focused tests and the full native-runtime discovery suite pass.

## Final Preflight Verification

Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
  python3 -m unittest discover \
  -s experiments/native_agentteam_runtime/m0_runtime/tests \
  -p 'test*.py'
```

Validate the schema and tracked Phase 1 blueprint:

```bash
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/taskpack_blueprint.schema.json
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/post_backlog_gate_receipt.schema.json
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/post_backlog_gate_epoch.schema.json
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/post_backlog_gate_approval.schema.json
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/runtime_release_binding.schema.json
python3 -m json.tool \
  experiments/native_agentteam_runtime/schemas/run_identity.schema.json
python3 -m json.tool \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-07-23-phase1-model-invocation-usage.blueprint.json
git diff --check
```

Then materialize the Phase 1 blueprint into an external draft root and compare
the manifest's task IDs and edges to the blueprint in explicit dry,
non-freezable mode. Do not retain it as the execution draft or freeze Phase 1
until its P1-00 approval record is digest-valid.

Before declaring preflight complete, inspect the active release pointer and
manifest mechanically. Its `source_commit` must equal the reviewed PRE-04
integration commit. Invoke that release's CLI directly and require its
`taskpack materialize --help` to expose blueprint/dry-run options and its gate
command family to expose register/status. Run a no-provider binding probe that
changes the active pointer after run initialization and proves continuation
still selects the original release. Then reinstall the reviewed launcher,
verify its digest, and repeat the probe through the executable returned by
`command -v agentteam`. The reviewed test evidence at the same source commit
proves verified dependency dispatch and milestone completion semantics.
Finally run `agentteam doctor --invocation-supervision-probe` through that
installed launcher and require the bounded no-provider result to pass.

## Completion Evidence

The prerequisite milestone has one compact report containing:

```text
PRE-00 source commit, active release ID, and host-probe result
PRE-01 source commit and active release ID
PRE-02A/PRE-02B source commits and active release IDs
PRE-03A/PRE-03B/PRE-03C/PRE-03D source commits and active release IDs
PRE-04 source commit and final active release ID
active release source_commit equality
active release CLI capability probes
installed launcher path and digest
runtime release binding/continuation probe
focused and full verification results
Phase 1 blueprint validation result
dry materialization task count and dependency-edge count
generated package validation result
operator-review gate enforcement result
remaining blockers
operator decision
```

No prerequisite task may merge, push, activate a release, approve P1-00, or
freeze the Phase 1 package on its own.

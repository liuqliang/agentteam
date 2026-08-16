# Phase 3 Adaptive Checkpoint And Controller Verification

Status: live mechanism check completed; retained for follow-up, not defaulted

## Decision

- Decision ID: `DEC-P3-adaptive-checkpoint-controller-verification-v1`
- Parent: `DEC-P3-bounded-worker-turn-experiment-v1`
- Authority: `L2` evaluation decision
- Claim status: hypothesis pending a controlled live comparison

Evaluate an adaptive implementation path that preserves recoverability without
paying for a separate model turn at every nominal phase boundary. The target
policy is:

1. materialize a task-bound deterministic repository handoff;
2. when that handoff has no declared semantic gap, do not launch a dedicated
   model `locate` turn;
3. launch one implementation worker against the frozen taskpack, handoff, and
   persistent worktree;
4. create a semantic checkpoint only after material code progress, an actual
   subsystem boundary, a recoverable block, or a settled budget boundary;
5. let the controller run declared deterministic verification commands;
6. launch a repair worker only when a verification failure needs semantic code
   diagnosis.

This decision does not claim that model localization is unnecessary. The
implementation worker may inspect and localize code as part of implementation.
It removes only the mandatory fresh model invocation whose sole role is to
produce a locate handoff that the next worker is likely to re-check.

## Motivation

The completed fixed-turn experiment used `820040` tokens across its live
startup and continuation, only `1.19%` fewer than the retained `829955`-token
long worker. It increased uncached input by `61.08%`, increased provider time by
`18.32%`, and caused the implementation turn to reread 15 of the locate turn's
17 source paths. The settled budget then prevented the model verify turn from
starting.

The negative result rejects fixed `locate -> implement -> verify` model slicing
as a default. It does not yet establish that this adaptive replacement reduces
cost or preserves quality.

## Frozen Comparison

Use the same benchmark problem as the preceding experiment:

- instance: `iterative__dvc_2.19.0_2.20.0`;
- source commit: `78dd045d29f274960bcaf48fd2d055366abaf2c1`;
- corrected task scope including the required `dvc/stage/*` paths;
- model: `gpt-5.6-sol`;
- reasoning profile: `high`;
- implementation route: the same `28/40` tool budget used by the valid v2
  implementation turn;
- evaluator: the same production-only SWE-EVO evaluator path;
- no access to the gold patch or evaluator-only state.

Before a provider call, regenerate the deterministic repository handoff for the
exact task ID and objective. Do not reuse a handoff with mismatched lineage and
do not give the implementation worker the previous model locate checkpoint.

The retained comparisons are:

- historical one-call implementation worker: `829955` total tokens;
- fixed-turn valid v2: `766760` total tokens before verify;
- fixed-turn aggregate including invalid startup: `820040` total tokens.

No historical control is rerun.

## Controller Verification Policy

The implementation result must publish bounded `verification_additions` as
structured command arrays. The controller validates each command against the
frozen command and environment policy, then executes it without a model call.

Route the outcome deterministically:

- all commands pass: seal the candidate and do not launch repair;
- environment or dependency failure: use the environment-recovery lane or
  block with evidence; do not ask a code-repair worker to change source;
- code-semantic failure with actionable diagnostics: launch at most one repair
  worker;
- ambiguous policy, scope, or architecture failure: stop at the existing
  review or semantic-authority gate.

When repair is admitted, its handoff contains the failing command, exit status,
bounded output, failure location when mechanically known, observed result, and
admissible alternatives when the controller can derive them without guessing.
The controller must not invent semantic alternatives merely to fill a schema.

## Checkpoint Admission

A checkpoint is allowed only when at least one condition is true:

- the worktree contains a material candidate patch;
- the next action crosses into a newly discovered subsystem whose scope was not
  present in the frozen handoff;
- the worker has a recoverable blocker and can state a bounded next action;
- the provider invocation has settled and the controller must enforce a budget
  or operator boundary;
- shutdown, cancellation, or infrastructure recovery requires durable state.

Elapsed time, command count, or a nominal phase label alone is not sufficient.
Git/worktree state remains code authority. The semantic checkpoint contains
only decision-relevant findings, verification evidence, remaining objective,
and exact lineage; it is not a replacement for source code.

## Acceptance And Metrics

Provider-free acceptance must prove:

1. an implementation-first schedule does not require a locate predecessor;
2. restart does not repeat a settled provider invocation;
3. controller verification is command-policy checked, recorded, and excluded
   from provider token totals;
4. a passing verification path launches no repair model;
5. environment failures launch no code-repair model;
6. code-semantic failures can launch one repair with bounded diagnostics;
7. stale Git state and task lineage still fail closed.

The live comparison records:

- total, cached, uncached-input, output, and reasoning tokens by invocation;
- provider, controller-verification, and evaluator wall time;
- model invocation and command counts;
- source paths read and cross-invocation rereads;
- deterministic execution-state bytes, repeated commands, and the number of
  commands that a conservative freshness rule could have reused;
- checkpoint count and bytes;
- changed files, stable patch ID, focused verification result, F2P, P2P,
  partial score, and resolved status.

The adaptive policy is promising only if it produces a scope-complete candidate,
reaches controller verification, and improves total or uncached-input cost over
the fixed-turn treatment without a worse official result. One instance is a
causal mechanism check, not evidence of general benchmark superiority.

## Avoid Changing Multiple Factors At Once

During the first live comparison, the deterministic execution record only
observes what happens. It records file reads, worktree changes, commands,
results, and whether earlier results might still be reusable. It does not add
new information to the running model conversation, stop a worker command, or
replace a command with an earlier cached result. The only retained recovery
optimization is reusing a provider invocation that has already completed.

This keeps the live treatment focused on the frozen independent variable:
remove the dedicated locate and model-verify calls while preserving one
implementation worker and controller verification. If the records show that
the worker repeats a meaningful amount of still-valid work, a separate later
experiment may test these two changes:

- before a new model call, provide a short summary of the current execution
  state;
- before running a command, check whether an identical and still-valid result
  already exists. A file read is reusable only when the file content is
  unchanged. A verification result is reusable only when the command, working
  directory, environment, source commit, and complete worktree state are all
  identical.

These changes require their own experiment and result. They must not be mixed
into the first adaptive-checkpoint score.

## Research Basis

The component-level evidence and its limits are recorded in
[`adaptive_checkpoint_controller_validation_related_work.md`](../../../../research/adaptive_checkpoint_controller_validation_related_work.md).
Ledger provides close repository-level evidence that a deterministic execution
state can lower cost and improve resolution without another model call. Other
work supports compact repository representations, external validation,
failure-guided repair, adaptive context boundaries, and durable execution. The
literature still does not validate this exact decision-bound combination with
controller verification and conditional repair; that is the purpose of this
experiment.

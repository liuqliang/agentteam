# Phase 3 Bounded Worker Turn Experiment

Status: completed; fixed three-turn default rejected

## Decision

Test whether natural-phase worker checkpoints can bound one implementation
invocation without losing code state or increasing repeated repository reading
enough to erase the benefit. This is an engineering experiment, not production
authorization for automatic worker slicing.

The retained DVC full-mode implementation invocation is the historical long-turn
control:

- instance: `iterative__dvc_2.19.0_2.20.0`;
- source commit: `78dd045d29f274960bcaf48fd2d055366abaf2c1`;
- model: `gpt-5.6-sol`, reasoning profile `high`;
- implementation usage: `829955` total, `812666` input, `696320` cached input,
  `17289` output tokens;
- provider time: `655` seconds;
- completed commands: `36`;
- outcome: incomplete patch after the frozen taskpack omitted required
  `dvc/stage/*` write authority.

## Treatment

Run the same implementation objective in one persistent worktree using fresh,
compact model turns at natural boundaries:

1. `locate`: inspect the taskpack and repository, identify the complete call
   chain and scope risks, but do not modify source files;
2. `implement`: consume the compact locate checkpoint, inspect only named
   source locations, and produce the candidate patch;
3. `verify`: consume the implementation checkpoint, inspect the current diff,
   run focused verification, make bounded repairs when permitted, and publish
   the final worker result.

The controller, not the model, persists each checkpoint under the repository
Git directory. Git/worktree state remains the code authority. A checkpoint may
carry only bounded semantic state: completed actions, key findings with exact
paths and symbols, decisions and rationale, verification evidence, remaining
objective, and the next stage.

## Provider-Free Acceptance

1. Checkpoint validation rejects wrong task/attempt lineage, invalid stage
   transitions, absolute or escaping paths, stale Git state, and oversized
   semantic collections.
2. The controller atomically writes checkpoints outside the candidate diff and
   verifies their content digest on resume.
3. A replayed locate/implement/verify sequence preserves one worktree diff,
   does not duplicate completed stages after restart, and rejects stale
   checkpoints after an external worktree mutation.
4. Usage aggregation treats every turn as a separate provider invocation and
   reports total, cached, uncached, output, and per-stage usage without double
   counting.
5. Transcript profiling reports command count, repeated source-file reads,
   checkpoint bytes, changed files, and final patch digest.

## Live Boundary

After provider-free acceptance, launch at most one three-stage DVC treatment.
Do not rerun the historical long-turn control. Stop before the next stage when:

- settled cumulative usage reaches `600000` total tokens;
- a checkpoint is invalid or stale;
- the locate stage finds that frozen write authority is incomplete;
- provider usage is unavailable;
- the worktree or source revision differs from the bound DVC instance;
- a new semantic or architecture decision is required.

Because provider usage settles only at invocation end, this experiment measures
bounded overshoot per turn; it does not claim an exact in-turn token hard cap.

## Evaluation

Compare the treatment with the retained long-turn control on:

- total and uncached tokens;
- provider time and invocation count;
- completed commands and repeated file reads;
- checkpoint bytes;
- changed files and patch digest;
- verification reached;
- final candidate validity and benchmark result when a valid patch exists.

The treatment is promising only when it preserves or improves completion
quality while using no more than the historical `829955` implementation tokens
and no more than two repeated reads of any source file across stage boundaries.
One treatment cannot establish general effectiveness.

## Provider-Free Result

The experimental controller now persists semantic checkpoints under
`.git/agentteam-worker-turns/`, binds task and attempt lineage, hashes the full
tracked and untracked worktree state, and accounts every provider turn as a
separate invocation. A restart reuses completed turns without relaunching them;
an external worktree mutation makes the predecessor checkpoint stale and fails
closed.

Nine focused tests pass. The broader checkpoint, model-usage, Git recovery,
cost-profile, and experiment-harness selection passed `257` tests with `5`
skips. Python compilation and `git diff --check` pass.

A provider-free replay applied the retained DVC full-worker patch between the
locate and implement stages. All three stages completed in one worktree and
preserved the same nine changed paths. The three checkpoints were `725`, `980`,
and `949` bytes. The regenerated Git diff was `18` bytes larger than the
retained patch because of diff serialization; stable patch identity is assessed
with `git patch-id --stable`, not raw patch bytes.

## Invalid Live Startup And Repair Continuation

The first live startup settled one locate invocation with `53280` total tokens
(`50834` input, `30976` cached input, `2446` output) and no source changes. It
is an invalid infrastructure trial, not a task-quality result:

1. the initial experimental `8/12` hook route exhausted after only one
   host-recorded command, so the model correctly reported incomplete locating;
2. the ordinary Codex adapter returned complete token fields without the
   experiment-terminal `usage_status` wrapper, which the new aggregator
   initially rejected;
3. the model returned `remaining_objective` as a string list because the prompt
   did not state the field type.

The provider result and terminal authority were retained and recovered without
relaunching locate. The controller published a `3617`-byte checkpoint and
sealed the startup as `worker_turn_not_completed`.

A repair continuation is authorized under the same aggregate live budget. The
invalid startup's `53280` tokens count, leaving `546720` settled tokens for the
continuation. The continuation uses the existing L2 worker `28/40` route for
locate and implementation, accepts either complete adapter or terminal usage
shape without changing the numeric fields, explicitly binds checkpoint field
types, and supports recovery of a settled provider stage without reexecution.
No historical-control rerun is authorized.

## Final Outcome

The repair continuation completed locate and implementation, then stopped at
the settled budget gate before verify. The two valid turns used `766760` total
tokens and `717` provider seconds. Including the invalid startup, live cost was
`820040` tokens and `775` provider seconds, only `1.19%` below the historical
long implementation total while using `61.08%` more uncached input.

The locate turn read all 17 selected source paths. The implementation turn
reread 15 of them and completed 34 commands, producing 15 cross-turn repeated
reads and 52 commands across the valid treatment. The fixed split therefore
did not replace source grounding with the compact checkpoint.

The treatment still improved candidate completeness: 11 focused public tests
passed, the production patch was compatible with the hidden test patch, and
the official evaluator reported F2P `2/14`, P2P `64/66`, partial `0.393939`, and
`resolved=false`. This quality difference also includes the corrected Stage
write scope and cannot be attributed solely to checkpointing.

Decision `DEC-P3-bounded-worker-turn-experiment-v1` rejects fixed three-stage
model slicing as the default. Retain semantic checkpoints for recovery and
real boundaries, skip a model locate turn when deterministic grounding has no
gaps, run declared deterministic verification in the controller, and launch a
repair model only when verification requires semantic diagnosis.

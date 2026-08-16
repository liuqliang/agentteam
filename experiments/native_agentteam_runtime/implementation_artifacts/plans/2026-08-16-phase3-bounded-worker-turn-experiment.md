# Phase 3 Bounded Worker Turn Experiment

Status: provider-free acceptance complete; live treatment not yet launched

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

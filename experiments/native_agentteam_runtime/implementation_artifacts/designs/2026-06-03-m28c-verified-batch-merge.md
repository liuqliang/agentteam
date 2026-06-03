# M28c Verified Batch Merge Design

## Goal

Allow the runtime to merge a verified integration batch back into the source
branch without requiring manual git operations.

## Meaning Of Checkpoint

A checkpoint is an intermediate integration record. It captures that a task or
batch reached a known state, such as accepted, verified, committed on an
integration branch, or merged. It is useful for audit, rollback, and debugging,
but it is not always the final delivery merge.

Task-level checkpoints are useful for locating which task introduced a change.
Feature-level batch merge is the final system-level gate: a set of queued
patches is applied together, verified together, and merged together.

## Merge Policy

M28c adopts feature-level batch merge:

1. queued accepted patches are selected for a batch;
2. the batch worktree applies all selected patches;
3. verification must pass in the batch worktree;
4. the batch worktree is committed;
5. the source branch is fast-forwarded to the batch commit.

The merge uses `git merge --ff-only <batch_commit>` from `project_root`. This
keeps history linear and rejects unexpected divergence instead of creating an
implicit merge commit.

## Safety Gates

The merge is rejected unless:

- the batch registry says `batch_status == verified`;
- the source worktree is clean;
- the batch worktree can create a commit;
- the source branch can fast-forward to that batch commit.

Results are persisted back to `state/integration_batches.json`.

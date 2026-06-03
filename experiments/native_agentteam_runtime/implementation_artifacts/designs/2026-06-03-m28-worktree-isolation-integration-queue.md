# M28 Worktree Isolation And Integration Queue Design

## Goal

Make accepted writable attempts durable enough to integrate as a batch later,
without changing the current branch merge policy.

## Existing Baseline

M0 already creates one git worktree per writable attempt, captures a patch
artifact, can apply an accepted patch to an integration worktree, can run an
integration verification command, and can optionally commit the verified
integration worktree.

The missing piece is a durable queue view that records accepted patches even
when automatic integration is not enabled.

## Design

Add `state/integration_queue.json` under each run output directory. The event
log remains the source of truth; this JSON file is a materialized current view
for schedulers and later CLI inspection.

Queue schema:

```json
{
  "queue_schema_version": "integration_queue.v1",
  "items": []
}
```

Each item is keyed by `task_id:attempt_id` and records:

- accepted attempt identity;
- attempt worktree and branch;
- patch artifact path;
- integration branch and worktree when applied;
- verification and commit gate status;
- queue status.

Queue status is derived from existing gates:

- `pending`: accepted patch captured, not applied;
- `applied`: patch applied to an integration worktree;
- `verified`: integration verification passed but no integration commit was
  created;
- `blocked`: verification or integration commit failed;
- `committed`: verified integration commit created.

Add an `integration_queued` event when an accepted attempt has a patch artifact.
Replay can reconstruct a lightweight `integration_queue` snapshot from this
event plus existing `patch_integrated`, `integration_verified`, and
`integration_commit_evaluated` events.

## Non-Goals

M28 does not decide whether integration commits are task-level or feature-level
checkpoints. It also does not merge integration branches into the main branch.
Those remain later policy decisions.

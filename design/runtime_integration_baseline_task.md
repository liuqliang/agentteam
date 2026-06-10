# Runtime Integration Baseline Task

## Purpose

AgentTeam currently creates worker attempt worktrees from the target repository `HEAD`.
That is safe for isolated patches, but it means a later worker may not see a
previous worker's verified changes. This task introduces a run-level integration
baseline so each accepted result can become the base for later worker attempts.

## M1 Scope

Implement the smallest useful baseline flow for the two-phase worker-pool
runtime path used by normal multi-agent runs:

1. Each taskpack run has one integration baseline branch:

   ```text
   agentteam/run/<taskpack-id>/integration
   ```

2. Each taskpack run has one integration baseline worktree:

   ```text
   <work_root>/runs/<taskpack-id>/integration-baseline
   ```

3. Worker attempt worktrees are created from the current integration baseline
   branch, not always from the original project `HEAD`.

4. When a worker result is accepted and has a patch, the patch is applied to the
   integration baseline worktree.

5. If integration verification passes, the baseline worktree is committed. That
   commit becomes the base for later worker attempts.

6. If patch application or verification fails, the baseline is not committed and
   the failed result remains visible in run state and trace artifacts.

## Out Of Scope

The M1 task does not implement:

- legacy one-shot runtime baseline handling
- final merge into the user's current project branch
- a user-facing `agentteam integrate` or `agentteam merge` command
- a full wave planner
- multi-patch batch merge as the default path
- Feishu reverse-control for integration decisions
- cross-run reuse of integration baselines

## Acceptance Criteria

- A run creates or reuses `agentteam/run/<taskpack-id>/integration`.
- A run creates or reuses `<run_dir>/integration-baseline`.
- A worker attempt records the baseline ref it was created from.
- At least one test proves a later attempt is created from the updated baseline
  commit rather than the original project `HEAD`.
- Accepted patches with passing integration verification create a baseline
  commit.
- Failed integration verification does not advance the baseline commit.
- The target repository's current checkout is not modified by this flow.
- Trace artifacts include the baseline state through existing event and state
  snapshots.

## Implementation Notes

- Keep the existing per-attempt worktree isolation.
- Keep the existing task-level integration queue for visibility.
- Prefer focused helpers around baseline creation, patch application, and commit
  evaluation instead of expanding CLI code.
- Preserve current default behavior for final merge: no automatic merge into the
  user's current project branch.

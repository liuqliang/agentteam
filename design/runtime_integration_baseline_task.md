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

7. Operators can inspect run paths and baseline locations with:

   ```text
   agentteam paths --project-root <repo> --taskpack <taskpack-id>
   ```

8. Operators can explicitly fast-forward a completed run's baseline into the
   target repository with:

   ```text
   agentteam integrate --project-root <repo> --taskpack <taskpack-id>
   ```

   This requires a clean target repository and refuses non-fast-forward merges.

9. `agentteam status` and `agentteam report` include the integration baseline
   branch, worktree, and head summary when available.

## Out Of Scope

The M1 task does not implement:

- legacy one-shot runtime baseline handling
- automatic final merge into the user's current project branch
- non-fast-forward merge or conflict resolution for `agentteam integrate`
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
- `agentteam paths` reports run, artifact, and baseline locations.
- `agentteam integrate` fast-forwards the current project branch only when the
  repository is clean and the baseline is a descendant of `HEAD`.
- `agentteam status` and `agentteam report` expose baseline identity and head
  information.
- Trace artifacts include the baseline state through existing event and state
  snapshots.

## Implementation Notes

- Keep the existing per-attempt worktree isolation.
- Keep the existing task-level integration queue for visibility.
- Prefer focused helpers around baseline creation, patch application, and commit
  evaluation instead of expanding CLI code.
- Preserve current default behavior for final merge: no automatic merge into the
  user's current project branch.
- Keep final merge explicit through `agentteam integrate` until the runtime has
  stronger operator gates and conflict handling.

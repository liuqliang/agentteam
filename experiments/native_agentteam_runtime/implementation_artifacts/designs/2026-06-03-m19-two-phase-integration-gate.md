# M19 Two-Phase Integration Gate Design

Status: approved for implementation.

## Goal

M19 makes the two-phase path useful for real code changes by connecting
accepted worktree results to the existing integration gate: diff audit, patch
artifact, integration worktree apply, verification, and optional integration
commit.

## Scope

M19 supports:

- auditing declared `changed_files` against the actual git worktree diff;
- writing a patch artifact for matched worktree changes;
- applying accepted patches to an isolated integration worktree;
- running an optional verification command inside that integration worktree;
- committing the integration worktree only when verification passed;
- exposing integration fields through two-phase summary, replay, and SQLite
  state index.

M19 deliberately defers:

- merging integration commits back to the source branch;
- conflict resolution across multiple accepted patches;
- batching multiple task patches into one integration branch;
- integration worktree cleanup;
- live Codex quality evaluation beyond existing smoke/fake paths.

## Architecture

The blocking scheduler already has the implementation primitives:

```python
audit_worktree_diff(...)
write_patch_artifact(...)
apply_patch_to_integration_worktree(...)
run_integration_verification(...)
evaluate_integration_commit(...)
```

M19 imports and reuses those helpers in `two_phase_scheduler.py`.

`TwoPhaseFileScheduler` gains:

```python
TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root=repo,
    integrate_accepted_patch=True,
    integration_verification_command=[...],
    commit_verified_integration=True,
)
```

The integration gate runs only after a runtime result is accepted. Rejected,
retryable, timeout, and blocked attempts never apply patches.

## Collect Flow

For each collected result:

1. If `worktree_path` exists, run `audit_worktree_diff(...)`.
2. If the audit has actual changed files, write
   `<output-dir>/attempts/<attempt-id>/worktree.patch`.
3. Classify the attempt with `classify_attempt_outcome(..., diff_audit=...)`.
4. Include `diff_audit` and `patch_path` in `runtime_output_received`,
   `validation_accepted` or `validation_rejected`, and the step result.
5. If accepted and `integrate_accepted_patch` is true, apply the patch to
   `<output-dir>/integration/<task-id>`.
6. If a verification command is configured, run it in the integration worktree.
7. If `commit_verified_integration` is true, evaluate and optionally commit the
   integration worktree.
8. Append `backlog_updated` after integration gate events so replay can still
   show the task as done only after accepted collection finishes.

## Events

M19 reuses existing event types:

- `runtime_output_received` with `diff_audit` and `patch_path`;
- `validation_accepted` or `validation_rejected` with the same audit fields;
- `patch_integrated`;
- `integration_verified`;
- `integration_commit_evaluated`;
- `backlog_updated`.

No schema changes are required.

## CLI

The existing CLI flags become effective for the two-phase worker-pool path:

```text
--integrate-accepted-patch
--integration-verification-command-json '["python3", "-c", "..."]'
--commit-verified-integration
```

`--project-root` is required for real worktree creation and patch integration.
If integration is requested without `--project-root`, the scheduler records the
accepted task as done but leaves integration fields as `not_requested`.

## Acceptance

M19 is accepted when:

- two-phase accepted worktree changes produce a patch artifact;
- the patch applies to an integration worktree without mutating the source
  repository HEAD;
- verification can pass or fail without changing validation acceptance;
- commit is created only after verification passed;
- existing retry, timeout, worker-pool, blocking scheduler, replay, and SQLite
  tests still pass.

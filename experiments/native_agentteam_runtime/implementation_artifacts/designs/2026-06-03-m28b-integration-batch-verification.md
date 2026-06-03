# M28b Integration Batch Verification Design

## Goal

Verify a set of accepted integration queue patches together before any future
main-branch merge policy is considered.

## Design

Add `state/integration_batches.json` as a materialized registry of batch
verification attempts. The integration queue remains the source of selected
patches; event logs and queue entries remain the source of per-attempt evidence.

`verify_integration_batch(project_root, output_dir, batch_id, command)` will:

1. read `state/integration_queue.json`;
2. select non-blocked queue items by status;
3. create a fresh batch worktree at
   `integration_batches/<batch_id>/worktree`;
4. apply each selected patch in queue order;
5. run the verification command in the batch worktree;
6. persist the batch result in `state/integration_batches.json`.

Batch statuses:

- `empty`: no queue items matched the selected statuses;
- `blocked`: a queued patch could not be applied to the batch worktree;
- `failed`: patches applied, but verification failed;
- `verified`: patches applied and verification passed.

## Policy Boundary

M28b does not create commits and does not merge into the source branch. It only
answers whether a set of queued patches can coexist and pass a command in one
integration worktree.

The task-level versus feature-level commit decision remains open. This API is
compatible with both policies because it verifies patch sets before commit or
merge decisions are made.

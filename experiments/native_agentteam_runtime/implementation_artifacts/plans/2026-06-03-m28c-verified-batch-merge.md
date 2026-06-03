# M28c Verified Batch Merge Plan

## Objective

Add an explicit verified-batch merge gate that can fast-forward the source
branch after a batch passes verification.

## Steps

- [x] **Step 1: Red test**
  - Add a failing test for `merge_verified_batch=True`.
  - Confirm existing API rejects the new argument.

- [x] **Step 2: Merge implementation**
  - Add `merge_verified_integration_batch`.
  - Commit the verified batch worktree.
  - Fast-forward merge the batch commit into `project_root`.
  - Persist merge status in `state/integration_batches.json`.

- [x] **Step 3: Export API**
  - Export `merge_verified_integration_batch`.
  - Keep `verify_integration_batch(..., merge_verified_batch=True)` as the
    automatic gate.

- [x] **Step 4: Documentation**
  - Explain checkpoint semantics.
  - Document the verified batch merge policy.
  - Update the roadmap.

- [x] **Step 5: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 6: Commit and push**
  - Commit M28c changes.
  - Push `native-runtime-m0`.

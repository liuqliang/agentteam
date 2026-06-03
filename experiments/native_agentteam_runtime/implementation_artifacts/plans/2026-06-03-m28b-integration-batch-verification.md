# M28b Integration Batch Verification Plan

## Objective

Add a reusable batch verification API over the durable integration queue.

## Steps

- [x] **Step 1: Add batch registry helpers**
  - Add an integration batch module.
  - Support registry read and result upsert.

- [x] **Step 2: Build batch worktree verifier**
  - Read selected queue items.
  - Create a batch worktree.
  - Apply selected patch artifacts in queue order.
  - Run a caller-provided verification command.

- [x] **Step 3: Export API**
  - Export `verify_integration_batch`.
  - Export `read_integration_batches`.

- [x] **Step 4: Tests**
  - Cover two queued patches verified together in one batch worktree.
  - Cover persisted batch registry state.

- [x] **Step 5: Documentation**
  - Document batch statuses and policy boundary.
  - Update roadmap next step.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M28b changes.
  - Push `native-runtime-m0`.

# M29a Worker Restart Budget And Quarantine Plan

## Objective

Add a restart budget and quarantine state to the resident worker pool.

## Steps

- [x] **Step 1: Red test**
  - Add a failing worker-pool test for `max_restart_count=1`.
  - Confirm the constructor rejects the new argument before implementation.

- [x] **Step 2: Implement quarantine**
  - Track `max_restart_count`.
  - Mark workers as `quarantined` after the restart budget is exhausted.
  - Preserve quarantine state in health checks and registry writes.

- [x] **Step 3: CLI exposure**
  - Add `--worker-max-restart-count`.
  - Pass it to worker-pool supervisors.
  - Include the configured value in pool summaries.

- [x] **Step 4: Tests**
  - Verify quarantine after budget exhaustion.
  - Verify existing restart behavior remains unchanged.
  - Verify CLI wiring.

- [x] **Step 5: Documentation**
  - Document restart budget and quarantine semantics.
  - Update roadmap.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M29a changes.
  - Push `native-runtime-m0`.

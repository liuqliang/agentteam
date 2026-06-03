# M29c Reassignment Event Lineage Plan

## Objective

Make conservative health-driven reassignment explicit in the event log and
replay snapshot.

## Steps

- [x] **Step 1: Red test**
  - Add a failing test for `task_reassigned`.
  - Confirm the scheduler currently skips the unavailable agent without writing
    explicit reassignment lineage.

- [x] **Step 2: Scheduler event**
  - Detect unavailable same-role agents before dispatch.
  - Emit `task_reassigned` for the selected replacement agent.

- [x] **Step 3: Event schema**
  - Add `task_reassigned` to the event schema enum.

- [x] **Step 4: Replay support**
  - Store reassignment lineage on the replayed attempt snapshot.

- [x] **Step 5: Documentation**
  - Document event payload and conservative policy.
  - Update roadmap status.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M29c changes.
  - Push `native-runtime-m0`.

# M30c Roadmap And Decomposition Visibility Plan

## Objective

Expose current milestone and next decomposition task in runtime observability
without adding a mutation path.

## Steps

- [x] **Step 1: Red test**
  - Add a failing test that starts a two-phase scheduler with auto decomposition.
  - Confirm observability lacks `current_milestone` and `next_decomposition`.

- [x] **Step 2: State reader**
  - Read `state/two_phase_scheduler_state.json` when present.
  - Return empty context for older runs without that state file.

- [x] **Step 3: Milestone visibility**
  - Select the active milestone.
  - Resolve its `current_decomposition_task_id` into a compact task summary.

- [x] **Step 4: Documentation**
  - Document read-only policy and non-goals.
  - Update roadmap status.

- [x] **Step 5: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 6: Commit and push**
  - Commit M30c changes.
  - Push `native-runtime-m0`.

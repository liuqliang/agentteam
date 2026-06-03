# M29b Quarantined Agent Dispatch Avoidance Plan

## Objective

Route new two-phase work away from quarantined worker agents when another
compatible agent is available.

## Steps

- [x] **Step 1: Red test**
  - Add a failing test for `unavailable_agent_ids`.
  - Confirm the scheduler constructor rejects the argument before
    implementation.

- [x] **Step 2: Scheduler support**
  - Add `unavailable_agent_ids`.
  - Mark unavailable agents before dispatch.
  - Add `set_unavailable_agent_ids`.

- [x] **Step 3: CLI bridge**
  - Derive quarantined agent ids from worker-pool health.
  - Update the scheduler before each supervised tick.

- [x] **Step 4: Tests**
  - Verify scheduler skips an unavailable agent.
  - Verify the supervised worker-pool CLI path still runs.

- [x] **Step 5: Documentation**
  - Document conservative reassignment.
  - Update roadmap.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M29b changes.
  - Push `native-runtime-m0`.

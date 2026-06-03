# M28 Worktree Isolation And Integration Queue Plan

## Objective

Persist accepted patch integration state in a durable queue while preserving the
existing apply, verify, and commit gates.

## Steps

- [x] **Step 1: Add queue helpers**
  - Add a small integration queue module.
  - Support read, status derivation, and upsert by `task_id:attempt_id`.

- [x] **Step 2: Wire single-run integration**
  - Queue accepted patch artifacts from `run_simulation`.
  - Emit `integration_queued`.
  - Return queue metadata in result summaries.

- [x] **Step 3: Wire two-phase integration**
  - Queue accepted patch artifacts from `TwoPhaseFileScheduler`.
  - Keep queued state updated after apply, verify, and commit gates.

- [x] **Step 4: Replay visibility**
  - Add `integration_queued` to the event schema.
  - Reconstruct a lightweight `integration_queue` snapshot in replay.

- [x] **Step 5: Tests**
  - Cover pending queue state when automatic integration is disabled.
  - Cover committed queue state through the two-phase scheduler.

- [x] **Step 6: Documentation**
  - Document the queue file, state transitions, and policy boundary.
  - Advance the roadmap to the next milestone if verified.

- [x] **Step 7: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 8: Commit and push**
  - Commit M28 changes.
  - Push `native-runtime-m0`.

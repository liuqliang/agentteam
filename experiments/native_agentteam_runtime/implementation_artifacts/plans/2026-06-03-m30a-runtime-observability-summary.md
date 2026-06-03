# M30a Runtime Observability Summary Plan

## Objective

Add a read-only CLI summary that makes long-running runtime output inspectable
without opening raw event JSONL.

## Steps

- [x] **Step 1: Red test**
  - Add a failing CLI test for `--show-runtime-observability`.
  - Confirm the CLI rejects the unknown argument before implementation.

- [x] **Step 2: Observability module**
  - Add `build_runtime_observability(output_dir)`.
  - Aggregate replay, state index, integration queue, worker registry, and
    recent failures.

- [x] **Step 3: CLI flag**
  - Add `--show-runtime-observability`.
  - Keep it mutually exclusive with `--show-state-index`.
  - Do not require agent pool or backlog for read-only show commands.

- [x] **Step 4: Public export**
  - Export `build_runtime_observability` from `agentteam_runtime`.

- [x] **Step 5: Documentation**
  - Document the CLI-only policy.
  - Update roadmap status and next M30 slice.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M30a changes.
  - Push `native-runtime-m0`.

# M30b Runtime Observability Drilldown Plan

## Objective

Add read-only CLI drilldown views for runtime resources already available in
events, state index, worker registry, and integration queue files.

## Steps

- [x] **Step 1: Red tests**
  - Add a failing API test for `build_runtime_observability(..., view=...)`.
  - Add a failing CLI test for `--observability-view events`.

- [x] **Step 2: API views**
  - Add supported views to `build_runtime_observability`.
  - Return common metadata for every view.
  - Return resource-specific payloads for backlog, leases, events, sessions,
    workers, and integration queue.

- [x] **Step 3: CLI flag**
  - Add `--observability-view`.
  - Require it to be used with `--show-runtime-observability`.

- [x] **Step 4: Documentation**
  - Document supported views and CLI-only policy.
  - Update roadmap remaining work.

- [x] **Step 5: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 6: Commit and push**
  - Commit M30b changes.
  - Push `native-runtime-m0`.

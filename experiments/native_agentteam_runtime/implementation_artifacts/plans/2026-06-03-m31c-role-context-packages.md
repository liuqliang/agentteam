# M31c Role Context Packages Plan

## Objective

Generate bounded role context files at dispatch time and pass their paths to
Codex workers.

## Steps

- [x] **Step 1: Red dispatch tests**
  - Add a single-step scheduler test that expects `role_context_path`.
  - Add a two-phase scheduler test that expects the same behavior.

- [x] **Step 2: Red prompt test**
  - Add a Codex prompt test that requires an explicit `Role context package:`
    section.

- [x] **Step 3: Context package writer**
  - Resolve `agent_pool.role_context_packages[agent.role]`.
  - Write a bounded `role_context.v1` JSON file under `role_contexts/`.
  - Reuse artifact summaries instead of embedding full files.

- [x] **Step 4: Dispatch and prompt wiring**
  - Attach `role_context_path` and schema version to mailbox payloads.
  - Render a dedicated prompt section that points to the context file.

- [x] **Step 5: Schema and docs**
  - Extend `agent_pool.schema.json` with `role_context_packages`.
  - Update runtime behavior docs and roadmap.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M31c changes.
  - Push `native-runtime-m0`.

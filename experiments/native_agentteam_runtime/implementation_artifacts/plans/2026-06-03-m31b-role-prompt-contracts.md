# M31b Role Prompt Contracts Plan

## Objective

Add role prompt contracts to dispatch payloads and Codex prompts.

## Steps

- [x] **Step 1: Red dispatch tests**
  - Add a single-step scheduler test that expects a role contract in the mailbox
    payload.
  - Add a two-phase scheduler test that expects the same payload shape.

- [x] **Step 2: Red prompt test**
  - Add a Codex prompt test that requires an explicit `Role prompt contract:`
    section.

- [x] **Step 3: Dispatch payloads**
  - Add shared role prompt field resolution.
  - Attach `agent_role`, `required_role`, and `role_prompt_contract` during
    single-step and two-phase dispatch.

- [x] **Step 4: Codex prompt rendering**
  - Render role contracts before the fixed result schema.
  - Keep scope and result schema rules unchanged.

- [x] **Step 5: Schema and docs**
  - Extend `agent_pool.schema.json` with `role_prompt_contracts`.
  - Update runtime behavior docs and roadmap.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M31b changes.
  - Push `native-runtime-m0`.

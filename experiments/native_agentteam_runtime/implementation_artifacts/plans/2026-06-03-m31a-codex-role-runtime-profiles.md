# M31a Codex Role Runtime Profiles Plan

## Objective

Add role-level Codex runtime profile routing for scheduler core and resident
worker pools.

## Steps

- [x] **Step 1: Red scheduler test**
  - Add a scheduler-core test where the selected agent has no
    `runtime_profile`.
  - Define `agent_pool.role_runtime_profiles.repo_map_agent`.
  - Confirm the runtime session and fake Codex command use the role profile.

- [x] **Step 2: Scheduler resolver**
  - Pass the loaded agent pool into runtime adapter resolution.
  - Resolve `role_runtime_profiles[agent.role]` after agent-level profile and
    before caller defaults.

- [x] **Step 3: Red worker-pool test**
  - Add a resident worker-pool test where the worker has no agent-level profile.
  - Confirm the worker starts as a Codex worker from its role profile.

- [x] **Step 4: Worker-pool resolver**
  - Load role profiles from the agent pool.
  - Use the same profile precedence for resident worker process startup.
  - Preserve CLI/default Codex command merging.

- [x] **Step 5: Schema and docs**
  - Extend `agent_pool.schema.json` with `role_runtime_profiles`.
  - Keep `agent_state.schema.json` aligned with runtime-supported
    `fallback_worktree_path`.
  - Update M0 runtime behavior docs and the roadmap.

- [x] **Step 6: Verification**
  - Run focused tests.
  - Run full unit tests.
  - Run artifact lint, compileall, diff check, and placeholder scan.

- [x] **Step 7: Commit and push**
  - Commit M31a changes.
  - Push `native-runtime-m0`.

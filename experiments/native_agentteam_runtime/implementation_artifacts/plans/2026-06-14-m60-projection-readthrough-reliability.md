# M60 Projection Read-Through Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AgentTeam operator commands use the rebuildable projection DB consistently when it is fresh, fall back to authoritative files when it is missing/stale/corrupt, and explain that fallback clearly.

**Architecture:** Keep file-backed artifacts authoritative. Treat `<work_root>/agentteam.db` as a read-through acceleration and index layer only: commands may read it when `check_project_projection_db()` passes, but must fall back to file replay/scan when it does not. M60 must not introduce artifact deletion or make the DB the source of truth.

**Tech Stack:** Python standard library, SQLite projection helpers in `projection_db.py`, existing AgentTeam CLI, JSON/text command output, `unittest`.

---

### Task 1: Projection Read-Through Contract

**Files:**
- Modify if needed: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing tests for projection freshness metadata**
  Add tests that create a work root with one completed run, rebuild the projection DB, and assert the projection check exposes enough status for callers to distinguish:
  - fresh DB;
  - missing DB;
  - stale DB after an event/report/taskpack file changes;
  - corrupt/unreadable DB.

- [ ] **Step 2: Write failing tests for read-through fallback semantics**
  Add tests for at least two existing projection readers, such as `read_projected_taskpacks()` and `read_projected_run_events()`, proving that:
  - a fresh DB returns `projection_source == "db"`;
  - a stale/missing/corrupt DB does not return stale DB rows;
  - callers can still obtain file-backed state through existing command fallback paths.

- [ ] **Step 3: Implement the smallest shared contract**
  Prefer a small helper or normalized status fields over a broad refactor. The contract should let command code report:
  - `projection_source`;
  - optional `projection_warning`;
  - `projection_db_path`;
  - optional operator hint such as `agentteam db rebuild`.

- [ ] **Step 4: Keep DB non-authoritative**
  Do not remove file replay, raw JSONL reads, taskpack scans, reports, or existing artifact files. Do not add deletion behavior.

### Task 2: Command Output Consistency

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing tests for JSON output**
  Add or extend tests for `agentteam status --json`, `agentteam logs --json`, `agentteam report --json`, `agentteam taskpack list --json`, and `agentteam gc --artifacts --json` where applicable. Each command should show `projection_source`; when falling back it should include `projection_warning` and a clear rebuild suggestion.

- [ ] **Step 2: Write failing tests for compact text output**
  Add text-output assertions for the same high-value commands where text output is operator-facing. Text should not dump raw DB diagnostics; it should say the projection is using DB or file fallback and, on fallback, suggest `agentteam db rebuild`.

- [ ] **Step 3: Implement output normalization**
  Reuse existing summary fields and writer helpers where possible. Avoid large command rewrites. The goal is consistent operator visibility, not a new command surface.

- [ ] **Step 4: Preserve existing successful output**
  Existing tests for command summaries, token usage, worker status, review gates, and Chinese operator briefs must continue to pass.

### Task 3: Consistency And Staleness Guards

**Files:**
- Modify if needed: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify if needed: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing regression tests for stale data avoidance**
  Create a fresh projection DB, mutate authoritative files, then run read-through commands. The command must not silently return stale DB data. It should either rebuild only when the command explicitly asked for rebuild, or fall back to file state with a warning.

- [ ] **Step 2: Write failing regression tests for corrupt DB fallback**
  Replace `agentteam.db` with invalid bytes, then run representative read-through commands. They should not crash; they should fall back to files and report the projection warning.

- [ ] **Step 3: Implement robust exception handling**
  If SQLite open/query/check fails, convert it to the same projection warning path used for stale/missing DB. Do not hide unrelated file replay failures.

- [ ] **Step 4: Verify command parity**
  For a simple completed run, fresh-DB output and file-fallback output should identify the same latest run/taskpack/events even if they differ in `projection_source`.

### Task 4: Documentation And Roadmap

**Files:**
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Test if docs examples are covered by existing command tests: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Document read-through behavior**
  Update command reference to state that `agentteam.db` is a rebuildable projection. Explain the normal path:
  - run commands normally;
  - inspect `projection_source`;
  - run `agentteam db rebuild` when a command reports stale/missing/corrupt projection warning;
  - trust file artifacts as the authority.

- [ ] **Step 2: Document what M60 does not do**
  Explicitly state that M60 does not delete artifacts, does not make DB authoritative, and does not require operators to maintain DB manually for correctness.

- [ ] **Step 3: Update roadmap**
  Add an M60 section after M56-M59 with implemented behavior, validation approach, and the remaining route toward DB-primary only if future evidence shows file replay is a bottleneck.

### Final Verification

- [ ] Run `git diff --check`.
- [ ] Run `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime`.
- [ ] Produce a concise Chinese operator summary covering changed commands, projection fallback behavior, verification results, and any follow-up work.

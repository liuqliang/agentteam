# M56-M59 Pursue Control Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the operator-facing control loop around `agentteam pursue`: visible pursue state, review-gate handling, long-goal memory, and stronger taskpack decomposition quality.

**Architecture:** Keep `pursue` as a thin loop over ordinary taskpacks/runs. Add structured metadata and summaries that existing `status`, `report`, `watch`, and follow-up flows can read without introducing a second scheduler. Do not grant workers merge, push, release activation, or source-branch authority.

**Tech Stack:** Python standard library, existing AgentTeam CLI/runtime modules, JSON artifact files, unittest.

---

### Task 1: M56 Pursue State And Recap

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify if needed: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing tests for pursue recap artifacts**
  Add tests that run `agentteam pursue --max-rounds 1 --json` with fake author/runtime and assert a machine-readable pursue recap artifact exists under the run/work root. The recap must include `pursue_id`, `rounds_completed`, `max_rounds`, `stop_reason`, `latest_taskpack_id`, `latest_report_path`, and a compact operator next action.

- [ ] **Step 2: Write failing tests for status/report visibility**
  Add tests that `agentteam status` or the report-building path can expose pursue context when the latest run belongs to a pursue loop. The operator-facing text should say that the run stopped because of `review_gate_required`, `blocked`, `manual_gate_required`, `permission_request_required`, `failed`, or `max_rounds_reached`.

- [ ] **Step 3: Implement pursue recap writing**
  Extend `_run_pursue_loop` to write a small authoritative JSON recap artifact in the work root, and attach the recap path to the JSON/text result. Keep the existing per-run report as the source of task evidence; the pursue recap is only loop-level state.

- [ ] **Step 4: Implement compact visibility**
  Make status/report text show the latest pursue recap when present. Keep output short: current loop id, completed rounds, stop reason, latest taskpack, latest report, and next recommended command.

### Task 2: M57 Review Gate Workflow

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify if needed: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Docs: `docs/agentteam-command-reference.md`

- [ ] **Step 1: Write failing tests for review-gate explanation**
  Add tests that a completed run with `follow_up_recommendation.action == "integrate"` renders a concise review-gate section with the integration branch/baseline, diff/report commands, and integrate command.

- [ ] **Step 2: Write failing tests for review commands**
  Add or extend a read-only command path so an operator can inspect what is waiting at the review gate without merging. It must not mutate the target branch.

- [ ] **Step 3: Implement review-gate operator flow**
  Reuse existing `report`, `paths`, and `integrate` primitives. Prefer adding compact text fields and command suggestions over inventing a broad new workflow. Review output should clearly distinguish integration worktree/branch from the original project branch.

- [ ] **Step 4: Document the flow**
  Update the command reference with the exact review sequence after `review_gate_required`: inspect report, inspect paths/diff, integrate only when accepted, then optionally run `next` or `pursue`.

### Task 3: M58 Long-Goal Memory And Queue

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Create or modify if needed: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/goal_memory.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing tests for a goal memory artifact**
  Add tests that `agentteam pursue` writes a compact project/work-root goal memory file that records the original long goal, completed rounds, current hypothesis/next step, blocked reasons, and latest run ids.

- [ ] **Step 2: Write failing tests for follow-up prompt context**
  Add tests that a later pursue round includes the previous goal memory summary in the follow-up goal context, instead of relying only on one previous report.

- [ ] **Step 3: Implement the memory file**
  Store goal memory as small JSON under the work root. It must be rebuildable from reports where possible and must not replace per-run reports, events, taskpacks, or evidence artifacts.

- [ ] **Step 4: Keep memory bounded**
  Limit stored prose and round history so long-running loops do not grow unbounded context. Preserve paths and ids rather than copying full logs.

### Task 4: M59 Taskpack Decomposition Quality

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Docs: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [ ] **Step 1: Write failing validation tests**
  Add tests for optimization and long-running implementation goals that reject overly generic taskpacks. For optimization tasks, require baseline/profile/candidate/measurement language. For long-running pursue follow-ups, require a next-step objective tied to previous evidence.

- [ ] **Step 2: Write failing author-prompt tests**
  Add tests that the Codex taskpack author prompt instructs authors to preserve the operator goal, decompose large goals into measurable sub-tasks, and avoid safe-but-trivial documentation-only changes unless the operator asked for documentation.

- [ ] **Step 3: Implement validation and prompt hardening**
  Extend existing narrow validation gates rather than adding a separate policy engine. Keep the gate conservative enough to reject obviously generic tasks without blocking focused documentation tasks.

- [ ] **Step 4: Update roadmap**
  Add implemented M56-M59 entries or a compact M56-M59 section to `native_runtime_roadmap.md`, mapping each feature to shipped behavior and tests.

### Final Verification

- [ ] Run `git diff --check`.
- [ ] Run `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime`.
- [ ] Produce a concise Chinese operator summary describing what changed, what was verified, and which integration/review step remains.

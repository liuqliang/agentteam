# Optimization Taskpack Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent broad optimization goals from being authored or accepted as document-only audit taskpacks.

**Architecture:** Add a small goal-classification layer to taskpack validation and authoring. Optimization goals get a concrete `goal_kind`, code-facing deliverables, and validator checks that require at least one implementation-facing backlog item unless the taskpack is explicitly an audit-only goal.

**Tech Stack:** Python standard library, existing `unittest` runtime tests, JSON taskpack artifacts.

---

### Task 1: Classify optimization goals

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Write a failing test that drafts a fake taskpack for a Chinese optimization goal and asserts `taskpack.goal_kind == "optimization"`, the backlog task has `work_type == "code_implementation"`, and required deliverables include metric/baseline oriented fields.
- [x] Implement `classify_goal_kind(goal)` and use it in `draft_taskpack_files`.
- [x] Run the targeted test and confirm it passes.

### Task 2: Reject optimization taskpacks that only audit or document

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Write a failing validator test for an optimization taskpack whose only ready backlog item has `work_type == "audit"` and write scope limited to documentation.
- [x] Add semantic validation that optimization goals require at least one ready `code_implementation` or `code_investigation` item with non-document write scope.
- [x] Run the targeted validator test and confirm it passes.

### Task 3: Strengthen Codex author instructions and canonicalization

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Write a failing test that canonicalization adds missing `goal_kind` and `work_type` to Codex-authored optimization taskpacks.
- [x] Update `_author_prompt` to describe optimization workflow requirements and forbidden document-only fallback.
- [x] Update `_canonicalize_codex_taskpack_files` to fill missing `goal_kind`, `work_type`, and optimization deliverables from the original goal.
- [x] Run targeted author tests.

### Task 4: Documentation and full verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/README.md`
- Modify: `README.md`

- [x] Document that optimization goals are treated as code-facing taskpacks and must report baseline, candidate matrix, implementation result, verification, and metric delta or no-safe-change evidence.
- [x] Run `python3 -m unittest discover experiments/native_agentteam_runtime/m0_runtime/tests`.
- [x] Run `git diff --check`.
- [ ] Commit and push.

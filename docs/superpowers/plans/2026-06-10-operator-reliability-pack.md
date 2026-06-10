# Operator Reliability Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the P0 operator reliability features needed after the recent verisilicon run.

**Architecture:** Keep the scheduler unchanged. Add authoring lifecycle metadata in `taskpack_author.py`, expose it through the high-level CLI in `agentteam.py`, normalize run directory handling in the wrapper, and add an explicit taskpack creation command using existing `draft_taskpack_files`.

**Tech Stack:** Python standard library, existing AgentTeam runtime modules, `unittest`.

---

### Task 1: Authoring State

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add `author_state.json` writes for Codex authoring start, timeout, failure, and success.
- [x] Add a progress callback hook so `start` and `next` can print authoring progress.
- [x] Add project status support for active authoring entries.
- [x] Add `stop --authoring` to terminate the latest live author PID.

### Task 2: Run Path and Compact Output

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Normalize `agentteam run --run-root <...>/<taskpack-id>` to use the parent as runs root.
- [x] Resolve existing nested run directories for `status`, `paths`, `report`, and `chat`.
- [x] Make low-level `agentteam run` print the same concise completion format as `start` and `continue`.
- [x] Keep full JSON available through `agentteam run --json`.

### Task 3: Explicit Taskpack Creation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `README.md`
- Modify: `experiments/native_agentteam_runtime/README.md`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add `agentteam taskpack new`.
- [x] Read project profile by default and use `work_root/drafts` and `work_root/frozen`.
- [x] Accept repeated `--read-scope` and `--write-scope`.
- [x] Accept `--verification-command-json`.
- [x] Optionally freeze with `--freeze`.

### Task 4: Verification

**Files:**
- Test only.

- [x] Run focused taskpack tests.
- [x] Run full M0 unit tests.
- [x] Run `git diff --check`.
- [x] Commit and push the branch.

# Start Progress And Status CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show operator-visible progress during `agentteam start` and provide `agentteam status` for the latest run.

**Architecture:** Keep stdout as machine-readable final JSON. Write progress lines to stderr from the high-level `agentteam` wrapper. Implement `status` as a read-only summary over project profile, latest run directory, and existing scheduler state JSON.

**Tech Stack:** Python stdlib `argparse`, `json`, `pathlib`, existing `unittest` subprocess CLI tests.

---

### Task 1: Start Progress

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`

- [x] Write a failing subprocess test that runs `agentteam start` with a fake profile and asserts stderr contains progress lines while stdout remains JSON.
- [x] Implement a small progress writer in `agentteam.py`.
- [x] Emit progress before/after draft, validation, freeze, and run.
- [x] Run the focused test and verify it passes.

### Task 2: Status Command

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`

- [x] Write a failing subprocess test that runs a fake taskpack, then calls `agentteam status --project-root <repo>` and asserts it summarizes the latest run.
- [x] Add `status` parser and handler.
- [x] Resolve latest run from the project profile's `work_root`.
- [x] Read `state/two_phase_scheduler_state.json` and summarize scheduler status, tasks, integrations, manual gates, and last failure.
- [x] Run focused and full tests.

### Verification

- `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest discover experiments/native_agentteam_runtime/m0_runtime/tests`
- `python3 -m compileall experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime`
- `git diff --check`
- `agentteam status --project-root /home/liuql/projects/verisilicon`

# M62 Follow-Up Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Give operators a compact queue view for the next bounded task after a completed run, using existing reports and goal memory without starting workers or bypassing review gates.

**Architecture:** Add a small `follow_up_queue.py` helper that builds queue items from `goal_memory.follow_up_queue`, completion summary `next_steps`, and follow-up recommendations. Add an `agentteam queue` command group with read-only `show` and `next` subcommands. Keep `agentteam next` as the only command that actually authors/runs follow-up work.

**Tech Stack:** Python standard library, AgentTeam CLI parser, `unittest`.

---

### Task 1: Queue Helper Contract

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/follow_up_queue.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing helper test**

Add a test that imports `build_follow_up_queue_summary` and passes a source report with two `next_steps` plus goal memory with one queued item. Assert the summary includes deduplicated queue items, `next_goal`, and an `agentteam next --from-taskpack ... --goal ...` command.

- [x] **Step 2: Run focused test and verify failure**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_follow_up_queue_summary_merges_report_and_goal_memory
```

Expected: import failure because `follow_up_queue.py` does not exist.

- [x] **Step 3: Implement helper**

Implement `build_follow_up_queue_summary(...)` with bounded text, dedupe by objective/source taskpack id, and `render_follow_up_queue_text(...)`.

- [x] **Step 4: Run focused test and verify pass**

Run the same focused command. Expected: pass.

### Task 2: CLI Queue Commands

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing CLI tests**

Add tests for:

- `agentteam queue show --project-root ... --taskpack first-pass --json`
- `agentteam queue next --project-root ... --taskpack first-pass`

Assert JSON output includes queue items and text output includes `next_goal` plus the suggested `agentteam next` command.

- [x] **Step 2: Run focused tests and verify failure**

Expected: parser reports unknown command `queue`.

- [x] **Step 3: Add parser and handlers**

Add `_add_queue_parser`, `_handle_queue`, `_queue_source_run_dir`, `_latest_goal_memory_for_run`, and `_write_queue_text`. The command must not mutate files or start workers.

- [x] **Step 4: Run focused tests and verify pass**

Run the focused queue tests. Expected: pass.

### Task 3: Documentation And Roadmap

**Files:**
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Document `agentteam queue`**

Explain that `queue show` and `queue next` are read-only queue inspection commands and that `agentteam next` remains the command that actually creates and runs follow-up work.

- [x] **Step 2: Mark M62 implemented**

Add M62 after M61 and set the next route toward safe artifact retention deletion or semantic feedback proposals.

### Task 4: Full Verification

**Files:**
- No source edits.

- [x] **Step 1: Run diff whitespace check**

```bash
git diff --check
```

- [x] **Step 2: Run native runtime unit tests**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
```

- [x] **Step 3: Commit and push**

```bash
git add docs/agentteam-command-reference.md \
  experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-14-m62-follow-up-queue.md \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/follow_up_queue.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "Add follow-up queue inspection"
git push
```

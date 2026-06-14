# M66 Pursue Queue Consumption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `agentteam pursue` consume the same bounded follow-up queue that
operators can inspect with `agentteam queue`, so long-running goals advance from
completed reports, goal memory, and queue suggestions instead of relying on an
implicit first `next_steps` fallback.

**Architecture:** Keep `pursue` as a thin loop over ordinary taskpack
submission and run execution. After each completed round, build a
`follow_up_queue.v1` summary from the latest report plus goal memory, persist a
compact queue selection in the pursue round/recap, and use the selected
`next_goal` as the raw goal for the next round. Review gates, blocked runs,
manual gates, permission requests, and failed runs still stop the loop unless
the operator explicitly allows review-gate follow-up.

**Non-goals:** Do not add a second scheduler, do not mutate completed reports or
queue artifacts, do not auto-integrate review gates, do not push or release
runtime code, and do not implement M68 multi-model adapters.

---

### Task 1: Queue Selection Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing helper tests**
  Add tests proving pursue next-goal selection uses `build_follow_up_queue_summary`
  with both report and goal-memory inputs, records `queue_status`, `next_goal`,
  `next_command`, and stops conservatively with `follow_up_queue_empty` instead
  of inventing ungrounded follow-up work when the queue is empty.

- [x] **Step 2: Implement queue selection helper**
  Add a small helper in `agentteam.py` that returns a compact queue summary for
  a source run and goal memory without starting workers or mutating artifacts.

### Task 2: Pursue Loop Integration

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing CLI test**
  Extend fake-runtime pursue coverage so a two-round pursue run records the
  first round's follow-up queue selection and drafts the second taskpack from
  that selected `next_goal`.

- [x] **Step 2: Wire pursue to queue consumption**
  After each successful non-stopping round, build the queue summary, attach a
  compact record to the round, and use the selected goal as the next raw
  follow-up goal.

### Task 3: Operator Visibility

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Write failing recap/status assertions**
  Assert pursue recap/result surfaces the latest queue status and next command
  when the loop stops at `max_rounds_reached`.

- [x] **Step 2: Implement compact visibility**
  Keep text output short. For max-round stops, recommend
  `agentteam queue next --taskpack <latest>` so the operator can continue from
  the same queue rather than reading long reports.

- [x] **Step 3: Document and update roadmap**
  Document that `pursue` consumes the same queue inspected by `agentteam queue`,
  and mark M66 implemented in the roadmap after verification.

### Final Verification

- [x] Run `git diff --check`.
- [x] Run focused pursue/queue tests.
- [x] Run `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime`.
- [ ] Commit, push, and send a concise Chinese Feishu completion message.

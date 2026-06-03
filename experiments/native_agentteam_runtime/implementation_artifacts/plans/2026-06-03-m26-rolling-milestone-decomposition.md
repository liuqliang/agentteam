# M26 Rolling Milestone Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded rolling decomposition waves for one configured milestone.

**Architecture:** Extend `TwoPhaseFileScheduler` state with milestone decomposition metadata, tag generated tasks with wave lineage, and replace the one-decomposition-task guard with terminal-batch and max-wave checks. Keep the default wave limit at one for compatibility.

**Tech Stack:** Python 3.12 standard library, existing `unittest` suite, file-backed two-phase scheduler.

---

### Task 1: Generated Task Lineage And Milestone State

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`

- [x] **Step 1: Write failing lineage test**

Add a decomposition test that applies one planner proposal and asserts the
generated task contains:

```json
{
  "generated_by_decomposition_task_id": "DECOMPOSE-M26-001",
  "decomposition_wave": 1
}
```

Also assert `scheduler.state["milestones"]["M26"]` records wave count and
generated task ids.

- [x] **Step 2: Verify red**

Run the focused lineage test. Expected: generated task lineage and milestone
state are missing.

- [x] **Step 3: Implement lineage and milestone state update**

Add a `milestones` map to scheduler state. When decomposition is applied, tag
generated tasks and update milestone decomposition metadata.

- [x] **Step 4: Verify green**

Run the focused lineage test. Expected: pass.

### Task 2: Rolling Wave Creation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`

- [x] **Step 1: Write failing rolling-wave test**

Add a test with `decomposition_max_waves=2` that:

- dispatches and applies `DECOMPOSE-M26-001`;
- completes the generated worker task;
- calls `dispatch_ready()` again;
- asserts `DECOMPOSE-M26-002` is created and dispatched.

- [x] **Step 2: Verify red**

Run the focused rolling-wave test. Expected: scheduler stays idle because any
existing decomposition task blocks future decomposition.

- [x] **Step 3: Implement terminal-batch and max-wave checks**

Replace the existing any-decomposition-task guard with:

- previous decomposition tasks must be terminal;
- previous generated batch must be terminal;
- wave count must be below `decomposition_max_waves`.

- [x] **Step 4: Verify green**

Run the focused rolling-wave test. Expected: pass.

### Task 3: Max-Wave Milestone Completion

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`

- [x] **Step 1: Write failing max-wave test**

After a milestone reaches `decomposition_max_waves`, assert another
`dispatch_ready()` call does not create a third decomposition task and
`state["milestones"]["M26"]["milestone_status"] == "completed"` when all
generated tasks are done.

- [x] **Step 2: Verify red**

Run the focused max-wave test. Expected: milestone terminal status is missing.

- [x] **Step 3: Implement terminal milestone status**

When no further wave can be opened because the maximum is reached, mark the
milestone `completed` if all generated tasks are done and `blocked` if any are
blocked.

- [x] **Step 4: Verify green**

Run the focused max-wave test. Expected: pass.

### Task 4: Docs, Verification, Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Modify: runtime, test, and artifact files from Tasks 1 to 3.

- [x] **Step 1: Update docs and roadmap**

Add M26 notes to `m0_file_runtime.md`; mark M26 implemented in
`native_runtime_roadmap.md` and set the next recommended milestone to M27.

- [x] **Step 2: Run full verification**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime
git diff --check
```

- [x] **Step 3: Run placeholder scan**

```bash
rg -n 'TB[D]|TO[D]O|implement later|fill in details|Similar to|appropriate placeholder' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m26-rolling-milestone-decomposition.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m26-rolling-milestone-decomposition.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md
```

- [x] **Step 4: Commit and push**

```bash
git add experiments/native_agentteam_runtime
git commit -m "Add M26 rolling milestone decomposition"
git push origin native-runtime-m0
```

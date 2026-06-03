# M25 Proposal Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject low-quality planner-generated tasks before backlog insertion.

**Architecture:** Extend `task_proposal.py` with deterministic dependency, cycle, risk, and task-size checks. Keep scheduler authority unchanged, but include proposal rejection details in validation event payloads for inspection.

**Tech Stack:** Python 3.12 standard library, existing `unittest` suite, two-phase scheduler proposal path.

---

### Task 1: Proposal Dependency And Risk Rules

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py`

- [x] **Step 1: Write failing validator tests**

Add tests for:

- self-dependency rejection;
- generated dependency cycle rejection;
- unsupported `risk_target` rejection;
- `L0` with multiple write scopes rejection;
- `L2` normalization to `backlog_status=blocked` with `requires_review`.

- [x] **Step 2: Verify red**

Run the focused validator tests. Expected: the new invalid proposals are still
accepted, and `L2` remains ready.

- [x] **Step 3: Implement validator rules**

Add:

- `_validate_self_dependency(...)`;
- `_validate_generated_dependency_cycles(...)`;
- `_validate_risk_target(...)`;
- `_apply_risk_policy(...)`.

Keep the public `normalize_task_proposal(...)` return shape unchanged.

- [x] **Step 4: Verify green**

Run the same focused validator tests. Expected: pass.

### Task 2: Scheduler Rejection Evidence

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`

- [x] **Step 1: Write failing scheduler evidence test**

Add a two-phase decomposition test where the planner returns a self-dependent
generated task. Assert that:

- `decomposition_status == "rejected"`;
- `decomposition_error` contains `self dependency`;
- the canonical `events.jsonl` contains a `validation_rejected` payload with
  the same `decomposition_error`.

- [x] **Step 2: Verify red**

Run the focused scheduler test. Expected: validation event payload does not yet
contain `decomposition_error`.

- [x] **Step 3: Add rejection detail to validation event payload**

When `_apply_decomposition_result(...)` adds `decomposition_error` to `result`,
include `decomposition_status` and `decomposition_error` in the later
`validation_rejected` payload.

- [x] **Step 4: Verify green**

Run the focused scheduler evidence test. Expected: pass.

### Task 3: Docs And Roadmap

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Update implementation docs**

Add M25 notes covering self-dependency, cycle checks, risk target rules, L2
review blocking, and rejection evidence.

- [x] **Step 2: Update roadmap**

Mark M25 implemented and set the next recommended milestone to M26.

### Task 4: Full Verification And Commit

**Files:**
- Modify: runtime, test, and artifact files from Tasks 1 to 3.

- [x] **Step 1: Run full runtime tests**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
```

- [x] **Step 2: Run artifact lint**

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
```

- [x] **Step 3: Run syntax and repository checks**

```bash
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime
git diff --check
rg -n 'TB[D]|TO[D]O|implement later|fill in details|Similar to|appropriate placeholder' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m25-proposal-quality-gate.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m25-proposal-quality-gate.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md
```

- [x] **Step 4: Commit and push**

```bash
git add experiments/native_agentteam_runtime
git commit -m "Add M25 proposal quality gate"
git push origin native-runtime-m0
```

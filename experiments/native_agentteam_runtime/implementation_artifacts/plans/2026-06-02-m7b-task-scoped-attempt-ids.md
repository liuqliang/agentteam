# M7b Task-Scoped Attempt IDs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the file scheduler loop process multiple worktree-backed tasks in
one run without reusing attempt worktree ids or git branch names.

**Architecture:** Preserve existing single-task `run_simulation(...)` ids by
default. Add an optional attempt id namespace used by `FileScheduler` for each
task. The scheduler will call `run_simulation(..., attempt_id_prefix=task_id)`,
so task worktrees become `WT-TASK-001-ATTEMPT-001` and branches become
`agentteam/TASK-001-ATTEMPT-001`.

**Tech Stack:** Python 3.12 standard library, git worktrees, `unittest`.

---

### Task 1: Worktree-Backed Scheduler Loop

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing worktree loop test**

Add a test that creates a git repo, writes a two-task ready backlog, and runs:

```python
summary = run_scheduler_loop(
    FIXTURES / "sample_agent_pool.json",
    backlog_path,
    output_dir,
    clock=FixedClock(),
    project_root=repo,
    runtime_adapter=FakeRuntimeAdapter(),
)
```

Assert both tasks are processed and each step result has a distinct branch:

```python
self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
self.assertEqual(
    [step["result"]["branch"] for step in summary["steps"]],
    ["agentteam/TASK-001-ATTEMPT-001", "agentteam/TASK-002-ATTEMPT-001"],
)
```

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail because the second task tries to create the
already-existing `agentteam/ATTEMPT-001` branch.

- [x] **Step 3: Implement task-scoped attempt ids**

Add `attempt_id_prefix=None` to `run_simulation(...)`.

Default:

```python
attempt_id = "ATTEMPT-001"
```

Scheduler loop:

```python
attempt_id = "TASK-001-ATTEMPT-001"
```

Keep default public behavior unchanged for existing single-task tests.

- [x] **Step 4: Verify green**

Run the focused worktree loop test and existing scheduler loop tests. Expected:
pass.

### Task 2: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m7b-task-scoped-attempt-ids.md`

- [x] **Step 1: Document M7b**

Document that task-scoped attempt ids are used only by scheduler loops. Plain
`run_simulation(...)` keeps existing ids.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m7b
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m7b-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- worktree-backed scheduler loop test first failed because the second task
  attempted to create existing branch `agentteam/ATTEMPT-001`;
- focused test passed after adding `attempt_id_prefix` and having
  `FileScheduler` pass the task id as the prefix;
- scheduler focused tests ran 4 tests with `OK`;
- unit test discovery ran 36 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression kept the existing single-task id shape
  `ATTEMPT-001`;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

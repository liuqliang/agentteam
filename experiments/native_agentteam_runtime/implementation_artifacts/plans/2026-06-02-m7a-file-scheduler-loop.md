# M7a File Scheduler Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first persistent scheduler loop facade that can process more
than one ready backlog task without requiring a user command between tasks.

**Architecture:** Keep M7a as a file-backed sequential loop over the existing
single-task runtime path. Add a small `FileScheduler` API with `step_once()` and
`run_until_idle()`. Persist scheduler state to JSON after each step. Do not add
database storage, concurrent workers, long-lived model sessions, or merge-to-main.

**Tech Stack:** Python 3.12 standard library, JSON files, `unittest`, existing
runtime adapters and event/mailbox files.

---

### Task 1: Scheduler Loop API

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing test for sequential ready tasks**

Add a test that writes a backlog with two ready tasks and runs:

```python
summary = run_scheduler_loop(
    FIXTURES / "sample_agent_pool.json",
    backlog_path,
    output_dir,
    clock=FixedClock(),
    runtime_adapter=FakeRuntimeAdapter(),
)
```

Assert:

```python
self.assertEqual(summary["scheduler_status"], "idle")
self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
self.assertEqual(summary["step_count"], 2)
self.assertTrue(Path(summary["state_path"]).exists())
```

Read the state file and assert both task statuses are `done`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_scheduler_loop_runs_ready_tasks_until_idle -v
```

Expected: fail because `run_scheduler_loop` is not exported.

- [x] **Step 3: Implement minimal loop**

Add:

```python
class FileScheduler:
    def step_once(self):
        ...

    def run_until_idle(self, max_steps=100):
        ...

def run_scheduler_loop(...):
    scheduler = FileScheduler(...)
    return scheduler.run_until_idle(max_steps=max_steps)
```

Persist state at:

```text
<output-dir>/state/scheduler_state.json
```

Each step writes a single-task backlog file under:

```text
<output-dir>/steps/STEP-0001-TASK-001/backlog.json
```

and delegates to `run_simulation(...)` in that step directory.

- [x] **Step 4: Verify green**

Run the focused test. Expected: pass.

### Task 2: Resume And Dependency Guard

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing resume/dependency test**

Create a backlog with:

```json
[
  {"task_id": "TASK-001", "backlog_status": "done"},
  {"task_id": "TASK-002", "backlog_status": "ready", "depends_on": ["TASK-001"]},
  {"task_id": "TASK-003", "backlog_status": "ready", "depends_on": ["TASK-MISSING"]}
]
```

Run `run_scheduler_loop(...)` and assert only `TASK-002` is processed. `TASK-003`
must remain `ready` because its dependency is not done.

- [x] **Step 2: Implement dependency readiness**

Select a ready task only when:

```python
task["backlog_status"] == "ready"
not task.get("blockers")
all(done_by_id.get(dep_id) for dep_id in task.get("depends_on", []))
```

- [x] **Step 3: Verify green**

Run focused scheduler tests. Expected: pass.

### Task 3: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m7a-file-scheduler-loop.md`

- [x] **Step 1: Document M7a**

Document that M7a is a sequential file scheduler loop. It is not concurrency,
not a durable process daemon, and not a long-lived Codex/Claude session manager.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m7a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m7a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- sequential loop test first failed because `run_scheduler_loop` was not
  exported, then passed after adding `FileScheduler`;
- dependency test first failed because a task with a missing dependency was
  executed, then passed after adding dependency readiness checks;
- resume test first failed because the second loop run repeated `TASK-001`,
  then passed after loading existing `scheduler_state.json`;
- unit test discovery ran 35 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression remained on the single-task path and returned an
  accepted result;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

# M17 Two-Phase Dispatch Collect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a side-by-side scheduler that dispatches ready tasks up to `max_inflight` and collects mailbox results in later ticks.

**Architecture:** Create `two_phase_scheduler.py` rather than modifying the blocking `FileScheduler.step_once()` path. The two-phase scheduler writes canonical root events directly, tracks `inflight_attempts` in its own state file, and rebuilds the existing SQLite state index from the root event log. The daemon CLI gets a new worker-pool path that starts M16 workers and runs this scheduler.

**Tech Stack:** Python 3.12 standard library, existing JSONL mailbox protocol, existing worker pool supervisor, existing replay/state-index machinery, `unittest`.

---

### Task 1: Two-Phase Scheduler API

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing API test**

Add imports:

```python
import time

from agentteam_runtime import (
    TwoPhaseFileScheduler,
)
```

Add test:

```python
def test_two_phase_scheduler_dispatches_multiple_tasks_before_collecting(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task(
                    "TASK-002",
                    write_scope=["generated/task-002/"],
                    required_role="aux_role_1",
                ),
            ],
        )
        _write_agent_pool_with_agent_ids(
            agent_pool_path,
            ["agent-repo-map", "agent-doc-map"],
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")
        pool = FileMailboxWorkerPoolSupervisor(
            agent_pool_path,
            output_dir,
            env=env,
            poll_interval_seconds=0.01,
        )
        scheduler = TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=FixedClock(),
            max_inflight=2,
        )

        pool.start()
        try:
            dispatch = scheduler.dispatch_ready()
            self.assertEqual(dispatch["dispatch_status"], "dispatched")
            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(dispatch["inflight_count"], 2)
            self.assertEqual(scheduler.summary()["processed_task_ids"], [])

            collected = None
            for _ in range(50):
                collected = scheduler.collect_ready_results()
                if collected["collected_count"] == 2:
                    break
                time.sleep(0.02)
        finally:
            pool.stop()

        state = read_scheduler_state_index(output_dir)
        self.assertEqual(collected["collected_task_ids"], ["TASK-001", "TASK-002"])
        self.assertEqual(scheduler.summary()["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertEqual(scheduler.summary()["inflight_count"], 0)
        self.assertEqual(
            {task["task_id"]: task["task_status"] for task in state["tasks"]},
            {"TASK-001": "done", "TASK-002": "done"},
        )
```

Update `_backlog_task(...)` to accept `required_role="repo_map_agent"` and set
`"required_role": required_role`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_two_phase_scheduler_dispatches_multiple_tasks_before_collecting \
  -v
```

Expected red: import failure for `TwoPhaseFileScheduler`.

Observed red: import failure because `TwoPhaseFileScheduler` is not exported
from `agentteam_runtime`.

- [x] **Step 3: Implement two-phase scheduler**

Create `two_phase_scheduler.py` with:

- `TwoPhaseFileScheduler.__init__(..., max_inflight=2, state_path=None)`;
- `dispatch_ready()`;
- `collect_ready_results()`;
- `tick()`;
- `run_until_idle(max_ticks=100, poll_interval_seconds=0.02)`;
- `summary()`;
- private helpers for event append, ready task selection, outbox result reading,
  task update, and state writing.

Constraints:

- support `max_attempts=1` only;
- support `fake`/file mailbox worker pool results;
- no integration apply/verify/commit in this milestone;
- use existing event types and `rebuild_sqlite_state_index(...)`.

Export `TwoPhaseFileScheduler` and `run_two_phase_scheduler_loop` from
`__init__.py`.

- [x] **Step 4: Verify green**

Run the focused API test again. Expected: pass.

Observed green: focused two-phase scheduler API test passed.

### Task 2: Daemon CLI Two-Phase Worker Pool

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m17-two-phase-dispatch-collect.md`

- [x] **Step 1: Write failing CLI test**

Add test:

```python
def test_cli_can_run_two_phase_scheduler_with_static_worker_pool(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task(
                    "TASK-002",
                    write_scope=["generated/task-002/"],
                    required_role="aux_role_1",
                ),
            ],
        )
        _write_agent_pool_with_agent_ids(
            agent_pool_path,
            ["agent-repo-map", "agent-doc-map"],
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.cli",
                "--agent-pool",
                str(agent_pool_path),
                "--backlog",
                str(backlog_path),
                "--output-dir",
                str(output_dir),
                "--daemon-run-until-idle",
                "--daemon-two-phase-worker-pool",
                "--max-inflight",
                "2",
            ],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        state = read_scheduler_state_index(output_dir)

        self.assertEqual(completed.stderr, "")
        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["scheduler_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertEqual(summary["inflight_count"], 0)
        self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
        self.assertEqual(summary["worker_pool"]["worker_count"], 2)
        self.assertEqual(
            {task["task_id"]: task["task_status"] for task in state["tasks"]},
            {"TASK-001": "done", "TASK-002": "done"},
        )
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_can_run_two_phase_scheduler_with_static_worker_pool \
  -v
```

Expected red: CLI rejects unknown `--daemon-two-phase-worker-pool` or
`--max-inflight`.

Observed red: CLI rejected `--daemon-two-phase-worker-pool` and
`--max-inflight` as unrecognized arguments.

- [x] **Step 3: Wire CLI**

In `cli.py`:

- import `run_two_phase_scheduler_loop`;
- add `--daemon-two-phase-worker-pool`;
- add `--max-inflight`, default `2`;
- require `--daemon-run-until-idle`;
- make it mutually exclusive with all existing daemon mailbox worker flags;
- start `FileMailboxWorkerPoolSupervisor`;
- run `run_two_phase_scheduler_loop(...)`;
- stop the pool in `finally`;
- print summary with `daemon_status`, `snapshot`, and `worker_pool`.

- [x] **Step 4: Verify green**

Run the focused CLI test again. Expected: pass.

Observed green: focused two-phase worker pool CLI test passed.

- [x] **Step 5: Update docs**

Update `m0_file_runtime.md` with:

- public API import for `TwoPhaseFileScheduler` and `run_two_phase_scheduler_loop`;
- CLI example for `--daemon-two-phase-worker-pool --max-inflight 2`;
- M17 section explaining dispatch/collect, state file, root event log, and
  limitations;
- milestone summary line for M17.

Updated `m0_file_runtime.md` with M17 public API, CLI usage, dispatch/collect
semantics, state file, root event log behavior, and limitations.

- [x] **Step 6: Full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m17-two-phase-worker-pool-cli \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --max-inflight 2
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Supplemental TDD check:

- red: `test_two_phase_scheduler_does_not_double_book_same_agent` failed
  because dispatch returned `["TASK-001", "TASK-002"]` for one available
  `repo_map_agent`;
- green: the same test plus the two existing two-phase focused tests passed
  after marking inflight and newly selected agents busy during dispatch.

Observed pass:

- full unit test: `Ran 80 tests in 3.491s`, `OK`;
- artifact lint: `status: passed`, checked 21 JSON files and 1 JSONL file;
- two-phase worker-pool CLI smoke: `daemon_status: idle`,
  `scheduler_status: idle`, `processed_task_ids: ["TASK-001"]`;
- JSON validation: `find ... -name '*.json' -exec jq empty {} +` exited 0;
- sample JSONL validation: `jq -c . sample_events.jsonl` exited 0;
- bytecode compilation: `python3 -m compileall -q ...` exited 0;
- whitespace check: `git diff --check` exited 0.
- placeholder check: `rg` found no `TBD`, `TODO`, `implement later`,
  `fill in details`, `Similar to`, or `appropriate` matches in the M17 docs.

- [x] **Step 7: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m17-two-phase-dispatch-collect.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m17-two-phase-dispatch-collect.md
git commit -m "Add M17 two-phase dispatch collect scheduler"
git push origin native-runtime-m0
```

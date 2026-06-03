# M20 Worker Health Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add worker-pool health checks and restart of exited mailbox worker processes, then use that supervision in the two-phase CLI loop.

**Architecture:** Extend the existing process and pool supervisors instead of introducing a new daemon. Keep the scheduler state machine unchanged; the CLI orchestration layer interleaves worker supervision with scheduler ticks.

**Tech Stack:** Python 3.12 standard library, `subprocess.Popen`, existing mailbox worker process supervisor, existing two-phase scheduler, `unittest`.

---

### Task 1: Process Health API

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing process health test**

Add test near existing worker process supervisor tests:

```python
def test_file_mailbox_worker_process_supervisor_reports_health(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")
        supervisor = FileMailboxWorkerProcessSupervisor(
            agent_pool_path,
            output_dir,
            "agent-repo-map",
            env=env,
            poll_interval_seconds=0.01,
        )

        before = supervisor.health()
        start = supervisor.start()
        try:
            running = supervisor.health()
        finally:
            stop = supervisor.stop()
        stopped = supervisor.health()

        self.assertEqual(before["worker_status"], "not_started")
        self.assertEqual(running["worker_status"], "running")
        self.assertEqual(running["worker_pid"], start["worker_pid"])
        self.assertEqual(running["exit_code"], None)
        self.assertEqual(stop["worker_status"], "stopped")
        self.assertEqual(stopped["worker_status"], "exited")
        self.assertEqual(stopped["exit_code"], 0)
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_process_supervisor_reports_health \
  -v
```

Expected red: `FileMailboxWorkerProcessSupervisor` has no `health()` method.

Observed red:

```text
AttributeError: 'FileMailboxWorkerProcessSupervisor' object has no attribute 'health'
```

- [x] **Step 3: Implement process health**

In `mailbox_worker.py`, add:

```python
def health(self):
    if not self.process:
        return {
            "worker_status": "not_started",
            "worker_pid": None,
            "worker_agent_id": self.agent_id,
            "worker_runtime": self.runtime,
            "exit_code": None,
        }
    exit_code = self.process.poll()
    return {
        "worker_status": "running" if exit_code is None else "exited",
        "worker_pid": self.process.pid,
        "worker_agent_id": self.agent_id,
        "worker_runtime": self.runtime,
        "exit_code": exit_code,
    }
```

- [x] **Step 4: Verify green**

Run the focused process health test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.027s

OK
```

### Task 2: Pool Restart API

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/worker_pool.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing pool restart test**

Add test near `test_file_mailbox_worker_pool_supervisor_starts_and_stops_all_agents`:

```python
def test_file_mailbox_worker_pool_supervisor_restarts_exited_worker(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")
        pool = FileMailboxWorkerPoolSupervisor(
            agent_pool_path,
            output_dir,
            env=env,
            poll_interval_seconds=0.01,
        )

        start = pool.start()
        first_pid = start["workers"][0]["worker_pid"]
        pool.workers[0].process.terminate()
        pool.workers[0].process.wait(timeout=5)
        degraded = pool.health_check()
        restarted = pool.restart_exited_workers()
        try:
            recovered = pool.health_check()
        finally:
            stop = pool.stop()

        registry = json.loads(
            Path(stop["process_registry_path"]).read_text(encoding="utf-8")
        )

        self.assertEqual(degraded["pool_status"], "degraded")
        self.assertEqual(degraded["workers"][0]["worker_status"], "exited")
        self.assertEqual(restarted["restarted_count"], 1)
        self.assertEqual(restarted["workers"][0]["restart_status"], "restarted")
        self.assertNotEqual(restarted["workers"][0]["new_worker"]["worker_pid"], first_pid)
        self.assertEqual(recovered["pool_status"], "running")
        self.assertEqual(recovered["workers"][0]["worker_status"], "running")
        self.assertEqual(registry["workers"][0]["restart_count"], 1)
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_pool_supervisor_restarts_exited_worker \
  -v
```

Expected red: `FileMailboxWorkerPoolSupervisor` has no `health_check()` or
`restart_exited_workers()` method.

Observed red:

```text
AttributeError: 'FileMailboxWorkerPoolSupervisor' object has no attribute 'health_check'
```

- [x] **Step 3: Implement pool restart**

In `mailbox_worker.py`, add `restart_if_exited()` to
`FileMailboxWorkerProcessSupervisor`.

In `worker_pool.py`:

- keep `self.restart_counts = {}`;
- add `health_check()`;
- add `restart_exited_workers()`;
- add `supervise_once()`;
- write the registry after each health/restart call;
- preserve existing start/stop summary shape.

- [x] **Step 4: Verify green**

Run the focused pool restart test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.029s

OK
```

### Task 3: Two-Phase CLI Supervision Summary

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write failing CLI summary test**

Extend `test_cli_can_run_two_phase_scheduler_with_static_worker_pool` with:

```python
self.assertEqual(summary["worker_pool_health"]["pool_status"], "running")
self.assertGreaterEqual(len(summary["worker_pool_supervision"]), 1)
self.assertIn("restart_count", summary["worker_pool_health"]["workers"][0])
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_can_run_two_phase_scheduler_with_static_worker_pool \
  -v
```

Expected red: CLI summary lacks `worker_pool_health` and
`worker_pool_supervision`.

Observed red:

```text
KeyError: 'worker_pool_health'
```

- [x] **Step 3: Interleave supervision with two-phase scheduler ticks**

In `cli.py`, replace the two-phase branch call to `run_two_phase_scheduler_loop`
with:

```python
scheduler = TwoPhaseFileScheduler(...)
supervision = []
for _ in range(args.max_steps):
    supervision.append(worker_pool.supervise_once())
    tick = scheduler.tick()
    supervision.append(worker_pool.supervise_once())
    if tick["tick_status"] == "idle":
        result = {**scheduler.summary(), "scheduler_status": "idle", "tick_count": ..., "last_tick": tick}
        break
    if tick["tick_status"] == "waiting":
        time.sleep(0.02)
else:
    result = {**scheduler.summary(), "scheduler_status": "max_ticks_reached", ...}
```

Import `time` and `TwoPhaseFileScheduler` as needed. Include:

- `worker_pool_health=worker_pool.health_check()` before stop;
- `worker_pool_supervision=supervision`;
- existing final `worker_pool` merged start/stop summary.

Update `m0_file_runtime.md` with the M20 worker health/restart behavior and
current limits.

- [x] **Step 4: Verify green**

Run the focused CLI summary test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.163s

OK
```

### Task 4: Full Verification And Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m20-worker-health-restart.md`

- [x] **Step 1: Full verification**

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
  --output-dir /tmp/agentteam-m20-worker-health-cli \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --max-inflight 2 \
  --max-attempts 2 \
  --lease-timeout-seconds 900
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
rg -n 'TB[D]|TO[D]O|implement[ ]later|fill[ ]in[ ]details|Similar[ ]to|approp[r]iate' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m20-worker-health-restart.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m20-worker-health-restart.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md
```

- [x] **Step 2: Record observed verification**

Update this plan with exact observed pass or failure lines from the commands.

Observed verification:

```text
unittest discover: Ran 86 tests in 3.928s
unittest discover: OK
artifact_lint: {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
fresh CLI smoke: daemon_status=idle, scheduler_status=idle, tick_count=3
fresh CLI smoke: worker_pool_health.pool_status=running, worker_pool.pool_status=stopped
find *.json jq empty: exit 0
jq -c sample_events.jsonl: exit 0
compileall: exit 0
git diff --check: exit 0
placeholder scan: exit 1 with no matches, expected
```

- [ ] **Step 3: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/worker_pool.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m20-worker-health-restart.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m20-worker-health-restart.md
git commit -m "Add M20 worker health restart supervision"
git push origin native-runtime-m0
```

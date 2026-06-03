# M15a Long-Running Fake Mailbox Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run one long-lived fake mailbox worker process for a daemon run while preserving sequential scheduler execution.

**Architecture:** M15a keeps the existing per-step mailbox layout. A new `FileMailboxWorkerProcessSupervisor` starts one worker CLI process in `--serve` mode at the daemon root output directory. The serving worker scans root and `steps/*` mailbox inboxes for its agent id, processes dispatches through `FakeRuntimeAdapter`, writes outbox results, and keeps running until the supervisor stops it. A new `FileMailboxExternalRuntimeAdapter` does not start a worker; it waits for the current step outbox result written by the long-running process.

**Tech Stack:** Python 3.12 standard library, `subprocess.Popen`, JSONL file mailboxes, existing `FileSchedulerDaemon`, existing fake runtime, `unittest`.

---

### Task 1: Serve Loop and External Wait Adapter

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing long-running worker test**

Add imports:

```python
from agentteam_runtime import (
    FileMailboxExternalRuntimeAdapter,
    FileMailboxWorkerProcessSupervisor,
)
```

Add test:

```python
def test_scheduler_loop_can_use_long_running_mailbox_worker_process(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
            ],
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")
        supervisor = FileMailboxWorkerProcessSupervisor(
            FIXTURES / "sample_agent_pool.json",
            output_dir,
            "agent-repo-map",
            env=env,
            poll_interval_seconds=0.01,
        )

        start = supervisor.start()
        try:
            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FileMailboxExternalRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    timeout_seconds=5,
                    poll_interval_seconds=0.01,
                ),
            )
            self.assertIsNone(supervisor.process.poll())
        finally:
            stop = supervisor.stop()

        state = read_scheduler_state_index(output_dir)
        first_outbox = output_dir / "steps" / "STEP-0001-TASK-001" / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
        second_outbox = output_dir / "steps" / "STEP-0002-TASK-002" / "mailboxes" / "agent-repo-map" / "outbox.jsonl"

        self.assertEqual(start["worker_status"], "running")
        self.assertEqual(stop["worker_status"], "stopped")
        self.assertEqual(summary["scheduler_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertNotEqual(start["worker_pid"], os.getpid())
        self.assertTrue(first_outbox.exists())
        self.assertTrue(second_outbox.exists())
        self.assertEqual(
            {session["runtime_adapter"] for session in state["runtime_sessions"]},
            {"FileMailboxExternalRuntimeAdapter"},
        )
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_scheduler_loop_can_use_long_running_mailbox_worker_process \
  -v
```

Observed red: import failure because `FileMailboxExternalRuntimeAdapter` is not exported yet.

- [x] **Step 3: Implement serving worker scan**

In `mailbox_worker.py`:

- import `time`;
- add `FileMailboxWorker.poll_tree_once(root_output_dir, agent_pool_path, agent_id, runtime_adapter=None, clock=None)`;
- find candidate output dirs deterministically: root output dir first, then
  sorted `root_output_dir/steps/*` directories whose `mailboxes/<agent-id>/inbox.jsonl`
  exists;
- for each candidate, instantiate `FileMailboxWorker(...)` and call `poll_once()`;
- return the first processed summary with `mailbox_output_dir`; otherwise return idle.

Add CLI args:

```text
--serve
--poll-interval-seconds
--stop-file
```

When `--serve` is set, loop until the stop file exists:

```python
while not stop_file.exists():
    summary = FileMailboxWorker.poll_tree_once(...)
    processed_count += 1 if summary["poll_status"] == "processed" else 0
    time.sleep(args.poll_interval_seconds)
```

Print a JSON summary on exit with `worker_pid`, `serve_status`, and
`processed_count`.

- [x] **Step 4: Implement external adapter and supervisor**

In `mailbox_worker.py`, add:

- `FileMailboxExternalRuntimeAdapter(agent_pool_path, output_dir=None, timeout_seconds=60, poll_interval_seconds=0.05)`;
- `bind_output_dir(output_dir)`;
- `run(message, worktree_path=None)` waits for `_runtime_result_from_outbox(...)`
  to find a matching result until timeout, then returns either completed result
  or `{result_status: "timed_out", changed_files: [], output: {"adapter": "mailbox_external", ...}}`;
- `FileMailboxWorkerProcessSupervisor(agent_pool_path, output_dir, agent_id, command=None, env=None, poll_interval_seconds=0.05)`;
- `start()` launches `python -m agentteam_runtime.mailbox_worker --serve ...` with
  `subprocess.Popen`, stores `process`, writes no stop file, and returns pid/status;
- `stop()` writes stop file, waits up to 5 seconds, terminates/kills if needed, and
  returns stopped status.

Export both classes from `__init__.py`.

- [x] **Step 5: Verify green**

Run the focused long-running worker test. Expected: pass.

### Task 2: Daemon CLI Flag and Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m15a-long-running-fake-mailbox-worker.md`

- [x] **Step 1: Write failing daemon CLI long-running worker test**

Add test:

```python
def test_cli_can_run_file_daemon_with_long_running_mailbox_worker(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
            ],
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.cli",
                "--agent-pool",
                str(FIXTURES / "sample_agent_pool.json"),
                "--backlog",
                str(backlog_path),
                "--output-dir",
                str(output_dir),
                "--daemon-run-until-idle",
                "--daemon-long-running-mailbox-worker",
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        state = read_scheduler_state_index(output_dir)

        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
        self.assertEqual(
            {session["runtime_adapter"] for session in state["runtime_sessions"]},
            {"FileMailboxExternalRuntimeAdapter"},
        )
```

- [x] **Step 2: Verify red**

Observed red: CLI rejected unknown `--daemon-long-running-mailbox-worker` with exit 2.

- [x] **Step 3: Implement CLI flag**

In `cli.py`:

- import `FileMailboxExternalRuntimeAdapter` and `FileMailboxWorkerProcessSupervisor`;
- add `--daemon-long-running-mailbox-worker`;
- require `--daemon-run-until-idle`;
- reject combining it with `--daemon-mailbox-worker` or `--daemon-mailbox-subprocess-worker`;
- reject runtime command overrides/Codex options for M15a;
- before `run_file_daemon(...)`, start the supervisor for `agent-repo-map`;
- pass `FileMailboxExternalRuntimeAdapter(args.agent_pool)` as `runtime_adapter`;
- always stop the supervisor in `finally`;
- include `worker_process` with start/stop summaries in the printed daemon result.

- [x] **Step 4: Update artifact docs**

Document:

- worker `--serve` mode;
- root/steps mailbox scan rule;
- `FileMailboxExternalRuntimeAdapter`;
- `FileMailboxWorkerProcessSupervisor`;
- CLI flag `--daemon-long-running-mailbox-worker`;
- limitation: one fake worker process, one agent id, sequential scheduler, no
  restart/backoff, no Codex/Claude long-running process.

Updated `m0_file_runtime.md` with the public API, CLI flag, worker serve mode,
scan rule, supervisor/adapter contract, and M15a limitations.

- [x] **Step 5: Full verification**

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
  --output-dir /tmp/agentteam-m15a-long-worker-cli \
  --daemon-run-until-idle \
  --daemon-long-running-mailbox-worker
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Observed pass:

- unittest discover: 72 tests passed;
- artifact lint: passed, 21 JSON files and 1 JSONL file checked;
- long-running worker CLI smoke: daemon idle, one sample task processed, worker
  stopped through stop file, `worker_process.stderr` empty;
- JSON/JQ checks: passed;
- compileall: passed;
- git diff check: passed.

During verification, the worker CLI exposed a `runpy` warning caused by eager
package exports importing `agentteam_runtime.mailbox_worker` before executing
it as `python -m`. The package API exports were changed to lazy loading, and
tests now assert clean worker stderr.

- [x] **Step 6: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m15a-long-running-fake-mailbox-worker.md
git commit -m "Add M15a long-running fake mailbox worker"
git push origin native-runtime-m0
```

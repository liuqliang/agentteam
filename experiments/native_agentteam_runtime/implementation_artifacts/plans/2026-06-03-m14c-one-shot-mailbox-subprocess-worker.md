# M14c One-Shot Mailbox Subprocess Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the M14b mailbox worker in a real one-shot OS subprocess while keeping scheduler execution sequential.

**Architecture:** M14c adds a worker CLI entrypoint to `mailbox_worker.py` and a `FileMailboxSubprocessRuntimeAdapter`. The scheduler still writes dispatch messages to inbox JSONL, but the runtime adapter launches `python -m agentteam_runtime.mailbox_worker` once per dispatch, waits for it to poll the message and write outbox JSONL, then reads the matching `runtime_result`. This validates subprocess lifecycle, PID reporting, timeout failure, and stdout parsing without introducing a long-lived process supervisor.

**Tech Stack:** Python 3.12 standard library, `argparse`, `subprocess`, JSONL inbox/outbox files, existing `FileMailboxWorker`, `unittest`.

---

### Task 1: Worker CLI Entrypoint

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing worker CLI test**

Add test:

```python
def test_file_mailbox_worker_cli_processes_one_message_in_subprocess(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
        outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
        message = _mailbox_dispatch_message(
            message_id="MSG-SUBPROCESS-001",
            agent_id="agent-repo-map",
            write_scope=["generated/"],
        )
        _append_test_jsonl(inbox, [message])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.mailbox_worker",
                "--agent-pool",
                str(FIXTURES / "sample_agent_pool.json"),
                "--output-dir",
                str(output_dir),
                "--agent-id",
                "agent-repo-map",
                "--message-id",
                "MSG-SUBPROCESS-001",
                "--runtime",
                "fake",
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        result_message = _read_first_jsonl(outbox)

        self.assertEqual(summary["poll_status"], "processed")
        self.assertEqual(summary["source_message_id"], "MSG-SUBPROCESS-001")
        self.assertNotEqual(summary["worker_pid"], os.getpid())
        self.assertEqual(result_message["message_type"], "runtime_result")
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_cli_processes_one_message_in_subprocess \
  -v
```

Observed red: `mailbox_worker.py` ran but produced no JSON stdout, causing
`json.loads(completed.stdout)` to fail.

- [x] **Step 3: Implement CLI entrypoint**

In `mailbox_worker.py`:

- import `argparse` and `os`;
- add `_runtime_adapter_from_name(name)` supporting only `fake`;
- add `main(argv=None)` that parses `--agent-pool`, `--output-dir`, `--agent-id`,
  optional `--message-id`, optional `--worktree-path`, and `--runtime fake`;
- instantiate `FileMailboxWorker`, call `poll_once(...)`, attach
  `worker_pid=os.getpid()`, print sorted JSON, and return `0`;
- add `if __name__ == "__main__": raise SystemExit(main())`.

- [x] **Step 4: Verify green**

Run the focused worker CLI test. Expected: pass.

### Task 2: Subprocess Runtime Adapter

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing subprocess adapter scheduler test**

Add import:

```python
from agentteam_runtime import FileMailboxSubprocessRuntimeAdapter
```

Add test:

```python
def test_scheduler_loop_can_run_mailbox_worker_as_one_shot_subprocess(self):
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

        summary = run_scheduler_loop(
            FIXTURES / "sample_agent_pool.json",
            backlog_path,
            output_dir,
            clock=FixedClock(),
            runtime_adapter=FileMailboxSubprocessRuntimeAdapter(
                FIXTURES / "sample_agent_pool.json",
                timeout_seconds=30,
            ),
        )
        state = read_scheduler_state_index(output_dir)
        first_outbox = output_dir / "steps" / "STEP-0001-TASK-001" / "mailboxes" / "agent-repo-map" / "outbox.jsonl"

        self.assertEqual(summary["scheduler_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertTrue(first_outbox.exists())
        self.assertEqual(
            {session["runtime_adapter"] for session in state["runtime_sessions"]},
            {"FileMailboxSubprocessRuntimeAdapter"},
        )
```

- [x] **Step 2: Verify red**

Observed red: import failure because `FileMailboxSubprocessRuntimeAdapter` is not exported yet.

- [x] **Step 3: Implement subprocess adapter**

In `mailbox_worker.py`, add `FileMailboxSubprocessRuntimeAdapter`:

- default command: `[sys.executable, "-m", "agentteam_runtime.mailbox_worker"]`;
- attributes: `agent_pool_path`, `output_dir`, `command`, `timeout_seconds`,
  and `runtime`;
- `bind_output_dir(output_dir)` returns a new adapter with the bound output dir;
- `run(message, worktree_path=None)` validates output dir, builds the worker CLI
  command, runs it with `subprocess.run(...)`, and handles timeout/non-zero
  results;
- after subprocess success, read the matching outbox result via
  `_runtime_result_from_outbox(...)`;
- merge subprocess metadata into `result["output"]["mailbox_subprocess"]` with
  `worker_pid`, `exit_code`, and `stdout`.

Export `FileMailboxSubprocessRuntimeAdapter` from `__init__.py`.

- [x] **Step 4: Verify green**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_cli_processes_one_message_in_subprocess \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_scheduler_loop_can_run_mailbox_worker_as_one_shot_subprocess \
  -v
```

Expected: both pass.

### Task 3: Daemon CLI Flag and Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m14c-one-shot-mailbox-subprocess-worker.md`

- [x] **Step 1: Write failing daemon CLI subprocess test**

Add test:

```python
def test_cli_can_run_file_daemon_with_mailbox_subprocess_worker(self):
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
                "--daemon-mailbox-subprocess-worker",
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
        self.assertEqual(
            {session["runtime_adapter"] for session in state["runtime_sessions"]},
            {"FileMailboxSubprocessRuntimeAdapter"},
        )
```

- [x] **Step 2: Verify red**

Observed red: CLI rejected unknown `--daemon-mailbox-subprocess-worker` with exit 2.

- [x] **Step 3: Implement CLI flag**

In `cli.py`:

- import `FileMailboxSubprocessRuntimeAdapter`;
- add `--daemon-mailbox-subprocess-worker`;
- require `--daemon-run-until-idle`;
- reject combining it with `--daemon-mailbox-worker`;
- reject runtime command overrides or Codex options for this flag;
- pass `FileMailboxSubprocessRuntimeAdapter(args.agent_pool)` as
  `runtime_adapter` to `run_file_daemon(...)`.

- [x] **Step 4: Update artifact docs**

Document:

- worker CLI command;
- `FileMailboxSubprocessRuntimeAdapter`;
- subprocess metadata in runtime output;
- CLI flag `--daemon-mailbox-subprocess-worker`;
- limitation: one subprocess per dispatch, no long-lived worker process,
  no restart/backoff policy yet.

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
  --output-dir /tmp/agentteam-m14c-subprocess-daemon-cli \
  --daemon-run-until-idle \
  --daemon-mailbox-subprocess-worker
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Observed on 2026-06-03:

```text
python3 -m unittest discover ... Ran 70 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.cli ... --daemon-run-until-idle --daemon-mailbox-subprocess-worker ... exit 0
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

- [x] **Step 6: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m14c-one-shot-mailbox-subprocess-worker.md
git commit -m "Add M14c one-shot mailbox subprocess worker"
git push origin native-runtime-m0
```

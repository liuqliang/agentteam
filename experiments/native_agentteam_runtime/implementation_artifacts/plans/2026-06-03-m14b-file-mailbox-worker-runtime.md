# M14b File Mailbox Worker Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mailbox-polling worker runtime path so scheduler dispatches can round-trip through file inbox/outbox messages before validation.

**Architecture:** M14b introduces `FileMailboxWorker` and `FileMailboxRuntimeAdapter`. The worker reads dispatch messages from the selected agent inbox, runs an existing delegate adapter such as `FakeRuntimeAdapter`, and writes a `runtime_result` message to the agent outbox. The runtime adapter preserves the existing `runtime_adapter.run(message, worktree_path)` contract by binding to the current output directory and reading the matching outbox result; this keeps `FileScheduler` sequential while establishing the mailbox protocol used by future long-lived worker processes.

**Tech Stack:** Python 3.12 standard library, JSONL inbox/outbox files, existing `FakeRuntimeAdapter`, existing `FileScheduler`, `unittest`.

---

### Task 1: Mailbox Worker API

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing worker poll test**

Add imports:

```python
from agentteam_runtime import (
    FileMailboxWorker,
)
```

Add test:

```python
def test_file_mailbox_worker_poll_once_writes_runtime_result_to_outbox(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
        outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
        message = _mailbox_dispatch_message(
            message_id="MSG-MAILBOX-001",
            agent_id="agent-repo-map",
            write_scope=["generated/"],
        )
        _append_test_jsonl(inbox, [message])

        worker = FileMailboxWorker(
            FIXTURES / "sample_agent_pool.json",
            output_dir,
            "agent-repo-map",
            runtime_adapter=FakeRuntimeAdapter(),
            clock=FixedClock(),
        )
        summary = worker.poll_once()

        result_message = _read_first_jsonl(outbox)

        self.assertEqual(summary["poll_status"], "processed")
        self.assertEqual(summary["source_message_id"], "MSG-MAILBOX-001")
        self.assertEqual(result_message["message_type"], "runtime_result")
        self.assertEqual(result_message["payload"]["source_message_id"], "MSG-MAILBOX-001")
        self.assertEqual(result_message["payload"]["result_status"], "completed")
        self.assertEqual(result_message["payload"]["changed_files"], ["generated/m0_generated_repo_index.json"])
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_poll_once_writes_runtime_result_to_outbox \
  -v
```

Observed red: import failure because `FileMailboxWorker` is not exported yet.

- [x] **Step 3: Implement worker poll path**

Create `mailbox_worker.py` with:

```python
import json
from pathlib import Path

from .m0_runtime import FakeRuntimeAdapter, SystemClock


class FileMailboxWorker:
    def __init__(
        self,
        agent_pool_path,
        output_dir,
        agent_id,
        runtime_adapter=None,
        clock=None,
    ):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir)
        self.agent_id = agent_id
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self.clock = clock or SystemClock()
        self.agent = self._load_agent()
        self.inbox_path = self.output_dir / self.agent["inbox_path"]
        self.outbox_path = self.output_dir / self.agent["outbox_path"]

    def poll_once(self, message_id=None, worktree_path=None):
        message = self._next_dispatch(message_id=message_id)
        if not message:
            return {"poll_status": "idle", "reason": "no_dispatch_message"}
        runtime_result = self.runtime_adapter.run(message, worktree_path=worktree_path)
        result_message = self._result_message(message, runtime_result)
        _append_jsonl(self.outbox_path, [result_message])
        return {
            "poll_status": "processed",
            "source_message_id": message["message_id"],
            "result_status": runtime_result["result_status"],
            "changed_files": runtime_result["changed_files"],
            "outbox_path": str(self.outbox_path),
        }

    def _next_dispatch(self, message_id=None):
        answered = {
            record.get("payload", {}).get("source_message_id")
            for record in _read_jsonl_if_exists(self.outbox_path)
            if record.get("message_type") == "runtime_result"
        }
        for record in _read_jsonl_if_exists(self.inbox_path):
            if record.get("message_type") != "dispatch_task":
                continue
            if record.get("message_id") in answered:
                continue
            if message_id and record.get("message_id") != message_id:
                continue
            return record
        return None

    def _result_message(self, message, runtime_result):
        return {
            "message_id": f"RESULT-{message['message_id']}",
            "from_agent": self.agent_id,
            "to_agent": message["from_agent"],
            "message_type": "runtime_result",
            "correlation_id": message["correlation_id"],
            "created_at": self.clock.now(),
            "payload": {
                "source_message_id": message["message_id"],
                "task_id": message["payload"]["task_id"],
                "attempt_id": message["payload"]["attempt_id"],
                "lease_id": message["payload"]["lease_id"],
                "result_status": runtime_result["result_status"],
                "changed_files": runtime_result["changed_files"],
                "output": runtime_result.get("output", {}),
            },
        }

    def _load_agent(self):
        agent_pool = json.loads(self.agent_pool_path.read_text(encoding="utf-8"))
        for agent in agent_pool.get("agents", []):
            if agent.get("agent_id") == self.agent_id:
                return agent
        raise ValueError(f"agent not found in agent pool: {self.agent_id}")
```

Also implement local helpers `_append_jsonl(...)` and `_read_jsonl_if_exists(...)`.
Export `FileMailboxWorker` from `__init__.py`.

- [x] **Step 4: Verify green**

Run the focused worker test. Expected: pass.

### Task 2: Runtime Adapter Bridge

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing adapter scheduler-loop test**

Add imports:

```python
from agentteam_runtime import (
    FileMailboxRuntimeAdapter,
)
```

Add test:

```python
def test_scheduler_loop_can_round_trip_runtime_result_through_mailbox_worker(self):
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
            runtime_adapter=FileMailboxRuntimeAdapter(
                FIXTURES / "sample_agent_pool.json",
                runtime_adapter=FakeRuntimeAdapter(),
                clock=FixedClock(),
            ),
        )

        first_outbox = (
            output_dir
            / "steps"
            / "STEP-0001-TASK-001"
            / "mailboxes"
            / "agent-repo-map"
            / "outbox.jsonl"
        )
        state = read_scheduler_state_index(output_dir)

        self.assertEqual(summary["scheduler_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertTrue(first_outbox.exists())
        self.assertEqual(_read_first_jsonl(first_outbox)["message_type"], "runtime_result")
        self.assertEqual(
            {session["runtime_adapter"] for session in state["runtime_sessions"]},
            {"FileMailboxRuntimeAdapter"},
        )
```

- [x] **Step 2: Verify red**

Observed red: import failure because `FileMailboxRuntimeAdapter` is not exported yet.

- [x] **Step 3: Implement adapter bridge**

Add `FileMailboxRuntimeAdapter` in `mailbox_worker.py`:

```python
class FileMailboxRuntimeAdapter:
    def __init__(self, agent_pool_path, output_dir=None, runtime_adapter=None, clock=None):
        self.agent_pool_path = Path(agent_pool_path)
        self.output_dir = Path(output_dir) if output_dir else None
        self.runtime_adapter = runtime_adapter or FakeRuntimeAdapter()
        self.clock = clock or SystemClock()

    def bind_output_dir(self, output_dir):
        return FileMailboxRuntimeAdapter(
            self.agent_pool_path,
            output_dir=output_dir,
            runtime_adapter=self.runtime_adapter,
            clock=self.clock,
        )

    def run(self, message, worktree_path=None):
        if not self.output_dir:
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "mailbox", "error": "missing_output_dir"},
            }
        worker = FileMailboxWorker(
            self.agent_pool_path,
            self.output_dir,
            message["to_agent"],
            runtime_adapter=self.runtime_adapter,
            clock=self.clock,
        )
        poll_summary = worker.poll_once(
            message_id=message["message_id"],
            worktree_path=worktree_path,
        )
        if poll_summary["poll_status"] != "processed":
            return {
                "result_status": "failed",
                "changed_files": [],
                "output": {"adapter": "mailbox", "error": "mailbox_result_missing"},
            }
        return _runtime_result_from_outbox(worker.outbox_path, message["message_id"])
```

Add `_runtime_result_from_outbox(...)` to find `payload.source_message_id` and
return the scheduler runtime-result shape. Export `FileMailboxRuntimeAdapter`.

In `m0_runtime.py`, before calling `runtime_adapter.run(...)`, bind adapters
that expose `bind_output_dir`:

```python
runtime_adapter_for_attempt = _bind_runtime_adapter_output_dir(runtime_adapter, output_dir)
runtime_result = runtime_adapter_for_attempt.run(message, worktree_path=worktree_path)
```

Add:

```python
def _bind_runtime_adapter_output_dir(runtime_adapter, output_dir):
    binder = getattr(runtime_adapter, "bind_output_dir", None)
    if not binder:
        return runtime_adapter
    return binder(output_dir)
```

- [x] **Step 4: Verify green**

Run focused adapter test and both daemon tests. Expected: pass.

### Task 3: Daemon CLI Experiment and Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m14b-file-mailbox-worker-runtime.md`

- [x] **Step 1: Write failing CLI mailbox-worker test**

Add test:

```python
def test_cli_can_run_file_daemon_with_mailbox_worker_adapter(self):
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
                "--daemon-mailbox-worker",
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        first_outbox = output_dir / "steps" / "STEP-0001-TASK-001" / "mailboxes" / "agent-repo-map" / "outbox.jsonl"

        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
        self.assertTrue(first_outbox.exists())
```

- [x] **Step 2: Verify red**

Observed red: CLI rejected unknown `--daemon-mailbox-worker` with exit 2.

- [x] **Step 3: Implement CLI flag**

In `cli.py`, import `FakeRuntimeAdapter` and `FileMailboxRuntimeAdapter`. Add
`--daemon-mailbox-worker`. It must require `--daemon-run-until-idle` and reject
non-fake runtime overrides for M14b. When set, pass:

```python
runtime_adapter=FileMailboxRuntimeAdapter(
    args.agent_pool,
    runtime_adapter=FakeRuntimeAdapter(),
)
```

to `run_file_daemon(...)`.

- [x] **Step 4: Update artifact docs**

Document:

- `FileMailboxWorker`;
- `FileMailboxRuntimeAdapter`;
- outbox `runtime_result` message shape;
- CLI flag `--daemon-mailbox-worker`;
- limitation: M14b still invokes the worker in-process and sequentially; it does
  not supervise a separate long-running OS process.

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
  --output-dir /tmp/agentteam-m14b-mailbox-daemon-cli \
  --daemon-run-until-idle \
  --daemon-mailbox-worker
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Observed on 2026-06-03:

```text
python3 -m unittest discover ... Ran 67 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.cli ... --daemon-run-until-idle --daemon-mailbox-worker ... exit 0
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
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m14b-file-mailbox-worker-runtime.md
git commit -m "Add M14b file mailbox worker runtime"
git push origin native-runtime-m0
```

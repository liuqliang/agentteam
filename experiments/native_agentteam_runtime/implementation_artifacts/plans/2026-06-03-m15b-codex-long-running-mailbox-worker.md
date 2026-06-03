# M15b Codex Long-Running Mailbox Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the M15a long-running mailbox worker execute dispatches through `CodexRuntimeAdapter` while keeping one worker process and sequential scheduler execution.

**Architecture:** M15b does not change the scheduler mailbox protocol. The scheduler still writes dispatch messages that include `payload.worktree_path`, and `FileMailboxExternalRuntimeAdapter` still waits for outbox results. The worker process gains a `codex` delegate runtime option and calls the existing `CodexRuntimeAdapter` for each dispatch.

**Tech Stack:** Python 3.12 standard library, existing `CodexRuntimeAdapter`, JSONL file mailboxes, `subprocess.Popen`, `unittest`.

---

### Task 1: Worker CLI Codex Delegate

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing worker CLI Codex test**

Add a test in `M0RuntimeTests` near the existing mailbox worker CLI tests:

```python
def test_file_mailbox_worker_cli_can_use_codex_delegate_from_payload_worktree(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        output_dir = tmp_path / "run"
        fake_codex = tmp_path / "fake_codex_mailbox.py"
        target_file = "generated/mailbox_codex_delegate.json"
        _init_git_repo(repo)
        _write_fake_codex(fake_codex, changed_file=target_file)
        inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
        outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
        message = _mailbox_dispatch_message(
            message_id="MSG-CODEX-MAILBOX-001",
            agent_id="agent-repo-map",
            write_scope=["generated/"],
        )
        message["payload"]["worktree_path"] = str(repo)
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
                "MSG-CODEX-MAILBOX-001",
                "--runtime",
                "codex",
                "--codex-command-json",
                json.dumps([sys.executable, str(fake_codex)]),
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        result_message = _read_first_jsonl(outbox)

        self.assertEqual(completed.stderr, "")
        self.assertEqual(summary["poll_status"], "processed")
        self.assertEqual(summary["result_status"], "completed")
        self.assertTrue((repo / target_file).exists())
        self.assertEqual(result_message["payload"]["output"]["adapter"], "codex")
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_file_mailbox_worker_cli_can_use_codex_delegate_from_payload_worktree \
  -v
```

Expected red: the mailbox worker CLI rejects `--runtime codex` or the new
Codex command flag.

Observed red: CLI rejected `--runtime codex` because the worker runtime choices
were still limited to `fake`.

- [x] **Step 3: Implement worker delegate runtime factory**

In `mailbox_worker.py`:

- import `CodexRuntimeAdapter` from `.m0_runtime`;
- make `FileMailboxWorker.poll_once(...)` use
  `message["payload"]["worktree_path"]` when no explicit `worktree_path` is
  provided;
- add worker CLI flags:

```text
--codex-command-json
--codex-model
--codex-sandbox
--codex-timeout-seconds
```

- extend `--runtime` choices to `fake,codex`;
- replace `_runtime_adapter_from_name(...)` with an args-based factory that
  returns `FakeRuntimeAdapter()` for `fake` and `CodexRuntimeAdapter(...)` for
  `codex`;
- require `--codex-timeout-seconds >= 1`;
- reject Codex-only flags when `--runtime fake` is selected.

- [x] **Step 4: Verify green**

Run the focused worker CLI Codex test again. Expected: pass.

Observed green: focused worker CLI Codex test passed.

### Task 2: Daemon CLI Codex Long-Running Worker

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m15b-codex-long-running-mailbox-worker.md`

- [x] **Step 1: Write failing daemon CLI Codex long-worker test**

Add a test near `test_cli_can_run_file_daemon_with_long_running_mailbox_worker`:

```python
def test_cli_can_run_file_daemon_with_long_running_codex_mailbox_worker(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        output_dir = tmp_path / "run"
        fake_codex = tmp_path / "fake_codex_long_worker.py"
        target_file = "generated/long_worker_codex_delegate.json"
        _init_git_repo(repo)
        backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
        _write_fake_codex(fake_codex, changed_file=target_file)
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
                "--project-root",
                str(repo),
                "--daemon-run-until-idle",
                "--daemon-long-running-mailbox-worker",
                "--runtime",
                "codex",
                "--codex-command",
                sys.executable,
                str(fake_codex),
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        worktree_path = Path(
            summary["snapshot"]["attempts"]["TASK-001-ATTEMPT-001"]["worktree_path"]
        )

        self.assertEqual(completed.stderr, "")
        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
        self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
        self.assertEqual(summary["worker_process"]["worker_runtime"], "codex")
        self.assertEqual(summary["worker_process"]["stderr"], "")
        self.assertTrue((worktree_path / target_file).exists())
        self.assertEqual(
            summary["snapshot"]["runtime_sessions"]["SESSION-TASK-001-ATTEMPT-001"][
                "runtime_adapter"
            ],
            "FileMailboxExternalRuntimeAdapter",
        )
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_can_run_file_daemon_with_long_running_codex_mailbox_worker \
  -v
```

Expected red: the daemon CLI still rejects runtime profile defaults for
`--daemon-long-running-mailbox-worker`.

Observed red: CLI rejected the combination with
`--daemon-long-running-mailbox-worker currently supports only the fake delegate runtime`.

- [x] **Step 3: Wire supervisor runtime profile**

In `mailbox_worker.py`, extend `FileMailboxWorkerProcessSupervisor` with:

```python
runtime="fake"
codex_command=None
codex_model=None
codex_sandbox="workspace-write"
codex_timeout_seconds=300
```

`start()` should pass those settings to `agentteam_runtime.mailbox_worker` using
`--runtime` and `--codex-command-json` when needed. The start summary should
include `worker_runtime`.

In `cli.py`, change the long-running worker branch:

- keep fake as the default when `runtime_profile_defaults` is `None`;
- allow `runtime_profile_defaults["adapter"] == "codex"`;
- reject any other adapter;
- pass Codex command/model/sandbox/timeout settings into the supervisor;
- set `FileMailboxExternalRuntimeAdapter` timeout to at least the delegate
  timeout plus five seconds for Codex runs.

- [x] **Step 4: Verify green**

Run the focused daemon CLI Codex long-worker test. Expected: pass.

Observed green: both focused Codex worker CLI and daemon long-worker Codex tests
passed.

- [x] **Step 5: Update artifact docs**

Update `m0_file_runtime.md`:

- M15b section after M15a;
- worker CLI `--runtime codex` and `--codex-command-json`;
- daemon CLI example combining `--daemon-long-running-mailbox-worker` with
  `--runtime codex`;
- limitation: still one worker process, single sequential dispatch handling,
  no worker restart/backoff, no multi-agent worker pool, no Claude delegate.

Updated `m0_file_runtime.md` with the M15b worker CLI, daemon CLI, data flow,
and limitations.

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
  --output-dir /tmp/agentteam-m15b-long-codex-worker-fake-cli \
  --daemon-run-until-idle \
  --daemon-long-running-mailbox-worker
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

The CLI smoke above uses the fake long worker path because the fake Codex
command is created inside tests. The full unittest suite covers Codex delegate
execution through fake Codex scripts.

Observed pass:

- unittest discover: 74 tests passed;
- artifact lint: passed, 21 JSON files and 1 JSONL file checked;
- long-running worker CLI smoke: daemon idle, sample task processed, worker
  stopped through stop file, `worker_runtime` reported as `fake`;
- JSON/JQ checks: passed;
- compileall: passed;
- git diff check: passed.

- [x] **Step 7: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m15b-codex-long-running-mailbox-worker.md
git commit -m "Add M15b Codex long-running mailbox worker"
git push origin native-runtime-m0
```

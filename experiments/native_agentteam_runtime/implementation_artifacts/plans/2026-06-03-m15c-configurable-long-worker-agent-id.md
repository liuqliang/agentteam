# M15c Configurable Long-Worker Agent Id Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the daemon long-running mailbox worker path target a configured agent id instead of always using `agent-repo-map`.

**Architecture:** M15c keeps one worker process and sequential scheduler execution. The scheduler still selects the ready task's role agent from the agent pool; the new CLI option only chooses which single mailbox worker process to start. If the selected scheduler agent and worker agent id do not match, the existing external adapter will time out, so tests cover the matching configured-agent case.

**Tech Stack:** Python 3.12 standard library, existing file mailbox worker, existing daemon CLI, `unittest`.

---

### Task 1: CLI Worker Agent Id Override

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m15c-configurable-long-worker-agent-id.md`

- [x] **Step 1: Write failing CLI test**

Add a test near the long-running mailbox worker CLI tests:

```python
def test_cli_long_running_mailbox_worker_accepts_agent_id_override(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "custom_agent_pool.json"
        backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
        _write_agent_pool_with_agent_id(agent_pool_path, "agent-custom-map")
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
                "--daemon-long-running-mailbox-worker",
                "--daemon-long-running-worker-agent-id",
                "agent-custom-map",
            ],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        custom_outbox = (
            output_dir
            / "steps"
            / "STEP-0001-TASK-001"
            / "mailboxes"
            / "agent-custom-map"
            / "outbox.jsonl"
        )

        self.assertEqual(summary["daemon_status"], "idle")
        self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
        self.assertEqual(summary["worker_process"]["worker_agent_id"], "agent-custom-map")
        self.assertTrue(custom_outbox.exists())
```

Add helper:

```python
def _write_agent_pool_with_agent_id(path, agent_id):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_long_running_mailbox_worker_accepts_agent_id_override \
  -v
```

Expected red: CLI rejects unknown `--daemon-long-running-worker-agent-id`.

Observed red: CLI rejected `--daemon-long-running-worker-agent-id` as an
unrecognized argument.

- [x] **Step 3: Implement CLI override**

In `cli.py`:

- add `--daemon-long-running-worker-agent-id` with default `agent-repo-map`;
- pass `args.daemon_long_running_worker_agent_id` into
  `FileMailboxWorkerProcessSupervisor`;
- include the chosen id in `worker_process` through the supervisor start/stop
  summaries.

In `mailbox_worker.py`, add `worker_agent_id` to supervisor `start()` and
`stop()` summaries.

- [x] **Step 4: Verify green**

Run the focused CLI override test again. Expected: pass.

Observed green: focused configurable worker agent id CLI test passed.

- [x] **Step 5: Update docs**

Update `m0_file_runtime.md` to document
`--daemon-long-running-worker-agent-id` and change the M15b limitation from
hardcoded `agent-repo-map` to one configured agent id.

Updated `m0_file_runtime.md` with CLI examples, a M15c section, and revised
single configured-agent limitation wording.

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
  --output-dir /tmp/agentteam-m15c-worker-agent-id-cli \
  --daemon-run-until-idle \
  --daemon-long-running-mailbox-worker \
  --daemon-long-running-worker-agent-id agent-repo-map
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Observed pass:

- unittest discover: 75 tests passed;
- artifact lint: passed, 21 JSON files and 1 JSONL file checked;
- long-running worker CLI smoke: daemon idle, sample task processed, worker
  stopped through stop file, `worker_agent_id` reported as `agent-repo-map`;
- JSON/JQ checks: passed;
- compileall: passed;
- git diff check: passed.

- [x] **Step 7: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m15c-configurable-long-worker-agent-id.md
git commit -m "Add M15c configurable long worker agent id"
git push origin native-runtime-m0
```

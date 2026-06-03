# M18 Two-Phase Retry Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded retry and lease-timeout recovery to the M17 two-phase scheduler.

**Architecture:** Extend `TwoPhaseFileScheduler` side-by-side with the blocking scheduler. Reuse `classify_attempt_outcome(...)`, keep root event types compatible with replay, and keep worker process restart out of this milestone.

**Tech Stack:** Python 3.12 standard library, existing JSONL mailbox protocol, existing replay/state-index machinery, `unittest`.

---

### Task 1: Retryable Result Recovery

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing retry test**

Add helper near the other test helpers:

```python
def _append_runtime_result(outbox_path, source_message_id, task_id, attempt_id, lease_id, result_status, changed_files):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-repo-map",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": {"test": "m18"},
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")
```

Add test:

```python
def test_two_phase_scheduler_retries_retryable_failed_result(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/"]),
            ],
        )
        _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
        scheduler = TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=FixedClock(),
            max_attempts=2,
        )

        first_dispatch = scheduler.dispatch_ready()
        first_inflight = scheduler.state["inflight_attempts"][0]
        _append_runtime_result(
            first_inflight["outbox_path"],
            first_inflight["message_id"],
            first_inflight["task_id"],
            first_inflight["attempt_id"],
            first_inflight["lease_id"],
            "failed",
            [],
        )

        first_collect = scheduler.collect_ready_results()
        second_dispatch = scheduler.dispatch_ready()
        second_inflight = scheduler.state["inflight_attempts"][0]
        _append_runtime_result(
            second_inflight["outbox_path"],
            second_inflight["message_id"],
            second_inflight["task_id"],
            second_inflight["attempt_id"],
            second_inflight["lease_id"],
            "completed",
            ["generated/retry.json"],
        )
        second_collect = scheduler.collect_ready_results()
        events = [
            json.loads(line)
            for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        state = read_scheduler_state_index(output_dir)

        self.assertEqual(first_dispatch["dispatched_task_ids"], ["TASK-001"])
        self.assertEqual(first_collect["collected_task_ids"], ["TASK-001"])
        self.assertEqual(second_dispatch["dispatched_task_ids"], ["TASK-001"])
        self.assertEqual(second_inflight["attempt_id"], "TASK-001-ATTEMPT-002")
        self.assertEqual(second_collect["collected_task_ids"], ["TASK-001"])
        self.assertIn("recovery_routed", {event["event_type"] for event in events})
        self.assertEqual(
            [step["step_status"] for step in scheduler.state["steps"]],
            ["retry_routed", "processed"],
        )
        self.assertEqual(
            {task["task_id"]: task["task_status"] for task in state["tasks"]},
            {"TASK-001": "done"},
        )
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_two_phase_scheduler_retries_retryable_failed_result \
  -v
```

Expected red: constructor rejects `max_attempts` or failed result blocks the
task instead of routing `recovery_routed`.

Observed red: `TwoPhaseFileScheduler.__init__()` rejected the unexpected
`max_attempts` keyword.

- [x] **Step 3: Implement retry classification**

In `two_phase_scheduler.py`:

- import `classify_attempt_outcome`;
- add `max_attempts=1` to `TwoPhaseFileScheduler.__init__`;
- validate `max_attempts >= 1`;
- store `max_attempts` in new state files and normalize old state files;
- compute attempt number per task from `steps` and `inflight_attempts`;
- generate attempt, lease, message, worktree, and runtime session ids from that
  attempt number;
- use `classify_attempt_outcome(runtime_result, task, diff_audit=None)`;
- for retryable rejected attempts with remaining attempts, append
  `recovery_routed`, set step status to `retry_routed`, leave the task `ready`,
  and remove the attempt from inflight;
- for accepted attempts, mark task `done`;
- for exhausted or non-retryable rejected attempts, mark task `blocked`.

- [x] **Step 4: Verify green**

Run the focused retry test again. Expected: pass.

Observed green: focused retryable failed result test passed.

### Task 2: Lease Timeout Recovery

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing timeout test**

Add test:

```python
def test_two_phase_scheduler_collects_expired_inflight_as_timeout(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        agent_pool_path = tmp_path / "agent_pool.json"
        backlog_path = _write_backlog(
            tmp_path,
            write_scope=["generated/"],
            tasks=[
                _backlog_task("TASK-001", write_scope=["generated/"]),
            ],
        )
        _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
        scheduler = TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=FixedClock(),
            lease_timeout_seconds=0,
        )

        scheduler.dispatch_ready()
        collected = scheduler.collect_ready_results()
        state = read_scheduler_state_index(output_dir)

        self.assertEqual(collected["collect_status"], "collected")
        self.assertEqual(collected["collected_task_ids"], ["TASK-001"])
        self.assertEqual(scheduler.summary()["inflight_count"], 0)
        self.assertEqual(scheduler.state["steps"][0]["failure_category"], "timeout")
        self.assertTrue(scheduler.state["steps"][0]["result"]["retryable"])
        self.assertEqual(
            {task["task_id"]: task["task_status"] for task in state["tasks"]},
            {"TASK-001": "running"},
        )
        self.assertEqual(
            scheduler.state["backlog"]["items"][0]["blockers"],
            ["timeout"],
        )
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_two_phase_scheduler_collects_expired_inflight_as_timeout \
  -v
```

Expected red: constructor rejects `lease_timeout_seconds` or collect leaves the
attempt inflight.

Observed red: `TwoPhaseFileScheduler.__init__()` rejected the unexpected
`lease_timeout_seconds` keyword.

- [x] **Step 3: Implement timeout synthesis**

In `two_phase_scheduler.py`:

- add `lease_timeout_seconds=900` to `TwoPhaseFileScheduler.__init__`;
- validate `lease_timeout_seconds >= 0`;
- compute `lease_expires_at` from dispatch `created_at`;
- store `lease_expires_at` in the dispatch message and inflight attempt;
- in `collect_ready_results()`, synthesize a runtime result with
  `result_status: "timed_out"` when no outbox result exists and the lease has
  expired;
- include timeout metadata in `runtime_output_received.payload.output`.

- [x] **Step 4: Verify green**

Run the focused timeout test again. Expected: pass.

Observed green: focused lease-timeout collection test passed. Combined focused
two-phase regression run passed 5 tests.

### Task 3: CLI Options And Docs

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m18-two-phase-retry-timeout.md`

- [x] **Step 1: Write failing CLI parser test**

Extend `test_cli_can_run_two_phase_scheduler_with_static_worker_pool` command
with:

```python
"--max-attempts",
"2",
"--lease-timeout-seconds",
"900",
```

Add assertions:

```python
self.assertEqual(summary["max_attempts"], 2)
self.assertEqual(summary["lease_timeout_seconds"], 900)
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_cli_can_run_two_phase_scheduler_with_static_worker_pool \
  -v
```

Expected red: CLI rejects `--max-attempts` and `--lease-timeout-seconds`.

Observed red: CLI exited 2 and reported unrecognized `--max-attempts` and
`--lease-timeout-seconds` arguments.

- [x] **Step 3: Wire CLI options**

In `cli.py`:

- add `--max-attempts`, default `1`;
- add `--lease-timeout-seconds`, default `900`;
- validate `--max-attempts >= 1`;
- validate `--lease-timeout-seconds >= 0`;
- pass both options to `run_two_phase_scheduler_loop(...)`;
- include both values in the scheduler summary.

Update `m0_file_runtime.md` with M18 API, CLI usage, retry flow, timeout flow,
and limitations.

- [x] **Step 4: Verify green**

Run the focused CLI test again. Expected: pass.

Observed green: focused two-phase worker-pool CLI test passed with
`--max-attempts 2` and `--lease-timeout-seconds 900`.

### Task 4: Full Verification And Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m18-two-phase-retry-timeout.md`

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
  --output-dir /tmp/agentteam-m18-two-phase-retry-timeout-cli \
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
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m18-two-phase-retry-timeout.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m18-two-phase-retry-timeout.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md
```

- [x] **Step 2: Record observed verification**

Update this plan with exact observed pass or failure lines from the commands.

Observed pass:

- full unit test: `Ran 82 tests in 3.613s`, `OK`;
- artifact lint: `status: passed`, checked 21 JSON files and 1 JSONL file;
- two-phase worker-pool CLI smoke: `daemon_status: idle`,
  `scheduler_status: idle`, `max_attempts: 2`,
  `lease_timeout_seconds: 900`, `processed_task_ids: ["TASK-001"]`;
- JSON validation: `find ... -name '*.json' -exec jq empty {} +` exited 0;
- sample JSONL validation: `jq -c . sample_events.jsonl` exited 0;
- bytecode compilation: `python3 -m compileall -q ...` exited 0;
- whitespace check: `git diff --check` exited 0;
- placeholder check: `rg` found no matches in the M18 design, M18 plan, or
  `m0_file_runtime.md`.

- [x] **Step 3: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m18-two-phase-retry-timeout.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m18-two-phase-retry-timeout.md
git commit -m "Add M18 two-phase retry timeout recovery"
git push origin native-runtime-m0
```

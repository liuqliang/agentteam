# M12a Live Codex Scheduler Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a gated live Codex smoke path that exercises the scheduler loop, canonical event log, SQLite state index, and runtime session tracking through `CodexRuntimeAdapter`.

**Architecture:** Keep live model usage opt-in through `AGENTTEAM_RUN_LIVE_CODEX=1`. Add a new smoke module that creates a temporary git repo and one-task backlog, runs `run_scheduler_loop(...)` with `CodexRuntimeAdapter`, then verifies the expected generated file, scheduler summary, state index, and runtime session row. Unit tests use a fake Codex command so committed verification does not spend live model calls.

**Tech Stack:** Python 3.12 standard library, `codex exec`, git worktrees, JSONL, SQLite state index, `unittest`.

---

### Task 1: Scheduler Smoke Entry Point

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_scheduler_smoke.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`

- [x] **Step 1: Write failing skip test**

Add a test that runs:

```bash
python3 -m agentteam_runtime.live_codex_scheduler_smoke --output-dir <tmp>
```

without `AGENTTEAM_RUN_LIVE_CODEX=1`. Assert:

```python
self.assertEqual(summary["status"], "skipped")
self.assertEqual(summary["reason"], "set AGENTTEAM_RUN_LIVE_CODEX=1")
self.assertFalse(output_dir.exists())
```

- [x] **Step 2: Write failing fake-Codex scheduler test**

Add a fake Codex command test that sets `AGENTTEAM_RUN_LIVE_CODEX=1`, passes `--codex-command python3 <fake>`, and asserts:

```python
self.assertEqual(summary["status"], "completed")
self.assertEqual(summary["scheduler_status"], "idle")
self.assertEqual(summary["processed_task_ids"], ["TASK-LIVE-CODEX-SCHEDULER-SMOKE"])
self.assertEqual(summary["state_index"]["tasks"][0]["task_status"], "done")
self.assertEqual(summary["state_index"]["runtime_sessions"][0]["session_status"], "stopped")
```

- [x] **Step 3: Verify red**

Run the two focused tests. Expected: fail because `agentteam_runtime.live_codex_scheduler_smoke` does not exist.

Observed red:

```text
python3 -m agentteam_runtime.live_codex_scheduler_smoke ... returned non-zero exit status 1
```

- [x] **Step 4: Implement scheduler smoke module**

Implement a module parallel to `live_codex_smoke.py`:

- gate on `AGENTTEAM_RUN_LIVE_CODEX`;
- create temp repo, fixtures, and run dir;
- call `run_scheduler_loop(...)` with `CodexRuntimeAdapter`;
- call `read_scheduler_state_index(run_dir)`;
- return JSON with `status`, `scheduler_status`, `processed_task_ids`, `state_index`, `events_path`, `state_db_path`, `worktree_path`, and `expected_file_exists`.

- [x] **Step 5: Verify green**

Run the two focused tests. Expected: pass.

Observed green:

```text
test_live_codex_scheduler_smoke_skips_without_env_gate ... ok
test_live_codex_scheduler_smoke_runs_with_fake_codex_command ... ok
```

### Task 2: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m12a-live-codex-scheduler-smoke.md`

- [x] **Step 1: Document M12a**

Document:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_scheduler_smoke \
  --output-dir /tmp/agentteam-live-codex-scheduler-smoke
```

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m12a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_scheduler_smoke --output-dir /tmp/agentteam-live-codex-scheduler-skip-m12a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m12a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 53 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_scheduler_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

- [x] **Step 3: Try real Codex scheduler smoke**

After committed verification passes, try:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_scheduler_smoke \
  --output-dir /tmp/agentteam-live-codex-scheduler-real-m12a \
  --timeout-seconds 300
```

If sandbox/network restrictions block it, rerun with approval using escalated permissions.

Observed real Codex smoke on 2026-06-02 with `codex-cli 0.132.0`:

```text
status: completed
scheduler_status: idle
processed_task_ids: ["TASK-LIVE-CODEX-SCHEDULER-SMOKE"]
changed_files: ["generated/live_codex_scheduler_smoke.json"]
expected_file_exists: true
state_index.tasks[0].task_status: done
state_index.runtime_sessions[0].runtime_adapter: CodexRuntimeAdapter
state_index.runtime_sessions[0].session_status: stopped
```

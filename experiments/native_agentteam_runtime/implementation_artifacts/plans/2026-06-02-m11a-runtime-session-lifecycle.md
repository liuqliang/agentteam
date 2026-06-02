# M11a Runtime Session Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic runtime session lifecycle tracking around each runtime adapter invocation.

**Architecture:** Keep `run_simulation(...)` as a synchronous single-attempt execution path, but wrap each adapter call in logical session events. The session lifecycle is recorded in authoritative JSONL events and replay state; it does not start a daemon, add concurrency, or change the adapter contract.

**Tech Stack:** Python 3.12 standard library, JSONL event log, `unittest`.

---

### Task 1: Runtime Session Events

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing lifecycle test**

Add a test that runs a single simulation and asserts:

```python
self.assertEqual(result["runtime_session_id"], "SESSION-ATTEMPT-001")
self.assertEqual(result["runtime_session_status"], "stopped")
event_types = [event["event_type"] for event in events]
self.assertEqual(
    [
        event_type
        for event_type in event_types
        if event_type.startswith("runtime_session_")
    ],
    [
        "runtime_session_started",
        "runtime_session_observed",
        "runtime_session_stopped",
    ],
)
```

Assert all three session events carry `runtime_session_id`, `task_id`, `attempt_id`, and `lease_id`.

- [x] **Step 2: Verify red**

Run the focused lifecycle test. Expected: fail because the result lacks `runtime_session_id` and the event log lacks lifecycle events.

Observed red:

```text
KeyError: 'runtime_session_id'
```

- [x] **Step 3: Implement lifecycle events**

Inside `run_simulation(...)`, derive:

```python
runtime_session_id = f"SESSION-{attempt_id}"
```

Append `runtime_session_started` before `runtime_adapter.run(...)`, append `runtime_session_observed` after the adapter returns, and append `runtime_session_stopped` before validation. Add `runtime_session_id` and `runtime_session_status` to `final_attempt` and the top-level returned summary.

- [x] **Step 4: Verify green**

Run the focused lifecycle test. Expected: pass.

Observed intermediate failure after lifecycle events were added:

```text
KeyError: 'runtime_sessions'
```

### Task 2: Replay And Schema Support

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`

- [x] **Step 1: Write failing replay test**

Extend the lifecycle test or add a separate replay assertion:

```python
snapshot = replay_events(result["events_path"])
self.assertEqual(
    snapshot["runtime_sessions"]["SESSION-ATTEMPT-001"]["session_status"],
    "stopped",
)
self.assertEqual(
    snapshot["runtime_sessions"]["SESSION-ATTEMPT-001"]["result_status"],
    "completed",
)
```

- [x] **Step 2: Verify red**

Run the focused replay assertion. Expected: fail because replay does not yet track `runtime_sessions`.

- [x] **Step 3: Implement replay support**

Initialize replay snapshots as:

```python
snapshot = {"tasks": {}, "attempts": {}, "leases": {}, "runtime_sessions": {}}
```

Handle:

```python
runtime_session_started
runtime_session_observed
runtime_session_stopped
```

Update `event.schema.json` enum with `runtime_session_observed` and `runtime_session_stopped`.

- [x] **Step 4: Verify green**

Run the focused lifecycle/replay test and the event schema test. Expected: pass.

Observed green:

```text
test_run_simulation_records_runtime_session_lifecycle ... ok
test_emitted_types_are_allowed_by_schemas ... ok
test_artifact_lint_passes_native_runtime_tree ... ok
```

### Task 3: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m11a-runtime-session-lifecycle.md`

- [x] **Step 1: Document M11a**

Document that M11a records logical runtime sessions around synchronous adapter calls. Make clear this is not yet a long-lived worker process.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m11a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m11a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 48 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

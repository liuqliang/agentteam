# M11b Runtime Session State Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add runtime session rows to the SQLite scheduler state index and state-index query output.

**Architecture:** Keep JSONL events authoritative and continue rebuilding SQLite from `replay_events(...)`. Add a `runtime_sessions` table populated from replay state, and make stale detection rebuild older SQLite indexes that do not have the new table.

**Tech Stack:** Python 3.12 standard library, `sqlite3`, JSONL replay, `unittest`.

---

### Task 1: Runtime Sessions In SQLite Index

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing SQLite runtime session test**

Extend `test_scheduler_loop_writes_sqlite_state_index` to query:

```sql
select runtime_session_id, task_id, attempt_id, session_status, result_status
from runtime_sessions
order by runtime_session_id;
```

Assert:

```python
self.assertEqual(
    runtime_sessions,
    [
        ("SESSION-TASK-001-ATTEMPT-001", "TASK-001", "TASK-001-ATTEMPT-001", "stopped", "completed"),
        ("SESSION-TASK-002-ATTEMPT-001", "TASK-002", "TASK-002-ATTEMPT-001", "stopped", "completed"),
    ],
)
```

- [x] **Step 2: Verify red**

Run the focused SQLite index test. Expected: fail because the `runtime_sessions` table does not exist.

Observed red:

```text
sqlite3.OperationalError: no such table: runtime_sessions
```

- [x] **Step 3: Implement runtime_sessions table**

Add a SQLite table:

```sql
runtime_sessions(
  runtime_session_id text primary key,
  task_id text,
  attempt_id text,
  lease_id text,
  session_status text,
  result_status text,
  runtime_adapter text,
  changed_file_count integer
)
```

Populate it from `snapshot["runtime_sessions"]`.

- [x] **Step 4: Verify green**

Run the focused SQLite index test. Expected: pass.

Observed green:

```text
test_scheduler_loop_writes_sqlite_state_index ... ok
```

### Task 2: State Query Runtime Sessions

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing state-query test**

Extend `test_read_scheduler_state_index_returns_query_summary` to assert:

```python
self.assertEqual(
    state["runtime_sessions"],
    [
        {
            "runtime_session_id": "SESSION-TASK-001-ATTEMPT-001",
            "task_id": "TASK-001",
            "attempt_id": "TASK-001-ATTEMPT-001",
            "lease_id": "TASK-001-LEASE-001",
            "session_status": "stopped",
            "result_status": "completed",
            "runtime_adapter": "FakeRuntimeAdapter",
            "changed_file_count": 1,
        },
        ...
    ],
)
```

- [x] **Step 2: Verify red**

Run the focused state-query test. Expected: fail because `read_sqlite_state_index(...)` does not return `runtime_sessions`.

Observed red:

```text
KeyError: 'runtime_sessions'
```

- [x] **Step 3: Implement query output and schema freshness**

Add a `runtime_sessions` query to `read_sqlite_state_index(...)`. Update `_sqlite_state_index_is_stale(...)` so it returns true when required tables are missing:

```python
{"tasks", "attempts", "leases", "runtime_sessions", "events"}
```

- [x] **Step 4: Verify green**

Run the focused state-query test. Expected: pass.

Observed green:

```text
test_read_scheduler_state_index_returns_query_summary ... ok
test_read_scheduler_state_index_rebuilds_missing_runtime_session_table ... ok
```

### Task 3: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m11b-runtime-session-state-index.md`

- [x] **Step 1: Document M11b**

Document the `runtime_sessions(...)` table and state-index query output.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m11b
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m11b-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 49 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

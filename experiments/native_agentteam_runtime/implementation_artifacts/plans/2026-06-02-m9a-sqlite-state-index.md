# M9a SQLite State Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a SQLite query index for scheduler-loop state while keeping JSONL
events as the authoritative audit log.

**Architecture:** Keep `<output-dir>/events.jsonl` as the source of truth.
After each scheduler-loop step, replay the canonical event log and write a
compact SQLite index at `<output-dir>/state/scheduler_state.sqlite`. The SQLite
database is rebuildable from JSONL and is used for fast queries over tasks,
attempts, leases, and events.

**Tech Stack:** Python 3.12 standard library, `sqlite3`, JSONL files, `unittest`.

---

### Task 1: SQLite Index From Canonical Events

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing SQLite index test**

Add a test that runs a two-task scheduler loop, then asserts:

```python
db_path = Path(summary["state_db_path"])
self.assertTrue(db_path.exists())
```

Query the database:

```sql
select task_id, task_status from tasks order by task_id;
select lease_id, lease_status from leases order by lease_id;
select count(*) from events;
```

Assert:

```python
self.assertEqual(tasks, [("TASK-001", "done"), ("TASK-002", "done")])
self.assertEqual(
    leases,
    [("TASK-001-LEASE-001", "released"), ("TASK-002-LEASE-001", "released")],
)
self.assertEqual(event_count, root_event_count)
```

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail because `summary["state_db_path"]` is not
returned and no SQLite index exists.

Observed red:

```text
KeyError: 'state_db_path'
```

The test was then strengthened to require `attempts.task_id`, which produced
the expected red failure while the index existed but the attempt-to-task
relationship was missing.

- [x] **Step 3: Implement SQLite index helper**

Add `sqlite3` import and helper:

```python
def rebuild_sqlite_state_index(db_path, events_path):
    snapshot = replay_events(events_path)
    ...
```

Create tables:

```sql
tasks(task_id text primary key, task_status text not null)
attempts(attempt_id text primary key, task_id text, attempt_status text, validation_status text)
leases(lease_id text primary key, task_id text, attempt_id text, lease_status text)
events(sequence integer primary key, event_id text, event_type text, task_id text, attempt_id text, lease_id text, step_id text, time text)
```

Populate from `replay_events(events_path)` and `_read_jsonl(events_path)`.

- [x] **Step 4: Wire scheduler loop**

Add:

```python
self.state_db_path = self.output_dir / "state" / "scheduler_state.sqlite"
```

After canonical events are appended, call:

```python
rebuild_sqlite_state_index(self.state_db_path, self.events_path)
```

Return `state_db_path` in scheduler summaries.

- [x] **Step 5: Verify green**

Run the focused SQLite test and scheduler focused tests. Expected: pass.

Observed green:

```text
test_scheduler_loop_writes_sqlite_state_index ... ok
```

### Task 2: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m9a-sqlite-state-index.md`

- [x] **Step 1: Document M9a**

Document that SQLite is a rebuildable index, not the authority. JSONL remains
the audit log.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m9a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m9a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 40 tests ... OK
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

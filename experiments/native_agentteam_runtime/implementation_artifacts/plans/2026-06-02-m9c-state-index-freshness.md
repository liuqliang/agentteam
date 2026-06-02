# M9c State Index Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make scheduler state-index reads self-repair when SQLite is stale relative to the canonical JSONL event log.

**Architecture:** Keep `<output-dir>/events.jsonl` authoritative. Before reading the SQLite query index, compare the number of indexed events with the number of canonical JSONL events; if the counts differ, rebuild SQLite from JSONL and then read the query summary.

**Tech Stack:** Python 3.12 standard library, `sqlite3`, JSONL files, `unittest`.

---

### Task 1: Stale SQLite Detection And Rebuild

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing freshness test**

Add a test that runs a two-task scheduler loop, manually makes SQLite stale, then calls `read_scheduler_state_index(output_dir)`:

```python
with sqlite3.connect(summary["state_db_path"]) as connection:
    connection.execute("delete from tasks where task_id = ?", ("TASK-002",))
    connection.execute("delete from events where sequence = (select max(sequence) from events)")

state = read_scheduler_state_index(output_dir)
self.assertEqual(
    state["tasks"],
    [
        {"task_id": "TASK-001", "task_status": "done"},
        {"task_id": "TASK-002", "task_status": "done"},
    ],
)
self.assertEqual(state["event_count"], root_event_count)
```

- [x] **Step 2: Verify red**

Run the focused freshness test. Expected: fail because the current read path trusts an existing SQLite file even when it is stale.

Observed red:

```text
test_read_scheduler_state_index_rebuilds_stale_sqlite_index ... FAIL
TASK-002 missing from state["tasks"]
```

- [x] **Step 3: Implement freshness check**

Add:

```python
def _sqlite_state_index_is_stale(db_path, events_path):
    try:
        indexed_event_count = _sqlite_event_count(db_path)
    except sqlite3.DatabaseError:
        return True
    canonical_event_count = sum(1 for _ in _read_jsonl(events_path))
    return indexed_event_count != canonical_event_count
```

Update `read_scheduler_state_index(output_dir)` so it rebuilds when:

```python
not db_path.exists()
or (events_path.exists() and _sqlite_state_index_is_stale(db_path, events_path))
```

- [x] **Step 4: Verify green**

Run the focused freshness test. Expected: pass.

Observed green:

```text
test_read_scheduler_state_index_rebuilds_stale_sqlite_index ... ok
```

### Task 2: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m9c-state-index-freshness.md`

- [x] **Step 1: Document M9c**

Document that state-index reads repair missing or stale SQLite indexes from the canonical root JSONL log.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m9c
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m9c-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 43 tests ... OK
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

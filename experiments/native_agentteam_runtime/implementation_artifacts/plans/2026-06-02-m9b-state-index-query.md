# M9b State Index Query Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a minimal read-only query path for the scheduler SQLite state index.

**Architecture:** Keep `<output-dir>/events.jsonl` as the authoritative audit log and keep SQLite as a rebuildable query index. Add a small API that reads the SQLite tables into a JSON-serializable summary, plus a CLI mode that can inspect an existing scheduler run without requiring `--agent-pool` or `--backlog`.

**Tech Stack:** Python 3.12 standard library, `sqlite3`, JSONL files, `argparse`, `unittest`.

---

### Task 1: Read-Only State Index API

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing API test**

Add `read_scheduler_state_index` to the package import list in the test file and add a test that:

```python
summary = run_scheduler_loop(...)
state = read_scheduler_state_index(output_dir)
self.assertEqual(state["state_db_path"], summary["state_db_path"])
self.assertEqual(
    state["tasks"],
    [
        {"task_id": "TASK-001", "task_status": "done"},
        {"task_id": "TASK-002", "task_status": "done"},
    ],
)
self.assertEqual(state["event_count"], root_event_count)
self.assertEqual(state["latest_event"]["event_type"], "backlog_updated")
```

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail because `read_scheduler_state_index` is not exported or implemented.

Observed red:

```text
ImportError: cannot import name 'read_scheduler_state_index' from 'agentteam_runtime'
```

- [x] **Step 3: Implement query helpers**

Add:

```python
def read_scheduler_state_index(output_dir):
    output_dir = Path(output_dir)
    db_path = output_dir / "state" / "scheduler_state.sqlite"
    events_path = output_dir / "events.jsonl"
    if not db_path.exists():
        if not events_path.exists():
            raise FileNotFoundError(f"missing scheduler state index: {db_path}")
        rebuild_sqlite_state_index(db_path, events_path)
    return read_sqlite_state_index(db_path, events_path=events_path)
```

Add `read_sqlite_state_index(db_path, events_path=None)` that returns:

```json
{
  "state_db_path": "<path>",
  "events_path": "<path-or-null>",
  "tasks": [],
  "attempts": [],
  "leases": [],
  "event_count": 0,
  "latest_event": null
}
```

- [x] **Step 4: Verify green**

Run the focused API test. Expected: pass.

Observed green:

```text
test_read_scheduler_state_index_returns_query_summary ... ok
```

### Task 2: CLI State Index Inspection

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI test**

Add a test that runs a two-task scheduler loop, deletes `scheduler_state.sqlite`, then invokes:

```bash
python3 -m agentteam_runtime.cli --output-dir <run-dir> --show-state-index
```

Assert:

```python
self.assertEqual(summary["event_count"], root_event_count)
self.assertEqual(summary["tasks"][0]["task_id"], "TASK-001")
self.assertTrue((output_dir / "state" / "scheduler_state.sqlite").exists())
```

- [x] **Step 2: Verify red**

Run the focused CLI test. Expected: argparse failure because `--agent-pool` and `--backlog` are still required and `--show-state-index` does not exist.

Observed red was covered by the same focused test run before the API existed:

```text
ImportError: cannot import name 'read_scheduler_state_index' from 'agentteam_runtime'
```

- [x] **Step 3: Implement CLI mode**

Add `--show-state-index`. Make `--agent-pool` and `--backlog` conditionally required only for execution modes. In state-inspection mode, call:

```python
summary = read_scheduler_state_index(args.output_dir)
print(json.dumps(summary, sort_keys=True))
return
```

- [x] **Step 4: Verify green**

Run the focused CLI test. Expected: pass.

Observed green:

```text
test_cli_can_show_state_index_without_agent_pool_or_backlog ... ok
```

### Task 3: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m9b-state-index-query.md`

- [x] **Step 1: Document M9b**

Document that `--show-state-index` is an observability path over the SQLite index and that missing SQLite can be rebuilt from the canonical JSONL log.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m9b
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m9b-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 42 tests ... OK
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

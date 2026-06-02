# M8b Task-Scoped Lease And Message IDs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent scheduler-loop canonical replay from collapsing leases or
messages across steps that all use local `LEASE-001` and `MSG-0001` ids.

**Architecture:** Preserve default single-task `run_simulation(...)` ids. When
`FileScheduler` passes a task id prefix into `run_simulation(...)`, scope
attempt, lease, and message ids by that task id. This keeps canonical replay
keys unique without changing the per-step runtime event model.

**Tech Stack:** Python 3.12 standard library, JSONL files, `unittest`.

---

### Task 1: Scope Scheduler Loop Lease And Message IDs

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing replay test**

Add a test that runs a two-task scheduler loop, replays `summary["events_path"]`,
and asserts:

```python
self.assertEqual(
    set(snapshot["leases"].keys()),
    {"TASK-001-LEASE-001", "TASK-002-LEASE-001"},
)
```

Also read the first message in each step mailbox and assert:

```python
self.assertEqual(message["message_id"], "TASK-001-MSG-0001")
self.assertEqual(message["payload"]["lease_id"], "TASK-001-LEASE-001")
```

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail because both steps currently use
`LEASE-001` and `MSG-0001`.

- [x] **Step 3: Implement scoped ids**

Add helper:

```python
def _scoped_id(kind, number, id_prefix=None, width=3):
    local_id = f"{kind}-{number:0{width}d}"
    if not id_prefix:
        return local_id
    return f"{safe_prefix}-{local_id}"
```

Use it for:

```python
attempt_id = _scoped_id("ATTEMPT", attempt_number, attempt_id_prefix)
lease_id = _scoped_id("LEASE", attempt_number, attempt_id_prefix)
message_id = _scoped_id("MSG", attempt_number, attempt_id_prefix, width=4)
```

Keep non-scheduler `run_simulation(...)` output unchanged.

- [x] **Step 4: Verify green**

Run focused replay test and existing scheduler tests. Expected: pass.

### Task 2: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m8b-task-scoped-lease-message-ids.md`

- [x] **Step 1: Document M8b**

Document that scheduler-loop ids are task-scoped for attempt, worktree, lease,
and message ids. Plain `run_simulation(...)` remains unchanged.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m8b
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m8b-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- replay test first failed because two scheduler steps collapsed into one
  `LEASE-001` key;
- focused test passed after scoping scheduler-loop lease and message ids by
  task id;
- scheduler/default-id focused tests ran 5 tests with `OK`;
- unit test discovery ran 39 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression kept default `LEASE-001` and `MSG-0001` ids;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

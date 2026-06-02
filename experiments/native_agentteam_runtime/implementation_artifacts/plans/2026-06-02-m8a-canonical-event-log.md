# M8a Canonical Event Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a root scheduler event log that canonicalizes per-step events so
multi-task runs can be replayed from one file.

**Architecture:** Keep each step's existing `events.jsonl` unchanged. After a
processed scheduler step, copy that step's events into `<output-dir>/events.jsonl`
with new global `sequence` and `event_id` values, plus optional metadata
`run_id`, `step_id`, and `source_event_id`. The root canonical event log becomes
the preferred replay input for scheduler loop runs.

**Tech Stack:** Python 3.12 standard library, JSONL files, `unittest`.

---

### Task 1: Canonical Event Append

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`

- [x] **Step 1: Write failing canonical event test**

Add a test that runs a two-task scheduler loop and asserts:

```python
self.assertEqual(summary["events_path"], str(output_dir / "events.jsonl"))
self.assertTrue((output_dir / "events.jsonl").exists())
```

Read root events and assert:

```python
self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
self.assertEqual(events[0]["event_id"], "EVT-0001")
self.assertEqual({event["step_id"] for event in events}, {"STEP-0001-TASK-001", "STEP-0002-TASK-002"})
self.assertTrue(all(event["source_event_id"].startswith("EVT-") for event in events))
```

Replay the root event log:

```python
snapshot = replay_events(summary["events_path"])
self.assertEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
self.assertEqual(snapshot["tasks"]["TASK-002"]["task_status"], "done")
```

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail because `summary["events_path"]` is not
returned and root `events.jsonl` is not written by the scheduler loop.

- [x] **Step 3: Implement canonical append**

Add to `FileScheduler`:

```python
self.events_path = self.output_dir / "events.jsonl"
```

After each processed step:

```python
self._append_step_events_to_canonical_log(step_id, result["events_path"])
```

Implementation rules:

- read the step events with `_read_jsonl`;
- compute the next global sequence from root `events.jsonl` if it already exists;
- rewrite `event_id` to `EVT-<global sequence>`;
- rewrite `sequence` to the global sequence;
- keep the original payload unchanged;
- add optional top-level metadata: `run_id`, `step_id`, `source_event_id`,
  `source_event_sequence`;
- append to the root canonical log.

- [x] **Step 4: Verify green**

Run the focused test and existing scheduler tests. Expected: pass.

### Task 2: Schema, CLI, And Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m8a-canonical-event-log.md`

- [x] **Step 1: Update schema**

Allow optional top-level fields:

```json
"run_id": {"type": ["string", "null"]},
"step_id": {"type": ["string", "null"]},
"source_event_id": {"type": ["string", "null"]},
"source_event_sequence": {"type": ["integer", "null"]}
```

- [x] **Step 2: Document M8a**

Document that root `events.jsonl` is the canonical replay source for scheduler
loops. Step logs remain local detail logs.

- [x] **Step 3: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m8a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m8a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- canonical event test first failed because scheduler loop summaries did not
  return `events_path`;
- schema metadata assertion then failed because the event schema did not allow
  `run_id`, `step_id`, `source_event_id`, and `source_event_sequence`;
- focused canonical/scheduler tests ran 5 tests with `OK`;
- unit test discovery ran 38 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression kept the single-task summary plus replay snapshot;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

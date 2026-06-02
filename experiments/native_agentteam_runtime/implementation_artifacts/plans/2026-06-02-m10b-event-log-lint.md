# M10b Event Log Lint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen artifact lint so event JSONL files are checked for required event fields and monotonically increasing sequence numbers.

**Architecture:** Keep `agentteam_runtime.artifact_lint` as a lightweight standard-library lint command. When a JSONL record looks like an event record or the file name is `events.jsonl`, verify required event keys and ensure sequence numbers are strictly increasing by 1 within that file.

**Tech Stack:** Python 3.12 standard library, JSONL parsing, `unittest`.

---

### Task 1: Required Event Fields

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/artifact_lint.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing required-field test**

Add a test that writes `events.jsonl` with:

```python
{"event_type": "scheduler_started", "sequence": 1}
```

Call `lint_artifacts(tmp_path)` and assert:

```python
self.assertEqual(summary["status"], "failed")
self.assertEqual(summary["errors"][0]["kind"], "missing_event_fields")
self.assertIn("event_id", summary["errors"][0]["missing_fields"])
```

- [x] **Step 2: Verify red**

Run the focused required-field test. Expected: fail because current lint does not check required event fields.

Observed red:

```text
test_artifact_lint_reports_missing_event_fields ... FAIL
AssertionError: 'passed' != 'failed'
```

- [x] **Step 3: Implement required-field lint**

Add:

```python
EVENT_REQUIRED_FIELDS = {
    "event_id",
    "sequence",
    "time",
    "event_type",
    "actor",
    "idempotency_key",
    "correlation_id",
    "payload",
}
```

For each event record, append:

```json
{"kind": "missing_event_fields", "path": "...", "line": 1, "missing_fields": ["..."]}
```

- [x] **Step 4: Verify green**

Run the focused required-field test. Expected: pass.

Observed green:

```text
test_artifact_lint_reports_missing_event_fields ... ok
```

### Task 2: Event Sequence Lint

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/artifact_lint.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing sequence test**

Add a test that writes two valid event records with sequences `1` and `3`. Assert:

```python
self.assertEqual(summary["errors"][0]["kind"], "non_monotonic_event_sequence")
self.assertEqual(summary["errors"][0]["expected_sequence"], 2)
self.assertEqual(summary["errors"][0]["actual_sequence"], 3)
```

- [x] **Step 2: Verify red**

Run the focused sequence test. Expected: fail because current lint does not check event sequence continuity.

Observed red:

```text
test_artifact_lint_reports_non_monotonic_event_sequence ... FAIL
AssertionError: 'passed' != 'failed'
```

- [x] **Step 3: Implement sequence lint**

Track expected sequence per JSONL file. For event records with integer `sequence`, require the first sequence to be `1` and every next sequence to increment by 1.

- [x] **Step 4: Verify green**

Run the focused sequence test. Expected: pass.

Observed green:

```text
test_artifact_lint_reports_non_monotonic_event_sequence ... ok
```

### Task 3: Documentation And Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m10b-event-log-lint.md`

- [x] **Step 1: Document M10b**

Document that artifact lint now checks event required fields and sequence continuity.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m10b
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m10b-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 51 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

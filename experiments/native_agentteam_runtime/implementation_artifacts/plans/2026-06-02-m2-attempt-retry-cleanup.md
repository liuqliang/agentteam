# M2 Attempt Retry and Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the native runtime simulation from a single opaque attempt into
a managed execution attempt path with outcome classification, bounded retry, and
optional accepted-worktree cleanup.

**Architecture:** Keep `run_simulation` as the scheduler facade and preserve
the M0/M1 default one-attempt behavior. Add a small classification function that
turns runtime output into validation, failure category, and retryability. Use a
loop inside `run_simulation` only when `max_attempts > 1`, emit `recovery_routed`
between retryable attempts, and remove accepted git worktrees only when the
caller explicitly opts in.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees, append-only JSONL events.

---

### Task 1: Outcome Classification

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**

Add tests for `classify_attempt_outcome(runtime_result, task)`:

```python
def test_attempt_outcome_classifies_scope_violation_as_non_retryable(self):
    task = {"write_scope": ["generated/"]}
    result = {
        "result_status": "completed",
        "changed_files": ["outside.txt"],
        "output": {},
    }

    outcome = classify_attempt_outcome(result, task)

    self.assertEqual(outcome["validation_status"], "rejected")
    self.assertEqual(outcome["failure_category"], "scope_violation")
    self.assertFalse(outcome["retryable"])
```

Add a timeout case:

```python
def test_attempt_outcome_classifies_timeout_as_retryable(self):
    task = {"write_scope": ["generated/"]}
    result = {"result_status": "timed_out", "changed_files": [], "output": {}}

    outcome = classify_attempt_outcome(result, task)

    self.assertEqual(outcome["validation_status"], "rejected")
    self.assertEqual(outcome["failure_category"], "timeout")
    self.assertTrue(outcome["retryable"])
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py -v
```

Expected: fail because `classify_attempt_outcome` is not exported.

- [x] **Step 3: Implement classification**

Add and export:

```python
def classify_attempt_outcome(runtime_result, task):
    validation_status = _validate_runtime_result(runtime_result, task)
    if validation_status == "accepted":
        return {"validation_status": "accepted", "failure_category": None, "retryable": False}
    if runtime_result["result_status"] == "timed_out":
        return {"validation_status": "rejected", "failure_category": "timeout", "retryable": True}
    if runtime_result["result_status"] == "completed":
        return {"validation_status": "rejected", "failure_category": "scope_violation", "retryable": False}
    if runtime_result["result_status"] in {"blocked", "cancelled"}:
        return {
            "validation_status": "rejected",
            "failure_category": runtime_result["result_status"],
            "retryable": False,
        }
    return {"validation_status": "rejected", "failure_category": "runtime_error", "retryable": True}
```

- [x] **Step 4: Verify green**

Run the focused test command. Expected: pass.

### Task 2: Bounded Retry

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing retry test**

Add a runtime adapter that returns `failed` on the first call and `completed` on
the second call. Run `run_simulation(..., max_attempts=2)` and assert:

- final `attempt_id` is `ATTEMPT-002`;
- result `attempt_count` is `2`;
- final validation is `accepted`;
- events contain `recovery_routed`;
- replay shows `ATTEMPT-001` rejected and `ATTEMPT-002` accepted.

- [x] **Step 2: Verify red**

Run the focused test command. Expected: fail because `max_attempts` is not
accepted.

- [x] **Step 3: Implement retry loop**

Refactor `run_simulation` so each attempt allocates:

```text
ATTEMPT-001, LEASE-001, MSG-0001, WT-ATTEMPT-001
ATTEMPT-002, LEASE-002, MSG-0002, WT-ATTEMPT-002
```

Emit `recovery_routed` only when the outcome is retryable and another attempt is
available.

- [x] **Step 4: Verify green**

Run the focused test command. Expected: pass.

### Task 3: Accepted Worktree Cleanup

**Files:**
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing cleanup test**

Run `run_simulation(..., cleanup_accepted_worktrees=True)` with a real git repo
and assert the accepted worktree path no longer exists, the result reports
`worktree_removed: True`, and replay marks the accepted attempt with
`worktree_status == "removed"`.

- [x] **Step 2: Verify red**

Run the focused test command. Expected: fail because the parameter and event do
not exist.

- [x] **Step 3: Implement cleanup**

Add `worktree_removed` to `event.schema.json`. After accepted validation, call:

```python
subprocess.run(
    ["git", "-C", str(project_root), "worktree", "remove", "--force", str(worktree_path)],
    check=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
```

Emit a `worktree_removed` event with `cleanup_status: "removed"`.

- [x] **Step 4: Verify green**

Run the focused test command and schema event-type test. Expected: pass.

### Task 4: Documentation and Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m2-attempt-retry-cleanup.md`

- [x] **Step 1: Document M2 behavior**

Document the outcome fields, retry event, cleanup option, and default
non-cleanup behavior.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m2
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m2-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- unit test discovery ran 20 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- CLI regression returned an accepted single-attempt result with
  `attempt_count: 1`;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

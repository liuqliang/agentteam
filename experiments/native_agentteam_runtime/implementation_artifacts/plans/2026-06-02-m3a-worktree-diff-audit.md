# M3a Worktree Diff Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject runtime results that declare changed files but do not actually
produce a matching worktree diff, and record a compact audit summary for accepted
attempts.

**Architecture:** Keep patch integration out of scope. Add a `audit_worktree_diff`
helper that reads `git status --porcelain=v1` in the attempt worktree, normalizes
changed paths, and compares them to `runtime_result.changed_files`. Feed the
audit into attempt classification so validation can reject missing or undeclared
diffs before later patch-integration decisions.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees, append-only JSONL events.

---

### Task 1: Diff Audit Helper

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**

Add tests for `audit_worktree_diff(worktree_path, declared_changed_files)`:

```python
def test_worktree_diff_audit_detects_declared_file_missing_from_git_diff(self):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        _init_git_repo(repo)

        audit = audit_worktree_diff(repo, ["generated/missing.json"])

        self.assertEqual(audit["diff_status"], "mismatch")
        self.assertEqual(audit["missing_declared_files"], ["generated/missing.json"])
```

Add a positive case that writes `generated/actual.json` and checks
`diff_status == "matched"`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py -v
```

Expected: fail because `audit_worktree_diff` is not exported.

- [x] **Step 3: Implement helper**

Parse porcelain status lines, normalize rename/copy records conservatively, and
return:

```json
{
  "diff_status": "matched",
  "declared_changed_files": ["generated/actual.json"],
  "actual_changed_files": ["generated/actual.json"],
  "missing_declared_files": [],
  "undeclared_changed_files": []
}
```

- [x] **Step 4: Verify green**

Run focused tests. Expected: pass.

### Task 2: Validation Integration

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing integration test**

Add a runtime adapter that returns `completed` and declares
`generated/phantom.json` without writing any file. Run with a real git worktree
and assert:

- `validation_status == "rejected"`;
- `failure_category == "diff_mismatch"`;
- `result["diff_audit"]["missing_declared_files"] == ["generated/phantom.json"]`;
- replay stores the same failure category on the attempt.

- [x] **Step 2: Verify red**

Run focused tests. Expected: fail because validation ignores actual diff.

- [x] **Step 3: Wire audit into classification**

Call `audit_worktree_diff` after runtime output when `worktree_path` exists.
Pass the audit into `classify_attempt_outcome`. If the normal validation would
accept but `diff_status != "matched"`, reject with `failure_category:
"diff_mismatch"` and `retryable: false`.

- [x] **Step 4: Verify green**

Run focused tests. Expected: pass.

### Task 3: Documentation and Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m3a-worktree-diff-audit.md`

- [x] **Step 1: Document M3a behavior**

Document that M3a audits git diff but does not integrate patches.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m3a
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m3a-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- unit test discovery ran 23 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- CLI regression returned an accepted single-attempt result with `diff_audit:
  null` because no physical worktree was requested;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

# M6 Verified Integration Commit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit commit gate for verified integration worktrees without
merging, pushing, or changing the source branch.

**Policy:** Functional changes merge back only after all parts of the task have
been integrated and system-verified. M6 is therefore a local integration branch
checkpoint, not a merge policy.

**Architecture:** Keep the existing attempt worktree -> patch artifact ->
integration worktree -> verification chain. Add
`commit_verified_integration=True` as a final opt-in gate. Commit only if the
integration patch was applied and the integration verification command passed.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees.

---

### Task 1: API Commit Gate

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`

- [x] **Step 1: Write failing API tests**

Add tests for:

- verification passed -> commit integration branch;
- verification failed -> skip commit;
- verification missing -> skip commit.

Assert the source repo `HEAD` is unchanged in all cases.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py -v
```

Expected: fail because `run_simulation` does not yet accept
`commit_verified_integration`.

- [x] **Step 3: Implement gate**

Add:

- `commit_verified_integration` to `run_simulation`;
- `evaluate_integration_commit(...)`;
- `commit_integration_worktree(...)`;
- `integration_commit_evaluated` event;
- replay fields for integration commit status and SHA.

Use these result statuses:

- `not_requested`: default;
- `committed`: commit succeeded;
- `skipped`: gate requested but preconditions were not met;
- `failed`: git commit itself failed.

- [x] **Step 4: Verify green**

Run the focused runtime tests. Expected: pass.

### Task 2: CLI and Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write failing CLI test**

Add `--integration-verification-command-json` plus
`--commit-verified-integration`, then assert the integration branch is committed
and the source branch is unchanged.

- [x] **Step 2: Implement CLI support**

Parse the verification command as a JSON string array so it can coexist with
the existing `--shell-command` and `--codex-command` tail arguments.

- [x] **Step 3: Document M6 behavior**

Document the distinction between:

- committing a verified integration branch checkpoint;
- merging a complete functional change back to the source branch.

### Task 3: Verification

- [x] **Step 1: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m6
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m6-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- API tests first failed because `run_simulation` did not accept
  `commit_verified_integration`, then passed after implementation;
- CLI test first failed with argparse exit status 2, then passed after adding
  `--integration-verification-command-json` and
  `--commit-verified-integration`;
- unit test discovery ran 32 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression returned `integration_commit_status:
  "not_requested"`;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

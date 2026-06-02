# M4 Integration Branch Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply an accepted patch artifact to an isolated integration branch
worktree without committing or merging it into the source repository.

**Architecture:** Keep merge-to-main and auto-commit out of scope. Add an
explicit `integrate_accepted_patch=True` option to `run_simulation`. When an
accepted attempt has a `patch_path`, create a separate git worktree on
`agentteam/integration/<task-id>`, run `git apply <patch>`, emit
`patch_integrated`, and return integration metadata.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees, git patch apply.

---

### Task 1: Integration Apply Path

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing integration test**

Add a shell runtime test that creates `generated/integration_result.json`, runs
`run_simulation(..., integrate_accepted_patch=True)`, and asserts:

- `integration_status == "applied"`;
- `integration_branch == "agentteam/integration/TASK-001"`;
- the integration worktree exists and contains `generated/integration_result.json`;
- the integration worktree `HEAD` equals the source repo `HEAD`, proving no
  commit happened;
- replay stores `integration_status == "applied"`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py -v
```

Expected: fail because `integrate_accepted_patch` is not accepted.

- [x] **Step 3: Implement integration helper**

Add `apply_patch_to_integration_worktree(project_root, output_dir, task_id,
patch_path)` that:

- creates worktree path `output_dir/integration/<task-id>`;
- creates branch `agentteam/integration/<task-id>`;
- runs `git apply <patch_path>` in that worktree;
- returns branch, path, and status metadata.

- [x] **Step 4: Verify green**

Run focused tests. Expected: pass.

### Task 2: Documentation and Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m4-integration-branch-apply.md`

- [x] **Step 1: Document M4 behavior**

Document the explicit opt-in flag, returned integration fields, and the fact
that M4 does not commit or merge.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m4
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m4-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- focused tests first failed because `integrate_accepted_patch` was not
  accepted, then passed after implementation;
- CLI integration test first failed because `--integrate-accepted-patch` was not
  recognized, then passed after adding the flag;
- unit test discovery ran 26 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression returned `integration_status: "not_requested"`;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

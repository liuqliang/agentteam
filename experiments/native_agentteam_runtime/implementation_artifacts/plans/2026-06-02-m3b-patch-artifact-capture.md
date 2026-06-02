# M3b Patch Artifact Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist a patch artifact for each worktree-backed attempt so later
integration policy can inspect or apply the actual diff without trusting only
runtime prose.

**Architecture:** Keep automatic patch integration out of scope. After runtime
execution and diff audit, write a compact patch file under
`output_dir/attempts/<attempt_id>/worktree.patch` when the attempt has actual
changed files. Include tracked diffs via `git diff HEAD` and untracked-file
additions via `git diff --no-index /dev/null <path>`.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees, git patch format.

---

### Task 1: Patch Artifact Writer

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing test**

Add a shell runtime test that creates `generated/patch_result.json`, runs with a
real worktree, and asserts:

- `result["patch_path"]` points to an existing file;
- the patch file contains `generated/patch_result.json`;
- the attempt result also stores the same `patch_path`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py -v
```

Expected: fail because `patch_path` is not returned.

- [x] **Step 3: Implement writer**

Add `write_patch_artifact(worktree_path, artifact_dir, actual_changed_files)`.
Use `git status --porcelain=v1 --untracked-files=all` to identify untracked
files. For tracked changes, run `git diff --binary --no-ext-diff HEAD -- <paths>`.
For each untracked path, append `git diff --binary --no-ext-diff --no-index
-- /dev/null <path>`; accept exit code 1 because `git diff --no-index` returns
1 when differences exist.

- [x] **Step 4: Verify green**

Run focused tests. Expected: pass.

### Task 2: Documentation and Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m3b-patch-artifact-capture.md`

- [x] **Step 1: Document M3b behavior**

Document the patch artifact path, what it includes, and that it is not applied
to the source repository.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m3b
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m3b-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- unit test discovery ran 24 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- CLI regression returned an accepted single-attempt result with `patch_path:
  null` because no physical worktree was requested;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

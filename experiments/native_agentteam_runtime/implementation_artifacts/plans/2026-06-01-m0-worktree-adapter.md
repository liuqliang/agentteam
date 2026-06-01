# M0 Worktree Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the M0 file runtime with real git worktree creation and a first pluggable runtime adapter boundary.

**Architecture:** Keep the scheduler deterministic. Bind real git worktrees to writable attempts only when `project_root` is supplied. Keep the runtime backend as a replaceable adapter and use `FakeRuntimeAdapter` to prove result validation before connecting Codex or Claude Code.

**Tech Stack:** Python 3.12 standard library, `unittest`, local `git` command.

---

### Task 1: Real Worktree Creation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing test**

Add a test that initializes a temporary git repository, runs `run_simulation`
with `project_root`, and asserts the returned `worktree_path` is a real git
worktree.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
```

Expected: fail before `project_root` support exists.

- [x] **Step 3: Implement worktree creation**

Create a worktree under `<output_dir>/worktrees/WT-ATTEMPT-001` using:

```bash
git -C <project_root> worktree add -b agentteam/ATTEMPT-001 <worktree_path> HEAD
```

- [x] **Step 4: Verify green**

Run the same unittest command. Expected: pass.

### Task 2: Runtime Adapter Boundary

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**

Add tests for `FakeRuntimeAdapter` and for an out-of-scope adapter result that
must be rejected by validation.

- [x] **Step 2: Verify red**

Run unit tests. Expected: fail before `FakeRuntimeAdapter` is exported and
before `run_simulation` accepts `runtime_adapter`.

- [x] **Step 3: Implement adapter support**

Add `FakeRuntimeAdapter.run(message, worktree_path=None)` and make
`run_simulation` use the injected adapter result instead of hardcoded fake
changed files.

- [x] **Step 4: Verify green**

Run unit tests. Expected: pass.

### Task 3: CLI And Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write failing CLI test**

Add a test that invokes `python3 -m agentteam_runtime.cli` with `--project-root`
and asserts a real worktree path is returned.

- [x] **Step 2: Verify red**

Run unit tests. Expected: CLI exits with argument parsing failure before
`--project-root` exists.

- [x] **Step 3: Implement CLI flag and update docs**

Add `--project-root` and document the new behavior.

- [x] **Step 4: Verify green**

Run unit tests and CLI verification. Expected: pass.

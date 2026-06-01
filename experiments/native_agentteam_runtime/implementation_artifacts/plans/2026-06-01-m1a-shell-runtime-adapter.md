# M1a Shell Runtime Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first real process runtime adapter before connecting Codex or Claude Code.

**Architecture:** Keep `run_simulation` as the deterministic scheduler path. Add `ShellRuntimeAdapter` as a replaceable backend that runs a local command in the attempt worktree, sends the mailbox message through stdin, parses one JSON result from stdout, and routes failures into normal validation.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local git worktrees.

---

### Task 1: Shell Adapter API

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**

Add tests that import `ShellRuntimeAdapter`, run a temporary Python worker in a
real worktree, and assert the worker-created file is inside `write_scope`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
```

Expected: fail because `ShellRuntimeAdapter` is not exported.

- [x] **Step 3: Implement adapter**

Add `ShellRuntimeAdapter(command, timeout_seconds=60)` with:

- stdin: mailbox message JSON;
- cwd: attempt worktree path;
- stdout: one JSON result;
- non-zero exit: failed result;
- timeout: timed-out result;
- invalid stdout JSON: failed result.

- [x] **Step 4: Verify green**

Run unit tests. Expected: pass.

### Task 2: Failure Routing

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing test**

Add a shell worker that exits non-zero and assert the attempt is `failed` and
validation is `rejected`.

- [x] **Step 2: Verify red**

Run unit tests. Expected: fail before result-status-aware validation.

- [x] **Step 3: Implement validation**

Reject runtime results unless `result_status == "completed"` and every
`changed_files` entry is inside the task `write_scope`.

- [x] **Step 4: Verify green**

Run unit tests. Expected: pass.

### Task 3: CLI Support

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write failing CLI test**

Add a test that invokes:

```bash
python3 -m agentteam_runtime.cli ... --shell-command python3 worker.py
```

and asserts the shell-created file exists in the worktree.

- [x] **Step 2: Verify red**

Run unit tests. Expected: CLI rejects `--shell-command`.

- [x] **Step 3: Implement CLI flag and docs**

Add `--shell-command` as the final CLI argument and instantiate
`ShellRuntimeAdapter` when present.

- [x] **Step 4: Verify green**

Run unit tests and CLI verification. Expected: pass.

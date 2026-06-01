# M1b Codex Runtime Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Codex process adapter that satisfies the same bounded runtime
result contract as the fake and shell adapters.

**Architecture:** Keep `run_simulation` as the deterministic scheduler path.
`CodexRuntimeAdapter` starts `codex exec` in the attempt worktree, sends the
bounded mailbox task as stdin prompt input, reads the final JSON result from
`--output-last-message`, and routes all failures through normal validation.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees, Codex CLI command contract.

---

### Task 1: Codex Adapter Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**

Add tests that import `CodexRuntimeAdapter`, run a fake Codex command in a real
worktree, and assert the adapter reads the JSON result from
`--output-last-message`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
```

Expected: fail because `CodexRuntimeAdapter` is not implemented/exported.

- [x] **Step 3: Implement adapter**

Add `CodexRuntimeAdapter(command=None, model=None, sandbox="workspace-write",
timeout_seconds=300, extra_args=None)` with:

- command prefix defaulting to `["codex", "exec"]`;
- `-C <worktree>` so Codex operates in the attempt worktree;
- `-s workspace-write` as the conservative sandbox default;
- stdin prompt containing the mailbox message and final JSON contract;
- `--output-last-message <file>` as the result extraction path;
- failed results for timeout, non-zero exit, missing result file, and invalid
  result JSON.

- [x] **Step 4: Verify green**

Run unit tests. Expected: pass.

### Task 2: CLI Support

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI test**

Add a test that invokes:

```bash
python3 -m agentteam_runtime.cli ... --codex-command python3 fake_codex.py
```

and asserts the fake Codex-created file exists in the worktree.

- [x] **Step 2: Verify red**

Run unit tests. Expected: CLI rejects `--codex-command`.

- [x] **Step 3: Implement CLI flag**

Add `--codex-command` as a final CLI argument and instantiate
`CodexRuntimeAdapter` when present. Reject simultaneous use of
`--shell-command` and `--codex-command`.

- [x] **Step 4: Verify green**

Run unit tests. Expected: pass.

### Task 3: Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Add: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-01-m1b-codex-runtime-adapter.md`

- [x] **Step 1: Document API and CLI**

Document `CodexRuntimeAdapter`, the CLI flag, and the expected final JSON result
contract.

- [x] **Step 2: Document verification boundary**

Record that committed tests use a fake Codex command and do not spend live model
calls.

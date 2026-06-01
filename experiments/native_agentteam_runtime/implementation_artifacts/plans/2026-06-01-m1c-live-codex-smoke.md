# M1c Live Codex Smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, gated smoke entrypoint that can prove the M1b Codex
adapter works with a real `codex exec` invocation without putting live model
calls into normal unit tests.

**Architecture:** Keep normal tests deterministic. The live smoke command skips
unless `AGENTTEAM_RUN_LIVE_CODEX=1` is set. When enabled, it creates a temporary
git repository, writes a minimal L0 backlog item, runs `CodexRuntimeAdapter`,
and fails unless Codex creates the exact expected file inside `write_scope` and
reports it in `changed_files`.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees, Codex CLI command contract.

---

### Task 1: Gated Entry Point

**Files:**
- Add: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_smoke.py`
- Add: `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`

- [x] **Step 1: Write failing skip test**

Add a subprocess test for:

```bash
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/smoke
```

without `AGENTTEAM_RUN_LIVE_CODEX=1`.

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail because the module does not exist.

- [x] **Step 3: Implement skip path**

Return JSON with `{"status": "skipped", "reason": "set AGENTTEAM_RUN_LIVE_CODEX=1"}`
and do not create the output directory.

- [x] **Step 4: Verify green**

Run the focused test. Expected: pass.

### Task 2: Fake-Codex Smoke Path

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_smoke.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`

- [x] **Step 1: Write failing fake command test**

Add a subprocess test that sets `AGENTTEAM_RUN_LIVE_CODEX=1`, passes
`--codex-command python3 fake_codex.py`, and asserts the expected file exists in
the attempt worktree.

- [x] **Step 2: Verify red**

Run the focused test. Expected: fail before the enabled smoke path is
implemented.

- [x] **Step 3: Implement enabled smoke path**

Create:

- a temporary git repo;
- generated agent-pool and backlog fixtures;
- a `CodexRuntimeAdapter` with either default `codex exec` or the passed command;
- exact-file validation for `generated/live_codex_smoke.json`.

- [x] **Step 4: Verify green**

Run the focused test. Expected: pass.

### Task 3: Documentation

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Add: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-01-m1c-live-codex-smoke.md`

- [x] **Step 1: Document live invocation**

Document the opt-in command, skip behavior, expected file, and failure
condition.

- [x] **Step 2: Document deterministic verification boundary**

Record that normal tests use skip/fake paths and do not spend live model calls.

### Verification Evidence

- Focused smoke tests first failed because `agentteam_runtime.live_codex_smoke`
  did not exist, then passed after implementation.
- Current local CLI is `codex-cli 0.132.0`; `codex exec --help` does not expose
  the old `-a/--ask-for-approval` option, so the adapter now omits that flag by
  default.
- A real opt-in run completed on 2026-06-01:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke \
  --output-dir /tmp/agentteam-live-codex-real-m1c-v2 \
  --timeout-seconds 180
```

Observed result:

```json
{
  "changed_files": ["generated/live_codex_smoke.json"],
  "expected_file_exists": true,
  "status": "completed",
  "validation_status": "accepted"
}
```

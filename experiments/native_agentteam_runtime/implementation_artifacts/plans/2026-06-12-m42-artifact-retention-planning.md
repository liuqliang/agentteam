# M42 Artifact Retention Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only artifact retention planning view to `agentteam gc`.

**Architecture:** Use the M40 projection DB to list bounded rebuildable
artifact candidates. Keep deletion disabled for artifact candidates and leave
authoritative files protected.

**Tech Stack:** Python standard library, SQLite, existing AgentTeam CLI
patterns, `unittest`.

---

## M42a Retention Plan Projection API

**Files:**

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add a failing test for `read_projected_artifact_retention_plan(work_root)`
      returning rebuildable candidates from a fresh projection DB.
- [x] Add a failing test that returns `None` when the projection DB is missing
      or stale.
- [x] Implement the DB reader with `deletion_enabled == False`, policy counts,
      protected policy explanations, and bounded candidate rows.
- [x] Export `read_projected_artifact_retention_plan`.

## M42b `agentteam gc --artifacts`

**Files:**

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `docs/agentteam-command-reference.md`
- Modify:
  `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add failing CLI tests for `agentteam gc --artifacts --json` with a fresh
      projection DB and missing DB.
- [x] Add `--artifacts` and `--artifact-limit` flags to `gc`.
- [x] Include `artifact_retention_plan` only when `--artifacts` is requested.
- [x] Update text output with a compact artifact plan count.
- [x] Update docs and roadmap.
- [x] Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack

env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime

git diff --check
```

Expected: all tests pass and whitespace check reports no errors.

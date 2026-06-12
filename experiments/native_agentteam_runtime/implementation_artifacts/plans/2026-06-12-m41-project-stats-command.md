# M41 Project Stats Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `agentteam stats` command for compact project-level
runtime, artifact, evidence, and token summaries.

**Architecture:** Use the M40 projection DB when it is fresh, and fall back to
a direct file scan when the DB is missing, stale, or unreadable. Keep files
authoritative and avoid automatic rebuild or cleanup side effects.

**Tech Stack:** Python standard library, SQLite, existing AgentTeam CLI
patterns, `unittest`.

---

## M41a Stats Projection API

**Files:**

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add a failing test for `build_project_stats(work_root)` using a fresh
      projection DB. It should report `projection_source == "db"`, run/taskpack
      counts, artifact totals, evidence counts, and token totals.
- [x] Add a failing test for file-scan fallback when the DB is missing or stale.
      It should report `projection_source == "files"` and the same core counts.
- [x] Implement `build_project_stats(work_root)` with DB-first and file-scan
      fallback behavior.
- [x] Export `build_project_stats` from `agentteam_runtime.__init__`.

## M41b `agentteam stats` CLI

**Files:**

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `docs/agentteam-command-reference.md`
- Modify:
  `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add failing CLI tests for `agentteam stats --json` reading a fresh DB and
      falling back to files.
- [x] Add parser, help entry, JSON output, and compact human text output.
- [x] Update command reference and roadmap.
- [x] Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack

env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime

git diff --check
```

Expected: all tests pass and whitespace check reports no errors.

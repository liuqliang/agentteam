# M40 Artifact Projection Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a rebuildable project-level SQLite projection database for
AgentTeam artifacts.

**Architecture:** Keep file-backed artifacts authoritative. Add a focused
`projection_db.py` module that scans a project work root and writes
`agentteam.db` as a rebuildable summary. Expose the first operator commands
through `agentteam db rebuild` and `agentteam db check`.

**Tech Stack:** Python standard library, SQLite, existing AgentTeam profile and
CLI patterns, `unittest`.

---

## M40a Projection Schema And CLI

Implement the minimum useful database projection.

**Files:**

- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add failing tests for `rebuild_project_projection_db(work_root)` creating
      `<work_root>/agentteam.db` with `schema_info`, `runs`, `taskpacks`,
      `events`, `tasks`, and `evidence_summaries`.
- [x] Add failing tests for `check_project_projection_db(work_root)` returning
      `check_status == "passed"` after rebuild and `check_status == "failed"`
      when an event file changes afterward.
- [x] Add failing CLI tests for `agentteam db rebuild --json` and
      `agentteam db check --json`.
- [x] Implement `projection_db.py` with temporary rebuild, schema creation,
      file scan, projection writes, and compact summary output.
- [x] Add `agentteam db` parser and text output.
- [x] Update roadmap status from deferred to M40a in progress/implemented.
- [x] Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack

env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime

git diff --check
```

Expected: all tests pass and whitespace check reports no errors.

## M40b DB-Eligible Read Paths

Use the projection as an optional read accelerator without changing authority.

- [x] Add file fallback tests for stale DB on `taskpack list`.
- [x] Let `taskpack list --json` read frozen taskpack rows from the projection
      when it is present and fresh.
- [x] Add stale DB fallback tests for `logs --json`.
- [x] Let `logs --json` read events from the projection when it is present and
      fresh.
- [x] Add stale DB fallback tests for `status --json`.
- [x] Let `status --json` replay events from the projection when it is present
      and fresh, while retaining file-backed liveness/state reads.
- [x] Add file fallback tests for missing/corrupt DB on the remaining read
      paths.
- [x] Let report metadata read from the projection when it is present and
      fresh.
- [x] Keep exact user-facing output compatible with existing tests.

## M40c Artifact Metadata And Smart GC

Add artifact-level indexing and cleanup explanations.

- [x] Add content hash and size metadata for reports, patches, taskpacks,
      state snapshots, role contexts, repo contexts, and evidence summaries.
- [x] Add token/stat aggregates.
- [x] Teach `gc --dry-run` to use indexed metadata to explain protected and
      rebuildable artifacts.

## Out Of Scope For M40a

- DB-primary storage;
- replacing per-run `scheduler_state.sqlite`;
- changing worker or scheduler event durability;
- live Codex calls in tests;
- automatic deletion of artifacts based on DB rows.

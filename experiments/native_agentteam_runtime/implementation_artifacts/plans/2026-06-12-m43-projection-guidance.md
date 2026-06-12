# M43 Projection Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stale or missing projection DB states actionable in `stats` and
`gc --artifacts` output.

**Architecture:** Keep the M40 projection rebuildable and optional. When a
command falls back to files or cannot build an artifact retention plan, include
compact `projection_warning` and `next_action` fields instead of forcing the
operator to infer what to run next.

**Tech Stack:** Python standard library, existing AgentTeam CLI, `unittest`.

---

## M43a Stats Guidance

**Files:**

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add a failing test that `build_project_stats` file fallback includes
      `projection_warning` and `next_action`.
- [x] Add the fields to file fallback stats output.
- [x] Print `next_action` in human stats text when present.

## M43b Artifact Plan Guidance

**Files:**

- Modify:
  `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `docs/agentteam-command-reference.md`
- Modify:
  `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Test:
  `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] Add a failing CLI test that `gc --artifacts --json` without a fresh DB
      includes `next_action`.
- [x] Add `next_action` and `projection_warning` to unavailable artifact plans.
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

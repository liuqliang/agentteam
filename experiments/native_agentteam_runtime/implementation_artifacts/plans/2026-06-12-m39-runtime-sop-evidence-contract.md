# M39 Runtime SOP Evidence Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the runtime enforce SOP-style risk and evidence rules without
copying the outer SOP file layout or introducing the artifact database yet.

**Architecture:** Keep file-backed artifacts authoritative. Add compact
evidence summaries to task/result/report paths, route `L3` to semantic
architecture escalation, and block `L2` integration when evidence is incomplete.

**Tech Stack:** Python standard library, existing file-backed scheduler,
existing task proposal validation, fake/shell worker tests, `unittest`.

---

## M39a Risk And Evidence Contract

Define the runtime contract and shared helpers before changing scheduler
behavior.

**Files:**

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] Add failing tests that assert `risk_target == "L3"` is recognized as a
      supported declared level, but is not normalized as a ready implementation
      task.
- [ ] Add failing tests for evidence summary normalization:
      `evidence_level`, `evidence_status`, `trace_carrier`, and
      `missing_evidence`.
- [ ] Add small helper functions or dataclasses near existing task/result
      normalization code rather than introducing a separate framework.
- [ ] Keep `L0` and `L1` behavior backward compatible when workers do not
      provide explicit evidence fields.
- [ ] Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack
```

Expected: all tests pass after implementation.

## M39b L3 Semantic Architecture Escalation

Route semantic architecture risk to a dedicated role instead of writable
workers.

**Files:**

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/events.py` if event type constants are centralized there
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] Add failing proposal-quality tests that an `L3` generated proposal becomes
      a blocked semantic escalation candidate with a stable blocker reason such
      as `semantic_escalation_required`.
- [ ] Add failing scheduler tests that `L3` backlog items are not dispatched to
      `implementation-worker` roles.
- [ ] Add an event payload for `semantic_escalation_required` containing
      `task_id`, declared risk, reason, source proposal id, and recommended
      role `semantic_architecture_agent`.
- [ ] Add a manual gate path for unresolved semantic architecture decisions.
      The gate should be opened only when the semantic architecture step reports
      unresolved, not simply because a task was classified `L3`.
- [ ] Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
```

Expected: all tests pass after implementation.

## M39c L2 Evidence Gate And Reporting

Capture incomplete results but stop under-evidenced changes before integration.

**Files:**

- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py` if CLI report/status formatting lives there
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] Add failing tests where an `L2` worker returns a patch and
      `evidence_status == "incomplete"`.
- [ ] Assert the result is retained for inspection, but integration is blocked
      with `integration_blocked_by_evidence`.
- [ ] Add report/status summary tests for completed, blocked, and escalated
      evidence counts.
- [ ] Reuse existing sparse notification/report summary paths so Feishu receives
      semantic completion text instead of raw logs when a taskpack becomes idle,
      blocked, or escalated.
- [ ] Run:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack

env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime

git diff --check
```

Expected: all tests pass and whitespace check reports no errors.

## Out Of Scope

- SQLite schema or DB-backed query changes;
- automatic editing of outer `design/` SOP documents;
- direct semantic authority edits by ordinary implementation workers;
- live Codex calls in normal unit tests;
- multi-backend model routing beyond existing Codex profiles.

# M25 Proposal Quality Gate Design

Status: approved for implementation by standing roadmap authorization.

## Goal

M25 rejects low-quality planner-generated tasks before they become executable
backlog state.

## Scope

M25 supports:

- self-dependency rejection;
- generated dependency cycle rejection;
- supported risk target enforcement for `L0`, `L1`, and `L2`;
- basic risk-size checks for generated tasks;
- automatic review routing for `L2` generated tasks by normalizing them to
  `backlog_status=blocked` with `requires_review`;
- scheduler rejection evidence that preserves the deterministic validation
  reason.

M25 deliberately defers:

- learned task sizing;
- semantic risk inference;
- L3 authority-update routing;
- reviewer-agent execution;
- automatic unblocking after review;
- cross-milestone planning.

## Architecture

`task_proposal.py` remains the proposal quality gate. The scheduler still treats
planner output as untrusted and calls `normalize_task_proposal(...)` before
adding generated tasks to backlog state.

M25 adds deterministic checks:

- a task may not depend on itself;
- generated tasks may not form cycles through `depends_on`;
- `risk_target` must be `L0`, `L1`, or `L2`;
- `L0` tasks may not declare more than one write scope or repository-wide
  write scope;
- `L1` tasks may not declare more than three write scopes;
- `L2` tasks are accepted only as blocked review candidates.

The normalized shape for an `L2` generated task is:

```json
{
  "task_id": "TASK-M25-REVIEW-001",
  "backlog_status": "blocked",
  "risk_target": "L2",
  "blockers": ["requires_review"]
}
```

This keeps the task visible in backlog state while preventing immediate worker
dispatch.

When proposal validation fails during decomposition, `TwoPhaseFileScheduler`
keeps the existing `invalid_task_proposal` failure category and records the
specific validation message in result state. M25 also includes that message in
the validation event payload so state-index or event inspection can reveal why
the proposal was rejected.

## Acceptance

M25 is accepted when:

- self-dependencies are rejected;
- generated dependency cycles are rejected;
- unsupported risk targets are rejected;
- over-broad `L0` and `L1` generated tasks are rejected;
- `L2` generated tasks normalize to blocked review candidates;
- two-phase decomposition rejection exposes the quality-gate reason in result
  state and validation event payload;
- existing M21-M24 planner, context, artifact, and Codex planner tests still
  pass.

# Phase 3A Execution Contract v2

Status: operator-authorized execution repair for the approved Phase 3A route.
It supersedes only the launch configuration of the 2026-08-06 Phase 3A
blueprint. The benchmark choice, task graph, acceptance criteria, P1/P2
authority boundary, and prohibition on scored benchmark execution are
unchanged.

## Repair Decision

The v1 blueprint incorrectly placed `medium` in the Codex `model` field. The
runtime would therefore request a model named `medium` instead of selecting a
medium reasoning effort. Before launch, add role-level `reasoning_profile`
propagation and bind both Phase 3A roles to the installed model:

```text
repo_map_agent:       gpt-5.6-sol, medium reasoning
implementation_worker: gpt-5.6-sol, high reasoning
```

These are metered development-worker invocations used to implement Phase 3A.
They are not benchmark samples and must not enter Phase 3 scores. This contract
continues to authorize zero `single_codex`, `agentteam_direct`, or
`agentteam_full` scored benchmark executions. Phase 3B still requires a
separate frozen instance list, budget, mode order, and operator authorization.

## Execution Boundary

- Execute P3-MAP, P3-01, P3-02, and P3-03 in the original dependency order.
- Preserve the original decision root and task bindings.
- Record development token usage through normal model-invocation accounting.
- Do not download benchmark data or expose gold material.
- Do not merge the verified integration branch into the source branch without
  operator review.
- Stop if implementation changes the benchmark, metrics, fairness rules, or
  repetition policy.

## Acceptance

- role reasoning profiles survive blueprint materialization and freeze;
- the repo-map worker command contains `model_reasoning_effort=medium` and a
  real model identifier;
- all original Phase 3A acceptance criteria remain in force;
- the readiness receipt records zero scored/live benchmark mode executions;
- focused tests, artifact lint, and the complete runtime test suite pass.


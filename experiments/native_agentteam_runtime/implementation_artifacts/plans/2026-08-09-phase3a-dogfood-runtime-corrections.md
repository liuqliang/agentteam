# Phase 3A Dogfood Runtime Corrections

## Decision

Repair the bounded runtime defects observed during
`phase3a-execution-contract-v3` before implementing the retained readiness
controller. The failed P3-03 attempt remains immutable evidence.

## Scope

1. Add a compact retry handoff to the next dispatch. It must identify the
   previous attempt, failure category, evidence status and missing evidence,
   changed files, and retained code-state/evidence references without copying
   the full prior transcript.
2. On a run stop, stop every exact active invocation service referenced by an
   inflight attempt before the worker pool exits. Preserve identity checks and
   report ambiguous or failed stops instead of signaling an unverified PID.
3. Bound `worker_pool_supervision` retained in the terminal JSON result and
   report the total observation count separately.
4. Permit `agentteam stop --run-dir <path>` to operate without loading a
   project profile when all required run identity is available from the run.

## Acceptance

- A retry dispatch contains the compact handoff and does not embed the prior
  runtime output or transcript.
- A stopped supervised run invokes active-invocation cleanup and worker-pool
  cleanup, with no model subprocess left behind in the tested lifecycle.
- Long waiting loops retain a constant number of supervision snapshots.
- Explicit-run-directory stop is covered by a CLI regression test.
- Focused tests and the complete runtime suite pass.

## Deferred

The deterministic `P3-READY` controller and retained
`acceptance/phase3-readiness.json` publication remain the next Phase 3A task.
No scored benchmark or provider-mode execution is authorized by this repair.

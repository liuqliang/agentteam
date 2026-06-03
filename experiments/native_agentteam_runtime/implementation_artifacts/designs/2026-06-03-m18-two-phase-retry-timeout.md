# M18 Two-Phase Retry Timeout Design

Status: approved for implementation.

## Goal

M18 prevents the two-phase scheduler from getting stuck when a worker returns a
retryable failure or never writes a result. It adds bounded retry and lease
timeout recovery to the M17 dispatch/collect path.

## Scope

M18 supports:

- `max_attempts` on `TwoPhaseFileScheduler`;
- `lease_timeout_seconds` for inflight attempts without mailbox results;
- retry routing for retryable runtime outcomes;
- terminal blocking for non-retryable or exhausted attempts;
- CLI flags for the two-phase worker-pool path;
- root event replay and SQLite state index rebuild after retry and timeout.

M18 deliberately defers:

- worker process restart and backoff policy;
- killing a still-running external worker command;
- integration branch apply, verification, and commit in the two-phase path;
- cross-run recovery for partially written mailbox messages;
- fairness beyond the M17 ready-task ordering and one-agent-one-attempt rule.

## Architecture

The scheduler keeps the M17 public shape:

```python
scheduler.dispatch_ready()
scheduler.collect_ready_results()
scheduler.tick()
```

M18 extends construction:

```python
TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    max_inflight=2,
    max_attempts=2,
    lease_timeout_seconds=900,
)
```

`max_attempts` is per task. Attempt ids use the existing two-phase task-scoped
format:

```text
TASK-001-ATTEMPT-001
TASK-001-ATTEMPT-002
```

`dispatch_ready()` computes the next attempt number from scheduler state, writes
attempt-specific message and lease ids, and stores `lease_expires_at` in
`inflight_attempts`.

`collect_ready_results()` first looks for a matching mailbox `runtime_result`.
If no result exists and the lease has not expired, the attempt remains inflight.
If the lease has expired, the scheduler synthesizes a `timed_out` runtime result
and collects that result through the same classification path.

## Result Classification

M18 reuses the existing `classify_attempt_outcome(...)` helper from the blocking
scheduler:

- `completed` with changed files inside write scope: accepted, terminal done;
- `completed` outside write scope: rejected, `scope_violation`, non-retryable;
- `timed_out`: rejected, `timeout`, retryable;
- `failed` and other unknown runtime statuses: rejected, `runtime_error`,
  retryable;
- `blocked` or `cancelled`: rejected, non-retryable.

The two-phase path still does not audit git diffs in M18, so
`diff_audit` remains `null`.

## Retry Flow

When an attempt is rejected and retryable:

1. If `attempt_number < max_attempts`, append `recovery_routed`, remove the
   attempt from `inflight_attempts`, leave the task `ready`, and allow a later
   dispatch pass to create the next attempt.
2. If the attempt has exhausted `max_attempts`, mark the task `blocked` with the
   failure category.

Accepted attempts mark the task `done` and append `backlog_updated`.
Non-retryable rejected attempts mark the task `blocked` immediately.

## Events

M18 keeps the M17 event sequence for collected attempts:

- `runtime_session_observed`;
- `runtime_output_received`;
- `runtime_session_stopped`;
- `validation_accepted` or `validation_rejected`;
- `backlog_updated`, only for accepted attempts.

Retryable rejected attempts with remaining attempts also append:

- `recovery_routed`.

No new event schema type is required.

## State

The two-phase state file gains scheduler settings:

```json
{
  "scheduler_status": "initialized",
  "max_attempts": 2,
  "lease_timeout_seconds": 900,
  "backlog": {"items": []},
  "steps": [],
  "inflight_attempts": []
}
```

Each inflight attempt gains:

```json
{
  "attempt_number": 1,
  "lease_expires_at": "2026-05-31T00:15:00Z"
}
```

The scheduler can reconstruct the next attempt number from existing steps and
inflight attempts, so no separate counter file is needed.

## CLI

The two-phase worker-pool path accepts:

```text
--max-attempts <n>
--lease-timeout-seconds <n>
```

Both values must be positive except `--lease-timeout-seconds 0`, which is
allowed for deterministic timeout tests and immediate local recovery.

## Acceptance

M18 is accepted when:

- a retryable failed result routes recovery and the same task can succeed on a
  second attempt;
- an inflight attempt with an expired lease is collected as `timed_out` instead
  of staying inflight;
- exhausted retry attempts block the task;
- existing M17 two-phase dispatch and worker-pool CLI behavior still passes;
- full runtime tests, artifact lint, JSON checks, bytecode compilation, and
  diff whitespace checks pass.

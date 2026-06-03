# M17 Two-Phase Dispatch Collect Design

Status: approved for implementation.

## Goal

M17 introduces the first real scheduler concurrency boundary: dispatch ready
tasks without blocking on runtime execution, then collect mailbox results in a
later tick.

M16 proved that several resident worker processes can run at the same time.
M17 uses that pool by allowing multiple task attempts to be inflight
simultaneously.

## Scope

M17 adds a new two-phase scheduler path. It does not replace the existing
blocking `FileScheduler.step_once()` path.

The new path supports:

- `max_inflight`;
- dispatching more than one ready task before collecting results;
- file mailbox workers as the execution mechanism;
- root event log replay and SQLite state index rebuild;
- daemon CLI smoke through a worker pool.

M17 deliberately defers:

- retry attempts beyond one attempt;
- integration branch apply/verify/commit;
- worker restart/backoff;
- lease expiry;
- task cancellation;
- sophisticated fairness across roles;
- replacing the existing blocking daemon path.

## Architecture

Add a new module:

```text
agentteam_runtime/two_phase_scheduler.py
```

It owns a new `TwoPhaseFileScheduler` with three public operations:

```python
scheduler.dispatch_ready()
scheduler.collect_ready_results()
scheduler.tick()
```

`dispatch_ready()` selects ready tasks up to `max_inflight`, creates per-task
step directories, writes dispatch messages to the selected agents' inboxes, and
records runtime sessions as started. The scheduler treats agents with existing
inflight attempts as busy and marks newly selected agents busy for the current
dispatch pass, so a single agent cannot receive two simultaneous attempts.

`collect_ready_results()` scans the matching outboxes for inflight attempts. If
a matching `runtime_result` exists, it records runtime observation, validates
the result, updates backlog state, appends canonical events, rebuilds the
SQLite state index, and removes the attempt from `inflight_attempts`.

`tick()` runs `collect_ready_results()` first, then dispatches additional ready
tasks if inflight capacity remains.

## State

The two-phase scheduler writes:

```text
<output-dir>/state/two_phase_scheduler_state.json
<output-dir>/events.jsonl
<output-dir>/state/scheduler_state.sqlite
```

State shape:

```json
{
  "scheduler_status": "initialized|running|waiting|idle|max_ticks_reached",
  "backlog": {"items": []},
  "steps": [],
  "inflight_attempts": []
}
```

Each inflight attempt records enough information to collect without re-reading
transient process state:

```json
{
  "step_id": "STEP-0001-TASK-001",
  "task_id": "TASK-001",
  "attempt_id": "TASK-001-ATTEMPT-001",
  "lease_id": "TASK-001-LEASE-001",
  "message_id": "TASK-001-MSG-0001",
  "runtime_session_id": "SESSION-TASK-001-ATTEMPT-001",
  "agent_id": "agent-repo-map",
  "outbox_path": "/tmp/run/steps/STEP-0001-TASK-001/mailboxes/agent-repo-map/outbox.jsonl",
  "worktree_path": null
}
```

## Events

Dispatch appends:

- `task_selected`;
- `lease_acquired`;
- `worktree_created`, when a project root is supplied;
- `message_dispatched`;
- `runtime_session_started`.

Collect appends:

- `runtime_session_observed`;
- `runtime_output_received`;
- `runtime_session_stopped`;
- `validation_accepted` or `validation_rejected`;
- `backlog_updated`, when validation is accepted.

The event types match the existing blocking path so replay and state index code
continue to work.

## CLI

Add:

```text
--daemon-two-phase-worker-pool
--max-inflight <n>
```

Rules:

- requires `--daemon-run-until-idle`;
- starts the M16 static worker pool;
- uses the two-phase scheduler instead of `run_file_daemon`;
- is mutually exclusive with existing daemon mailbox worker flags;
- defaults `--max-inflight` to `2`.

The CLI output includes:

```json
{
  "daemon_status": "idle",
  "scheduler_status": "idle",
  "processed_task_ids": ["TASK-001", "TASK-002"],
  "inflight_count": 0,
  "worker_pool": {"pool_status": "stopped"}
}
```

## Why A Separate Scheduler

The current `FileScheduler.step_once()` delegates to `run_simulation()`, which
combines dispatch, runtime execution, result validation, and backlog update into
one blocking call. Splitting that in place would risk breaking the stable M0 to
M16 path.

M17 adds a side-by-side scheduler so the new lifecycle can be tested without
regressing the existing path. Later milestones can migrate or retire the
blocking path once two-phase behavior is stable.

## Acceptance

M17 is accepted when:

- two ready tasks can be dispatched before either is collected;
- both tasks can be collected from worker outboxes and marked done;
- the root event log replays into correct task and runtime session state;
- daemon CLI can run the two-phase scheduler with the static worker pool;
- existing blocking scheduler tests still pass.

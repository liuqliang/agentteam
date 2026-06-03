# M29a Worker Restart Budget And Quarantine Design

## Goal

Stop repeatedly failing resident workers from being restarted forever.

## Design

`FileMailboxWorkerPoolSupervisor` now accepts `max_restart_count`.

When a worker exits:

1. if the worker is under the budget, the pool restarts it and increments
   `restart_count`;
2. if the worker has reached the budget, the pool marks the agent as
   `quarantined`;
3. health checks and the process registry preserve `worker_status:
   quarantined` and `quarantine_reason: restart_budget_exceeded`.

The default `max_restart_count=None` preserves existing behavior: no restart
budget is enforced.

## CLI

The daemon worker-pool paths accept:

```text
--worker-max-restart-count <n>
```

The option is available for both long-running worker-pool mode and two-phase
worker-pool mode.

## Non-Goals

M29a does not reassign tasks away from a quarantined worker yet. That is the
next M29 slice.

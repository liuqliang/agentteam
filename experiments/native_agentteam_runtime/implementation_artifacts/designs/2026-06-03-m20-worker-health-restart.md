# M20 Worker Health Restart Design

Status: approved for implementation.

## Goal

M20 makes the static worker pool suitable for longer runs by detecting exited
worker processes, restarting them, and recording the health/restart state in the
worker registry.

## Scope

M20 supports:

- process-level health checks for each mailbox worker process;
- pool-level health summary;
- restart of workers that are `exited` or `not_started`;
- restart counts per worker agent;
- registry updates after start, health check, restart, and stop;
- two-phase CLI supervision that reconciles worker health between scheduler
  ticks.

M20 deliberately defers:

- exponential backoff;
- restart budgets and permanent quarantine;
- heartbeat files from worker processes;
- killing a worker that is alive but wedged inside a model call;
- task reassignment based on worker health;
- automatic task decomposition.

## Architecture

`FileMailboxWorkerProcessSupervisor` gains:

```python
worker.health()
worker.restart_if_exited()
```

`health()` uses `Popen.poll()` and returns:

```json
{
  "worker_status": "running|exited|not_started",
  "worker_pid": 123,
  "exit_code": null,
  "worker_agent_id": "agent-repo-map",
  "worker_runtime": "fake"
}
```

`restart_if_exited()` leaves running workers alone and starts a new process for
`exited` or `not_started` workers. It returns the previous health, the new start
summary, and `restart_status`.

`FileMailboxWorkerPoolSupervisor` gains:

```python
pool.health_check()
pool.restart_exited_workers()
pool.supervise_once()
```

`supervise_once()` is a compact one-tick operation: health check, restart
exited workers, health check again, write the registry, and return a summary.

## Two-Phase CLI

The `--daemon-two-phase-worker-pool` path stops using the opaque
`run_two_phase_scheduler_loop(...)` helper. Instead it creates a
`TwoPhaseFileScheduler`, starts the worker pool, and runs a small loop:

1. `worker_pool.supervise_once()`;
2. `scheduler.tick()`;
3. `worker_pool.supervise_once()`;
4. sleep only when the scheduler is waiting.

The final CLI summary includes:

```json
{
  "worker_pool_health": {...},
  "worker_pool_supervision": [...]
}
```

Existing worker-pool CLI behavior remains compatible: the final `worker_pool`
field still contains the merged start/stop summary.

## Registry

The registry remains:

```text
<output-dir>/state/worker_process_registry.json
```

M20 extends registry entries with:

- `restart_count`;
- latest `worker_status`;
- latest `exit_code`, when exited;
- latest `worker_pid`.

## Acceptance

M20 is accepted when:

- a pool health check reports all started workers as running;
- if a worker process exits unexpectedly, `restart_exited_workers()` starts a
  replacement process with a different pid and increments `restart_count`;
- the registry records restart counts;
- the two-phase CLI output includes health and supervision summaries;
- existing M16 to M19 scheduler, worker-pool, integration, retry, and timeout
  behavior still passes.

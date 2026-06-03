# M16 Static Multi-Worker Supervisor Design

Status: approved for implementation.

## Goal

M16 extends the current single long-running mailbox worker path into a static
worker pool. It starts one resident worker process per selected agent in the
agent pool, while keeping scheduler execution sequential.

This milestone is deliberately lifecycle-only. It proves that multiple local
agent processes can be started, tracked, stopped, and used through the existing
file mailbox protocol without changing the scheduler into a concurrent
dispatcher.

## Non-Goals

M16 does not implement:

- concurrent task dispatch;
- a result collector separate from scheduler step execution;
- dynamic worker scaling;
- worker restart or backoff policy;
- heartbeat timeout recovery;
- cross-agent task routing;
- merge or integration policy changes.

Those belong to later milestones. M17 should split dispatch from collect before
true task concurrency is introduced.

## Current Baseline

M15c has these properties:

- `FileMailboxWorkerProcessSupervisor` starts one serving worker process for one
  agent id.
- The daemon CLI can select that agent id through
  `--daemon-long-running-worker-agent-id`.
- `FileMailboxExternalRuntimeAdapter` waits for the selected worker to write the
  outbox result.
- The scheduler still performs one blocking runtime call per task attempt.

M16 keeps this blocking call. Starting more workers does not make scheduling
parallel yet; it only makes multiple resident agent processes available.

## Approach

Add a new focused module:

```text
agentteam_runtime/worker_pool.py
```

The module owns:

- reading `agent_pool.json`;
- selecting non-scheduler agents;
- resolving each worker's delegate runtime profile;
- starting one `FileMailboxWorkerProcessSupervisor` per selected agent;
- stopping all started workers;
- writing a process registry snapshot.

The existing `mailbox_worker.py` remains responsible for the single worker
process and delegate runtime execution. The existing `daemon.py` remains
responsible for logical scheduler ticks and the logical worker registry.

## Public API

```python
from agentteam_runtime import FileMailboxWorkerPoolSupervisor

pool = FileMailboxWorkerPoolSupervisor(
    agent_pool_path,
    output_dir,
    runtime_profile_defaults=None,
)
start = pool.start()
stop = pool.stop()
```

`start` returns:

```json
{
  "pool_status": "running",
  "worker_count": 2,
  "process_registry_path": "/tmp/run/state/worker_process_registry.json",
  "workers": [
    {
      "worker_status": "running",
      "worker_agent_id": "agent-repo-map",
      "worker_runtime": "fake",
      "worker_pid": 123
    }
  ]
}
```

`stop` returns the same shape with `pool_status: "stopped"` and stopped worker
summaries.

## Runtime Profile Rules

M16 supports only delegate runtimes already supported by
`agentteam_runtime.mailbox_worker`:

- `fake`;
- `codex`.

For each worker agent:

1. If CLI runtime defaults exist, apply them to every worker.
2. Else if the agent has `runtime_profile`, use it.
3. Else use `{"adapter": "fake"}`.

Unsupported adapters fail before starting the pool. This prevents silently
launching a worker that cannot execute its dispatches.

## CLI

Add:

```text
--daemon-long-running-worker-pool
```

Rules:

- requires `--daemon-run-until-idle`;
- mutually exclusive with `--daemon-mailbox-worker`,
  `--daemon-mailbox-subprocess-worker`, and
  `--daemon-long-running-mailbox-worker`;
- uses `FileMailboxExternalRuntimeAdapter` on the scheduler side;
- passes no runtime profile defaults to `run_file_daemon`, because the worker
  pool owns delegate runtime execution.

The single-worker flag remains available for focused experiments.

## Process Registry

M16 writes a separate process registry:

```text
<output-dir>/state/worker_process_registry.json
```

This avoids overloading the existing daemon `worker_registry.json`, which
currently records logical workers for scheduler ticks. The process registry
records actual OS process state from the supervisor.

Registry fields:

```json
{
  "registry_status": "running|stopped",
  "worker_count": 2,
  "workers": [
    {
      "worker_agent_id": "agent-repo-map",
      "worker_runtime": "fake",
      "worker_status": "running|stopped",
      "worker_pid": 123,
      "stop_file": "/tmp/run/state/workers/agent-repo-map.stop",
      "exit_code": 0
    }
  ]
}
```

## Data Flow

```text
CLI
  -> FileMailboxWorkerPoolSupervisor.start()
      -> one FileMailboxWorkerProcessSupervisor per agent
      -> worker_process_registry.json
  -> run_file_daemon(runtime_adapter=FileMailboxExternalRuntimeAdapter)
      -> scheduler step writes dispatch to selected agent inbox
      -> matching resident worker writes outbox result
      -> external adapter reads result
      -> scheduler validates attempt
  -> FileMailboxWorkerPoolSupervisor.stop()
      -> stop files
      -> worker_process_registry.json
  -> print daemon summary with worker_pool
```

## Validation

M16 should prove:

- a custom agent pool with at least two non-scheduler agents starts two worker
  processes;
- the sequential scheduler can still process a ready task through the matching
  worker;
- idle workers remain alive until daemon shutdown;
- daemon output includes `worker_pool`;
- process registry exists and records all worker ids;
- all workers stop cleanly.

## Residual Risks

M16 does not solve timeout or restart behavior. If the scheduler dispatches to
an agent whose worker failed after pool start, the external adapter waits until
timeout. M18 should address restart/backoff and dead-worker detection.

M16 also does not improve throughput because scheduler steps remain blocking.
M17 must split dispatch and result collection before `max_inflight` task
concurrency can be added.

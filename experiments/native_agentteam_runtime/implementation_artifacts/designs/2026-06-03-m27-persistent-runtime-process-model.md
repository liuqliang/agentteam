# M27 Persistent Runtime Process Model Design

Status: approved for implementation by standing roadmap authorization.

## Goal

M27 lets a worker-pool supervisor recover control-plane visibility for resident
worker processes that were started by an earlier supervisor instance.

## Scope

M27 supports:

- reading the existing file-backed `worker_process_registry.json`;
- attaching lightweight supervisor handles to worker PIDs that are still alive;
- reporting health for attached worker processes;
- stopping attached workers through their existing stop files;
- preserving the current file-backed registry model.

M27 deliberately defers:

- moving worker/session state into SQLite;
- daemonizing the scheduler itself;
- cross-host process recovery;
- process ownership authentication;
- restarting attached workers without first materializing a normal supervisor;
- long-term heartbeat files and quarantine policy.

## Architecture

The existing worker pool already writes:

```text
<output-dir>/state/worker_process_registry.json
```

M27 adds a resume path to `FileMailboxWorkerPoolSupervisor`:

```python
resumed = pool.resume_from_registry()
```

The method reads the registry, creates one `FileMailboxWorkerProcessSupervisor`
per worker row, and attaches it to the recorded PID when that PID is still
alive. The attached supervisor does not own a `subprocess.Popen` object, but it
can still:

- report `worker_status=running` if the PID exists;
- write the same stop file used by the resident worker loop;
- wait until the PID exits;
- mark the registry as stopped after a successful stop.

The lifecycle distinction becomes:

```text
logical agent availability
  != worker process health
  != current supervisor object lifetime
```

This preserves the file-first M0 runtime model while giving later daemon restarts
a path to resume worker visibility without relaunching every process.

## Acceptance

M27 is accepted when:

- a new worker-pool supervisor can resume from a registry written by an earlier
  pool instance;
- resumed health checks report the attached worker as running;
- stopping the resumed pool stops the attached process through its stop file;
- the registry records the resumed worker lifecycle;
- existing worker start/stop/restart, two-phase, and planner tests still pass.

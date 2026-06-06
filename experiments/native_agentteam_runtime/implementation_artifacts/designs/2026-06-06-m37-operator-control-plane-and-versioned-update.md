# M37 Operator Control Plane And Versioned Update Design

Status: proposed for next implementation stage.

## Goal

Make long-running AgentTeam operation understandable and controllable while
allowing the framework itself to be updated without breaking runs that are
already active.

M37 turns the current operator-facing CLI from a launch-only wrapper into a
small control plane for:

- truthful liveness reporting;
- terminal progress watching;
- safe stop and stale-state cleanup;
- project-scoped outbound notifications;
- versioned framework update for future runs.

## Problem

The current runtime can leave `scheduler_status=running` in state files even
after worker processes have stopped. Conversely, a quiet terminal does not prove
that the run is stuck. Operators must inspect raw processes, worker registries,
and JSON state by hand.

The current local launcher also points at the development worktree. If a run is
active while the framework is edited or reinstalled, newly spawned subprocesses
can see a different runtime than the one that started the run. That is unsafe
for long tasks.

## Commands

M37 defines five operator commands as one coherent surface:

```bash
agentteam status [--project-root <repo>] [--taskpack <id>] [--json]
agentteam taskpack list [--project-root <repo>] [--json]
agentteam watch [--project-root <repo>] [--taskpack <id>] [--interval 30]
agentteam stop [--project-root <repo>] [--taskpack <id>|--run-dir <dir>] [--stale]
agentteam update [--status|--activate <release>|--rollback <release>|--from <checkout>]
```

`status` and `taskpack list` remain read-only. `watch` is also read-only but
keeps the terminal alive and prints compact progress. `stop` mutates run state
and worker stop files. `update` mutates the installed framework release pointer
for future commands, not active run directories.

## Liveness Model

The operator status layer must distinguish logical scheduler state from process
health.

Public run states:

```text
running-alive
running-stale
waiting
manual-gate-required
idle
blocked
failed
stopped
unknown
```

The liveness summary reads:

- `state/two_phase_scheduler_state.json`;
- `state/worker_process_registry.json`;
- `events.jsonl` replay;
- registered worker PIDs;
- registered or discoverable child process PIDs;
- last heartbeat or worker registry update time.

`running-alive` requires at least one live worker or scheduler process, a fresh
heartbeat or active inflight lease, and no terminal stop/failure state.
`running-stale` means persisted state claims running work, but no registered
process is alive or the heartbeat is older than the configured stale threshold.

The current `taskpack list` output should use this liveness summary instead of
only echoing `scheduler_status`.

## Watch Behavior

`agentteam watch` prints one compact line at a fixed interval and immediately
prints important events.

Example periodic line:

```text
[agentteam] run=taskpack-5 state=running-alive elapsed=12m inflight=1 workers=1 running task=optimize-gesture-evaluation-pipeline
```

Immediate event lines:

```text
[agentteam] task dispatched: TASK-001
[agentteam] task completed: TASK-001 changed_files=2
[agentteam] integration blocked: verification failed
[agentteam] manual gate required: Q-001
[agentteam] run completed: 3 tasks done, 0 blocked
```

`watch` does not own the run and does not restart or stop anything. It exits
when the run reaches a terminal state unless `--follow-stale` is later added.

## Stop Behavior

`agentteam stop` is scoped to one run unless `--stale` is used.

For a live run, stop should:

1. load the run directory and worker registry;
2. write each worker stop file;
3. wait up to `--grace-seconds`;
4. terminate only registered PIDs and their descendants owned by the current
   user;
5. kill remaining registered descendants only when `--force` is provided;
6. update worker registry and scheduler state to `stopped`;
7. append a compact `run_stopped` event when event writing is available.

For stale runs, `agentteam stop --stale` does not kill processes. It updates
state from `running` to `stopped` or `running-stale-cleaned` only when the
liveness check proves no registered process is alive.

The command must never run `killall codex` or terminate unrelated Codex
sessions.

## Notification Policy

Terminal output is the detailed progress channel. Feishu is the sparse
attention channel.

Default Feishu notification events:

- run started;
- run completed;
- run failed or timed out;
- manual gate required;
- integration blocked;
- stale run detected;
- update activated or rollback activated.

Task-level completion notifications are disabled by default and can be enabled
later through a `notify_level=task` profile setting. Notification failure remains
telemetry and must not block scheduling, integration, stopping, or updating.

Feishu remains outbound-only in M37. It can notify the operator, but it does not
control the runtime.

## Versioned Update Model

`agentteam update` must not overwrite the running framework in place. It creates
side-by-side releases:

```text
~/.local/share/agentteam/releases/
  active.json
  <release-id>/
    manifest.json
    agentteam
    experiments/native_agentteam_runtime/m0_runtime/...
```

The stable launcher in `~/.local/bin/agentteam` becomes a small dispatcher. It
reads `active.json`, prepends the active release runtime path to `PYTHONPATH`,
and invokes `agentteam_runtime.agentteam`.

Release ids should default to the source Git commit hash. Dirty source checkouts
are rejected unless an explicit development flag is later added. A release
manifest records:

- release id;
- source checkout path;
- source commit;
- installed timestamp;
- runtime root;
- launcher version;
- tests or smoke checks run before activation, when available.

`agentteam start` and `agentteam continue` record the selected release id and
release root in the run state. Existing runs keep that pinned release. Updating
the active release affects only future top-level commands and future runs.

If a run was started from an unmanaged development worktree, `status` should
report:

```text
runtime_release: unmanaged-dev-worktree
```

That run can continue, but it is not protected from source edits until stopped
or restarted under a versioned release.

## Update Command Semantics

`agentteam update --status` prints:

- active release id;
- active release path;
- launcher path;
- known releases;
- active runs grouped by release id;
- unmanaged active runs, if any.

`agentteam update --from <checkout>` creates a new release from that checkout
and activates it for future commands after validation. The default checkout is
the current AgentTeam source checkout when the command is run from a development
tree.

`agentteam update --activate <release>` switches the active pointer to an
already installed release.

`agentteam update --rollback <release>` is an alias for activating an older
release and emitting rollback-specific telemetry.

Update must warn, but not fail, when active runs exist. The warning should say
that active runs stay on their pinned release. If an active run is unmanaged,
the warning should recommend waiting, stopping, or restarting under a managed
release.

## Safety Rules

- Never mutate files under an active release directory.
- Never delete a release while a run is pinned to it.
- Never change a live run's release pointer during `update`.
- Never let a worker choose the runtime release.
- Never store secrets in release manifests or run release metadata.
- Prefer read-only status checks before mutation.

## Acceptance

M37 is accepted when:

- `status` and `taskpack list` distinguish `running-alive` from
  `running-stale`;
- `watch` prints periodic progress and important event lines without changing
  run state;
- `stop` can stop a live fake worker run and can clean stale running state
  without killing unrelated Codex sessions;
- Feishu notifications cover run-level terminal events and manual gates without
  sending task-level spam by default;
- `update` installs side-by-side releases and activates a release for future
  commands without changing active run release bindings;
- active unmanaged development-worktree runs are visible as an explicit warning;
- deterministic unit tests cover all command semantics without live Codex or
  real Feishu network calls.


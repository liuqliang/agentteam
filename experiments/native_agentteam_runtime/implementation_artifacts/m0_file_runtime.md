# M0 File Runtime Implementation

Status: implemented on branch `native-runtime-m0`.

This document records the first executable slice of the native AgentTeam
runtime experiment.

For the implementation route after M22, see
`implementation_artifacts/native_runtime_roadmap.md`.

## What M0 Proves

The M0 runtime proves the local control-plane path without requiring a live
model backend, A2A, MCP, an external database service, or a persistent agent
process. The current implementation route uses Codex as the only live LLM
backend. Future API-based models such as DeepSeek or Claude Opus can be added
through adapters when credentials and result contracts are available. The M1b
slice includes a Codex process adapter so the same control-plane contract can be
exercised through `codex exec`.

Implemented path:

```text
sample backlog
  -> deterministic ready-task selection
  -> idle role-agent lookup
  -> attempt / lease / message / worktree id creation
  -> optional real git worktree creation
  -> mailbox dispatch JSONL
  -> append-only event JSONL
  -> logical runtime session start / observe / stop events
  -> fake, shell, or Codex runtime adapter result
  -> write-scope validation
  -> backlog completion event
  -> replay to task / attempt / lease / runtime session snapshot
```

The core semantic boundary is preserved:

```text
long-lived logical agent
short-lived runtime invocation
one writable attempt, one worktree
```

## Files

```text
experiments/native_agentteam_runtime/m0_runtime/
  agentteam_runtime/
    __init__.py
    cli.py
    daemon.py
    mailbox_worker.py
    m0_runtime.py
  tests/
    test_m0_runtime.py
```

## Public API

```python
from agentteam_runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    FileSchedulerDaemon,
    FileMailboxExternalRuntimeAdapter,
    FileMailboxRuntimeAdapter,
    FileMailboxSubprocessRuntimeAdapter,
    FileMailboxWorker,
    FileMailboxWorkerProcessSupervisor,
    FileMailboxWorkerPoolSupervisor,
    FileScheduler,
    ShellRuntimeAdapter,
    TwoPhaseFileScheduler,
    classify_attempt_outcome,
    list_permission_requests,
    read_scheduler_state_index,
    resolve_permission_request,
    replay_events,
    run_file_daemon,
    run_scheduler_loop,
    run_simulation,
    run_two_phase_scheduler_loop,
)

result = run_simulation(agent_pool_path, backlog_path, output_dir)
snapshot = replay_events(result["events_path"])

loop_summary = run_scheduler_loop(agent_pool_path, backlog_path, output_dir)
daemon_summary = run_file_daemon(agent_pool_path, backlog_path, output_dir)
state_index = read_scheduler_state_index(output_dir)
permission_summary = list_permission_requests(output_dir)

daemon = FileSchedulerDaemon(agent_pool_path, backlog_path, output_dir)
tick_summary = daemon.tick()

mailbox_adapter = FileMailboxRuntimeAdapter(
    agent_pool_path,
    runtime_adapter=FakeRuntimeAdapter(),
)

mailbox_worker = FileMailboxWorker(
    agent_pool_path,
    output_dir,
    "agent-repo-map",
    runtime_adapter=FakeRuntimeAdapter(),
)

mailbox_subprocess_adapter = FileMailboxSubprocessRuntimeAdapter(agent_pool_path)
mailbox_external_adapter = FileMailboxExternalRuntimeAdapter(agent_pool_path)
mailbox_supervisor = FileMailboxWorkerProcessSupervisor(
    agent_pool_path,
    output_dir,
    "agent-repo-map",
)
mailbox_pool = FileMailboxWorkerPoolSupervisor(agent_pool_path, output_dir)
two_phase = TwoPhaseFileScheduler(agent_pool_path, backlog_path, output_dir)
two_phase_summary = run_two_phase_scheduler_loop(
    agent_pool_path,
    backlog_path,
    output_dir,
    max_inflight=2,
    max_attempts=2,
    lease_timeout_seconds=900,
)

result_with_worktree = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=FakeRuntimeAdapter(),
)

result_with_shell = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=ShellRuntimeAdapter(["python3", "/path/to/worker.py"]),
)

result_with_codex = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=CodexRuntimeAdapter(),
)

outcome = classify_attempt_outcome(
    {"result_status": "timed_out", "changed_files": [], "output": {}},
    {"write_scope": ["generated/"]},
)
permission_resolution = resolve_permission_request(
    output_dir,
    "PERM-TASK-001-ATTEMPT-001",
    "approved",
    operator="operator",
    reason="Allow one bounded retry.",
)
```

`run_simulation` writes:

- `events.jsonl`
- `mailboxes/<agent-id>/inbox.jsonl`

The returned summary includes:

- `task_id`
- `attempt_id`
- `lease_id`
- `message_id`
- `runtime_session_id`
- `runtime_session_status`
- `worktree_id`
- `worktree_path`, when `project_root` is provided
- `branch`, when `project_root` is provided
- `validation_status`
- `failure_category`
- `retryable`
- `diff_audit`
- `patch_path`
- `integration_status`
- `integration_branch`
- `integration_worktree_path`
- `integration_verification_status`
- `integration_verification_exit_code`
- `integration_verification_stdout`
- `integration_verification_stderr`
- `integration_commit_status`
- `integration_commit_sha`
- `integration_commit_message`
- `integration_commit_reason`
- `integration_commit_stdout`
- `integration_commit_stderr`
- `attempt_count`
- `attempts`
- `worktree_removed`
- output file paths

## CLI

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m0-run \
  --project-root /path/to/git/repo
```

The CLI prints one JSON summary containing the simulation result and replayed
snapshot.

To run the file scheduler loop until no ready tasks remain, pass
`--run-until-idle`:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m7c-run \
  --run-until-idle
```

The loop CLI prints the scheduler summary:

```json
{
  "scheduler_status": "idle",
  "processed_task_ids": ["TASK-001", "TASK-002"],
  "step_count": 2,
  "events_path": "/tmp/agentteam-m7c-run/events.jsonl",
  "state_path": "/tmp/agentteam-m7c-run/state/scheduler_state.json",
  "state_db_path": "/tmp/agentteam-m7c-run/state/scheduler_state.sqlite",
  "snapshot": {
    "tasks": {
      "TASK-001": {"task_status": "done"},
      "TASK-002": {"task_status": "done"}
    }
  }
}
```

Use `--max-steps <n>` with `--run-until-idle` to cap the number of scheduler
steps. The default CLI path remains single-task and still prints the replayed
snapshot. The loop path also prints a snapshot replayed from the canonical root
`events.jsonl`.

To run through the file daemon facade, pass `--daemon-run-until-idle`:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m14a-daemon-run \
  --daemon-run-until-idle
```

The daemon path prints the same replayed scheduler snapshot plus a worker
registry path:

```json
{
  "daemon_status": "idle",
  "processed_task_ids": ["TASK-001", "TASK-002"],
  "step_count": 2,
  "tick_count": 3,
  "worker_registry_path": "/tmp/agentteam-m14a-daemon-run/state/worker_registry.json"
}
```

`--daemon-run-until-idle` and `--run-until-idle` are mutually exclusive. The
daemon path currently reuses `--max-steps` as its tick budget.

To exercise the file mailbox worker bridge, add `--daemon-mailbox-worker`:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m14b-mailbox-run \
  --daemon-run-until-idle \
  --daemon-mailbox-worker
```

In M14b this flag uses a fake delegate runtime behind
`FileMailboxRuntimeAdapter`. Runtime command overrides and Codex options are
rejected for this flag until a real worker-process supervisor is added.

To exercise the one-shot subprocess worker bridge, use
`--daemon-mailbox-subprocess-worker` instead:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m14c-subprocess-run \
  --daemon-run-until-idle \
  --daemon-mailbox-subprocess-worker
```

This flag launches one worker subprocess per dispatch through
`FileMailboxSubprocessRuntimeAdapter`. It is mutually exclusive with
`--daemon-mailbox-worker`.

To exercise one long-running fake mailbox worker for the daemon run, use
`--daemon-long-running-mailbox-worker`:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m15a-long-worker-run \
  --daemon-run-until-idle \
  --daemon-long-running-mailbox-worker \
  --daemon-long-running-worker-agent-id agent-repo-map
```

This starts one serving worker process for the configured agent id, runs the
daemon with `FileMailboxExternalRuntimeAdapter`, then stops the worker before
printing the summary. It is mutually exclusive with the M14b/M14c mailbox
worker flags.

In M15b the same flag can run the serving worker with a Codex delegate:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m15b-long-codex-worker-run \
  --project-root /path/to/git/repo \
  --daemon-run-until-idle \
  --daemon-long-running-mailbox-worker \
  --daemon-long-running-worker-agent-id agent-repo-map \
  --runtime codex
```

The normal Codex CLI options, such as `--codex-command`, `--codex-model`,
`--codex-sandbox`, and `--codex-timeout-seconds`, are passed to the resident
worker process. The scheduler side still records
`FileMailboxExternalRuntimeAdapter` because it waits for mailbox results rather
than directly invoking Codex.

In M16 the daemon can start one long-running worker process per non-scheduler
agent in the agent pool:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m16-static-worker-pool-run \
  --daemon-run-until-idle \
  --daemon-long-running-worker-pool
```

This starts a static worker pool and writes
`<output-dir>/state/worker_process_registry.json`. The scheduler remains
sequential; the pool proves resident multi-process lifecycle, not concurrent
task dispatch.

In M17 the daemon can run the two-phase scheduler with the static worker pool:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m17-two-phase-worker-pool-run \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --max-inflight 2 \
  --max-attempts 2 \
  --lease-timeout-seconds 900
```

This path dispatches ready tasks up to `--max-inflight` without waiting for
runtime completion, then collects mailbox results in later ticks. It writes
`<output-dir>/state/two_phase_scheduler_state.json` plus the canonical root
`events.jsonl` and rebuildable SQLite index.

To inspect a completed or partially completed scheduler run without re-running
the scheduler, pass `--show-state-index` with only the output directory:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --output-dir /tmp/agentteam-m7c-run \
  --show-state-index
```

This prints a JSON summary read from
`<output-dir>/state/scheduler_state.sqlite`:

```json
{
  "event_count": 18,
  "events_path": "/tmp/agentteam-m7c-run/events.jsonl",
  "latest_event": {"event_type": "backlog_updated", "task_id": "TASK-002"},
  "state_db_path": "/tmp/agentteam-m7c-run/state/scheduler_state.sqlite",
  "tasks": [
    {"task_id": "TASK-001", "task_status": "done"},
    {"task_id": "TASK-002", "task_status": "done"}
  ]
}
```

If the SQLite file is missing or stale but the canonical root `events.jsonl`
exists, the CLI rebuilds the index from JSONL before printing the summary.
`--agent-pool` and `--backlog` are not required for this inspection mode.

To run a real local process through the shell adapter, put `--shell-command`
last:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m0-run \
  --project-root /path/to/git/repo \
  --shell-command python3 /path/to/worker.py
```

To apply an accepted patch artifact into an isolated integration worktree, pass
`--integrate-accepted-patch` before the final runtime command:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m4-run \
  --project-root /path/to/git/repo \
  --integrate-accepted-patch \
  --shell-command python3 /path/to/worker.py
```

To verify and then commit the integration worktree as a checkpoint, pass the
verification command as a JSON string array before the final runtime command:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m6-run \
  --project-root /path/to/git/repo \
  --integrate-accepted-patch \
  --integration-verification-command-json '["python3","-m","unittest","discover"]' \
  --commit-verified-integration \
  --shell-command python3 /path/to/worker.py
```

`--commit-verified-integration` never merges the source branch. It commits only
inside `agentteam/integration/<task-id>` after the integration verification
command exits 0.

The shell command receives the mailbox message as JSON on stdin. It must print
one JSON result to stdout:

```json
{
  "result_status": "completed",
  "changed_files": ["generated/result.json"],
  "output": {"adapter": "shell"}
}
```

Non-zero exit codes, invalid stdout JSON, timeouts, and changed files outside
the task `write_scope` produce rejected results.

To run through the Codex adapter, use `--runtime codex`. The default API
command is `codex exec`. In M12b this selector requires `--project-root`
because `CodexRuntimeAdapter` must run inside a git worktree:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m0-run \
  --project-root /path/to/git/repo \
  --runtime codex \
  --codex-model gpt-5.4 \
  --codex-sandbox workspace-write \
  --codex-timeout-seconds 300
```

`--codex-command` remains available as a command-prefix override for tests and
experiments. When no `--runtime` is supplied, the CLI preserves the old
inference behavior: `--shell-command` selects `ShellRuntimeAdapter`,
`--codex-command` selects `CodexRuntimeAdapter`, and no command override selects
the fake adapter. The inferred Codex path also requires `--project-root`.

M12c exposes three Codex runtime controls:

- `--codex-model`: passes `-m <model>` to `codex exec`;
- `--codex-sandbox`: passes `-s <sandbox>`, defaulting to `workspace-write`;
- `--codex-timeout-seconds`: sets the adapter subprocess timeout, defaulting to
  `300`.

M13a lets the selected agent override those CLI fallback values through
`agent_pool.agents[].runtime_profile`:

```json
{
  "agent_id": "agent-implementation",
  "role": "worker_agent",
  "status": "idle",
  "model_profile": "coding-l1",
  "runtime_adapter": "codex",
  "runtime_profile": {
    "adapter": "codex",
    "model": "gpt-5.4",
    "sandbox": "workspace-write",
    "timeout_seconds": 300
  }
}
```

M13c moves this resolution into runtime core instead of the CLI. The
dispatch-time precedence is:

1. explicit `runtime_adapter_factory`;
2. explicit Python `runtime_adapter`;
3. the selected agent's `runtime_profile`;
4. caller/CLI runtime defaults as fallback;
5. `FakeRuntimeAdapter` when neither profile nor fallback is supplied.

For tests and local experiments, `--codex-command` remains a command override
even when the selected agent has a Codex profile. This allows fake Codex
commands to verify profile model/sandbox forwarding without making a live model
call. The CLI now only converts global flags into fallback defaults and passes
them to `run_simulation(...)` or `run_scheduler_loop(...)`; it does not interpret
the selected agent profile itself.

`CodexRuntimeAdapter` invokes the command as:

```text
<command> -C <worktree> -s workspace-write --output-last-message <result.json> -
```

The prompt is passed on stdin through the final `-`. Codex must write its final
answer to the `--output-last-message` file as one JSON object:

```json
{
  "result_status": "completed",
  "changed_files": ["generated/result.json"],
  "output": {"adapter": "codex"}
}
```

If the Codex process exits non-zero with a clear sandbox or permission failure,
the adapter returns `result_status: "blocked"` with
`output.permission_request`. The two-phase scheduler turns that into a durable
`permission_request_required` event, blocks the task with a `PERM-...` request
id, and resumes only after `resolve_permission_request(..., "approved")` or the
CLI equivalent records an approval.

The unit tests use a fake Codex command that implements this CLI contract. They
do not perform a live model invocation.

## Live Codex Smoke

M1c adds a gated smoke-test entrypoint for one real Codex invocation:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke \
  --output-dir /tmp/agentteam-live-codex-smoke
```

By default, without `AGENTTEAM_RUN_LIVE_CODEX=1`, the command exits
successfully with:

```json
{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}
```

When enabled, it creates a temporary git repository, writes a minimal L0 backlog
item, runs `CodexRuntimeAdapter`, and requires Codex to create:

```text
generated/live_codex_smoke.json
```

The smoke command exits non-zero unless the scheduler validation is accepted,
the runtime result reports that exact file in `changed_files`, and the file
exists in the attempt worktree.

Local verification on 2026-06-01 with `codex-cli 0.132.0` completed this path:

```json
{
  "changed_files": ["generated/live_codex_smoke.json"],
  "expected_file_exists": true,
  "status": "completed",
  "validation_status": "accepted"
}
```

For deterministic local tests, the same entrypoint accepts a fake command:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke \
  --output-dir /tmp/agentteam-live-codex-smoke \
  --codex-command python3 /path/to/fake_codex.py
```

M12a adds a scheduler-level live Codex smoke. It uses the same
`AGENTTEAM_RUN_LIVE_CODEX=1` gate, but exercises the current scheduler loop,
canonical root event log, SQLite state index, and runtime session tracking:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_scheduler_smoke \
  --output-dir /tmp/agentteam-live-codex-scheduler-smoke
```

When enabled, it creates a temporary git repository, writes one L0 scheduler
backlog item, runs `run_scheduler_loop(...)` through `CodexRuntimeAdapter`, and
requires Codex to create:

```text
generated/live_codex_scheduler_smoke.json
```

The command exits non-zero unless:

- the scheduler reaches `idle`;
- the processed task id is `TASK-LIVE-CODEX-SCHEDULER-SMOKE`;
- validation is accepted;
- the expected file exists in the attempt worktree;
- the SQLite state index reports the task as `done`;
- the runtime session row is `stopped`.

For deterministic tests, it also accepts a fake command:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_scheduler_smoke \
  --output-dir /tmp/agentteam-live-codex-scheduler-smoke \
  --codex-command python3 /path/to/fake_codex.py
```

Local verification on 2026-06-02 with `codex-cli 0.132.0` completed this
scheduler path:

```json
{
  "changed_files": ["generated/live_codex_scheduler_smoke.json"],
  "expected_file_exists": true,
  "processed_task_ids": ["TASK-LIVE-CODEX-SCHEDULER-SMOKE"],
  "scheduler_status": "idle",
  "status": "completed"
}
```

M12b adds a CLI-level live Codex smoke for the preferred runtime selector. This
entrypoint uses the same gate, but calls the public CLI in a subprocess:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_cli_smoke \
  --output-dir /tmp/agentteam-live-codex-cli-smoke
```

Internally it runs:

```bash
python3 -m agentteam_runtime.cli \
  --agent-pool <generated-agent-pool.json> \
  --backlog <generated-backlog.json> \
  --output-dir <run-dir> \
  --project-root <temporary-git-repo> \
  --run-until-idle \
  --runtime codex
```

Then it queries:

```bash
python3 -m agentteam_runtime.cli \
  --output-dir <run-dir> \
  --show-state-index
```

The command exits non-zero unless the scheduler accepts the task, Codex creates
`generated/live_codex_cli_smoke.json`, the state index reports the task as
`done`, and the runtime session row records `CodexRuntimeAdapter` as `stopped`.

Local verification on 2026-06-02 with `codex-cli 0.132.0` completed this CLI
path:

```json
{
  "changed_files": ["generated/live_codex_cli_smoke.json"],
  "expected_file_exists": true,
  "processed_task_ids": ["TASK-LIVE-CODEX-CLI-SMOKE"],
  "scheduler_status": "idle",
  "status": "completed"
}
```

## M2 Attempt Management

M2 adds the first managed execution-attempt mechanics while preserving the
default one-attempt behavior:

```python
result = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=CodexRuntimeAdapter(),
    max_attempts=2,
    cleanup_accepted_worktrees=True,
)
```

`classify_attempt_outcome(runtime_result, task)` returns:

```json
{
  "validation_status": "accepted",
  "failure_category": null,
  "retryable": false
}
```

Current classification rules:

- accepted: `result_status == "completed"` and all `changed_files` are inside
  `write_scope`;
- `scope_violation`: completed result with out-of-scope files, not retryable;
- `timeout`: `result_status == "timed_out"`, retryable;
- `blocked` or `cancelled`: not retryable;
- `runtime_error`: all other failed results, retryable.

When `max_attempts > 1`, retryable rejected attempts emit `recovery_routed` and
the next attempt receives a new attempt/lease/message/worktree id:

```text
ATTEMPT-001, LEASE-001, MSG-0001, WT-ATTEMPT-001
ATTEMPT-002, LEASE-002, MSG-0002, WT-ATTEMPT-002
```

Accepted worktrees are kept by default for inspection. If
`cleanup_accepted_worktrees=True`, the scheduler removes only the accepted
attempt worktree with `git worktree remove --force` and emits
`worktree_removed`. Rejected attempt worktrees are still retained so failures
can be inspected.

## M3a Worktree Diff Audit

M3a adds a compact git diff audit before accepting worktree-backed attempts.
The runtime no longer accepts a successful JSON result solely because
`changed_files` is syntactically inside `write_scope`; it also checks that the
attempt worktree really contains the declared changes.

```python
audit = audit_worktree_diff(
    worktree_path,
    ["generated/result.json"],
)
```

The audit has this shape:

```json
{
  "diff_status": "matched",
  "declared_changed_files": ["generated/result.json"],
  "actual_changed_files": ["generated/result.json"],
  "missing_declared_files": [],
  "undeclared_changed_files": []
}
```

If ordinary validation would accept but `diff_status == "mismatch"`, the
attempt is rejected with:

```json
{
  "failure_category": "diff_mismatch",
  "retryable": false,
  "validation_status": "rejected"
}
```

The audit reads `git status --porcelain=v1 --untracked-files=all` in the attempt
worktree. Runtime-private files under `.agentteam/`, such as Codex
`--output-last-message` result files, are ignored because they are control-plane
artifacts rather than user patch content.

M3a does not integrate patches back into the source repository. It only records
whether the worktree diff is internally consistent with the runtime result.

## M3b Patch Artifact Capture

M3b persists the audited worktree diff as a patch artifact:

```text
<output-dir>/attempts/<attempt-id>/worktree.patch
```

The path is returned as `patch_path` on both the final result and the individual
attempt entry. It is also included in validation replay state.

Patch capture is intentionally separate from patch integration:

- tracked modifications and deletions come from
  `git diff --binary --no-ext-diff HEAD -- <paths>`;
- untracked additions come from
  `git diff --binary --no-ext-diff --no-index -- /dev/null <path>`;
- `.agentteam/` runtime-private files remain excluded;
- the patch is not applied, committed, or merged back to the source repository.

This gives the scheduler an auditable artifact for later review without
choosing an automatic integration policy yet.

## M4 Integration Branch Apply

M4 adds an explicit integration worktree step:

```python
result = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=ShellRuntimeAdapter(["python3", "/path/to/worker.py"]),
    integrate_accepted_patch=True,
)
```

When an accepted attempt has a patch artifact, the scheduler:

- creates `output_dir/integration/<task-id>`;
- creates branch `agentteam/integration/<task-id>`;
- runs `git apply <patch_path>` inside the integration worktree;
- emits `patch_integrated`;
- returns `integration_status`, `integration_branch`, and
  `integration_worktree_path`.

M4 deliberately does not commit, push, merge, or update the source repository's
main branch. The integration worktree HEAD remains equal to source `HEAD`; the
patch exists as unstaged working-tree changes for later verification and merge
policy.

## M5 Integration Verification

M5 adds an explicit verification command for the integration worktree:

```python
result = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=ShellRuntimeAdapter(["python3", "/path/to/worker.py"]),
    integrate_accepted_patch=True,
    integration_verification_command=[
        "python3",
        "-m",
        "unittest",
        "discover",
    ],
)
```

The command runs only after an accepted patch has been applied to the integration
worktree. It returns:

```json
{
  "integration_verification_status": "passed",
  "integration_verification_exit_code": 0,
  "integration_verification_stdout": "",
  "integration_verification_stderr": ""
}
```

If the command exits non-zero, the status is `failed`, but the underlying
implementation attempt remains `accepted`. This keeps code validation,
integration application, and integration verification as separate gates for a
future merge controller.

M5 still does not commit, push, or merge.

## M6 Verified Integration Commit Gate

M6 adds an opt-in checkpoint after M5 verification:

```python
result = run_simulation(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root="/path/to/git/repo",
    runtime_adapter=ShellRuntimeAdapter(["python3", "/path/to/worker.py"]),
    integrate_accepted_patch=True,
    integration_verification_command=["python3", "-m", "unittest", "discover"],
    commit_verified_integration=True,
)
```

The scheduler commits only when all of these are true:

- the implementation attempt was accepted;
- a patch artifact was applied to an integration worktree;
- an integration verification command was requested;
- that verification command exited 0;
- `commit_verified_integration=True`.

The result includes:

```json
{
  "integration_commit_status": "committed",
  "integration_commit_sha": "<sha>",
  "integration_commit_message": "AgentTeam integration TASK-001 ATTEMPT-001",
  "integration_commit_reason": null,
  "integration_commit_stdout": "",
  "integration_commit_stderr": ""
}
```

If the gate is requested but verification is missing or failed, the commit is
skipped:

```json
{
  "integration_commit_status": "skipped",
  "integration_commit_sha": null,
  "integration_commit_reason": "verification_failed"
}
```

This is intentionally not a merge policy. The commit is a local, auditable
checkpoint on the integration branch. Merging back to the source branch remains
a later full-task/system gate after all parts of the functional change have
been integrated and verified.

## M7a File Scheduler Loop

M7a adds the first persistent scheduler loop facade:

```python
summary = run_scheduler_loop(
    agent_pool_path,
    backlog_path,
    output_dir,
    runtime_adapter=FakeRuntimeAdapter(),
)
```

It is equivalent to:

```python
scheduler = FileScheduler(agent_pool_path, backlog_path, output_dir)
summary = scheduler.run_until_idle()
```

The loop repeatedly selects the next ready task, delegates that single task to
the existing `run_simulation(...)` path, updates backlog state, and writes:

```text
<output-dir>/state/scheduler_state.json
<output-dir>/events.jsonl
<output-dir>/steps/STEP-0001-<task-id>/
<output-dir>/steps/STEP-0002-<task-id>/
```

The summary shape is:

```json
{
  "scheduler_status": "idle",
  "processed_task_ids": ["TASK-001", "TASK-002"],
  "step_count": 2,
  "events_path": "<output-dir>/events.jsonl",
  "state_path": "<output-dir>/state/scheduler_state.json"
}
```

Task readiness is deterministic. A task is selectable only when:

- `backlog_status == "ready"`;
- it has no `blockers`;
- every task in `depends_on` is already `done`.

Accepted task results set the persisted backlog item to `done`. Rejected
results set it to `blocked` with a compact blocker reason.

If `<output-dir>/state/scheduler_state.json` already exists, `FileScheduler`
loads it and resumes from the persisted backlog/step state. Re-running the loop
with the same output directory does not repeat tasks already marked `done`.

When `FileScheduler` delegates a task to `run_simulation(...)`, it namespaces
attempt, worktree, lease, and mailbox message ids by task id. This keeps
worktree-backed multi-step runs from reusing the same git branch and keeps
canonical replay from collapsing multiple steps into one lease key:

```text
TASK-001-ATTEMPT-001 -> agentteam/TASK-001-ATTEMPT-001
TASK-001-LEASE-001
TASK-001-MSG-0001
TASK-002-ATTEMPT-001 -> agentteam/TASK-002-ATTEMPT-001
TASK-002-LEASE-001
TASK-002-MSG-0001
```

Plain `run_simulation(...)` keeps the existing default ids such as
`ATTEMPT-001`, `WT-ATTEMPT-001`, `LEASE-001`, `MSG-0001`, and
`agentteam/ATTEMPT-001`.

M8a makes `<output-dir>/events.jsonl` the canonical replay source for scheduler
loop runs. Each processed step still keeps its local event log, but the
scheduler copies those events into the root log with global `sequence` and
`event_id` values. Canonical events also carry:

```text
run_id
step_id
source_event_id
source_event_sequence
```

The original step event payload is unchanged, so `replay_events(...)` can read
the root log and reconstruct multi-task scheduler loop state from one file.

M9a adds a SQLite query index for scheduler-loop runs:

```text
<output-dir>/state/scheduler_state.sqlite
```

The SQLite database is not an authority. It is rebuilt from the canonical root
`events.jsonl` after each processed scheduler step, so the JSONL event stream
remains the audit log and replay source. The index exists to make routine state
queries cheap without forcing callers to parse the whole JSONL file every time.

Current index tables:

```text
tasks(task_id, task_status)
attempts(attempt_id, task_id, attempt_status, validation_status)
leases(lease_id, task_id, attempt_id, lease_status)
runtime_sessions(runtime_session_id, task_id, attempt_id, lease_id, session_status, result_status, runtime_adapter, changed_file_count)
events(sequence, event_id, event_type, task_id, attempt_id, lease_id, step_id, time)
```

M13b extends `runtime_sessions` with effective runtime config metadata:

```text
runtime_model
runtime_sandbox
runtime_timeout_seconds
```

The scheduler summary includes both state paths:

```json
{
  "events_path": "<output-dir>/events.jsonl",
  "state_path": "<output-dir>/state/scheduler_state.json",
  "state_db_path": "<output-dir>/state/scheduler_state.sqlite"
}
```

M9b adds a read-only query path over this index:

```python
state = read_scheduler_state_index(output_dir)
```

It returns sorted `tasks`, `attempts`, and `leases`, plus `event_count` and
`runtime_sessions`, `event_count`, and `latest_event`. If the SQLite index is
missing, if required tables are missing, or if its indexed event count does not
match the canonical root `events.jsonl`, it is rebuilt from JSONL before the
summary is returned. This is an observability path only; callers still treat
JSONL as the source of truth.

M9c makes that repair behavior explicit for stale indexes. This covers the
crash-recovery case where the scheduler appended canonical events but stopped
before the SQLite query index was refreshed.

## Artifact Lint

M10a adds a local artifact lint command:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint \
  --root experiments/native_agentteam_runtime
```

The command scans the root for JSON and JSONL files, parses every file, checks
JSONL `event_type` values against `schemas/event.schema.json` when that schema
is available, verifies event records have the required event fields, and checks
that event `sequence` values increase by 1 within each event JSONL file. It
prints a JSON summary:

```json
{
  "checked_json_files": 21,
  "checked_jsonl_files": 1,
  "errors": [],
  "root_path": "experiments/native_agentteam_runtime",
  "status": "passed"
}
```

This is a lightweight executable lint, not a full JSON Schema validator. It
exists so implementation loops have a cheap artifact sanity check before a
larger schema-validation engine is introduced.

M10b strengthens this lint for event logs. It reports:

```text
missing_event_fields
non_monotonic_event_sequence
```

These checks are intentionally local and mechanical. They catch broken event
logs before replay or SQLite indexing has to interpret them.

## Runtime Sessions

M11a adds logical runtime session lifecycle tracking around each runtime adapter
invocation. Every attempt receives a deterministic session id:

```text
SESSION-ATTEMPT-001
SESSION-TASK-001-ATTEMPT-001
```

The event log records:

```text
runtime_session_started
runtime_session_observed
runtime_session_stopped
runtime_output_received
```

`runtime_session_started` is emitted before the adapter is invoked,
`runtime_session_observed` records the adapter result status and changed-file
count, and `runtime_session_stopped` marks the logical session as stopped before
validation. The existing `runtime_output_received` event still carries the full
runtime result payload used by validation.

The returned attempt summary includes:

```json
{
  "runtime_session_id": "SESSION-ATTEMPT-001",
  "runtime_session_status": "stopped"
}
```

`replay_events(...)` now reconstructs a `runtime_sessions` snapshot. This is
still a synchronous logical lifecycle. It does not yet keep Codex, Claude Code,
or another worker process alive across multiple tasks.

M11b stores those replayed runtime sessions in the SQLite state index. The
state-index query output includes one row per runtime session:

```json
{
  "runtime_session_id": "SESSION-TASK-001-ATTEMPT-001",
  "task_id": "TASK-001",
  "attempt_id": "TASK-001-ATTEMPT-001",
  "lease_id": "TASK-001-LEASE-001",
  "session_status": "stopped",
  "result_status": "completed",
  "runtime_adapter": "FakeRuntimeAdapter",
  "changed_file_count": 1,
  "runtime_model": null,
  "runtime_sandbox": null,
  "runtime_timeout_seconds": null
}
```

If an older SQLite index has matching event counts but lacks the
`runtime_sessions` table, or if it lacks M13b runtime config columns, the read
path treats it as stale and rebuilds it from canonical JSONL.

The scheduler loop is still intentionally sequential. Even with M9a's SQLite
query index, JSONL remains the authority; the loop does not add concurrent
workers, authoritative database storage, a supervised daemon process,
long-lived Codex/Claude sessions, or merge-to-main.

## M14a File Daemon Worker Registry

M14a adds a file-backed daemon facade:

```python
daemon = FileSchedulerDaemon(
    agent_pool_path,
    backlog_path,
    output_dir,
    runtime_adapter=FakeRuntimeAdapter(),
)
tick_summary = daemon.tick()
summary = daemon.run_until_idle()
```

The daemon owns:

```text
<output-dir>/state/worker_registry.json
```

Each tick refreshes the registry heartbeat for every non-scheduler agent from
`agent_pool`, then delegates one scheduler step through `FileScheduler.step_once()`.
The registry shape is intentionally compact:

```json
{
  "registry_status": "active",
  "tick_count": 1,
  "workers": [
    {
      "worker_id": "WORKER-agent-repo-map",
      "agent_id": "agent-repo-map",
      "role": "repo_map_agent",
      "worker_status": "idle",
      "runtime_adapter": "manual",
      "runtime_profile": null,
      "active_task_id": null,
      "last_heartbeat": "2026-05-31T00:00:00Z"
    }
  ]
}
```

This is the first daemon-shaped control-plane experiment, not a worker process
supervisor. It does not fork or keep Codex, Claude Code, or shell workers alive.
The registry is durable state for observing intended long-lived agents while the
actual runtime invocation remains the existing short adapter call inside each
scheduler step.

## M14b File Mailbox Worker Runtime

M14b adds a mailbox-polling worker protocol without changing the scheduler
validation contract:

```python
adapter = FileMailboxRuntimeAdapter(
    agent_pool_path,
    runtime_adapter=FakeRuntimeAdapter(),
)

summary = run_scheduler_loop(
    agent_pool_path,
    backlog_path,
    output_dir,
    runtime_adapter=adapter,
)
```

`run_simulation(...)` still writes the dispatch message to:

```text
<step-dir>/mailboxes/<agent-id>/inbox.jsonl
```

`FileMailboxRuntimeAdapter` binds itself to the current step output directory,
creates a `FileMailboxWorker`, lets it poll the matching dispatch message, and
then reads the matching `runtime_result` from:

```text
<step-dir>/mailboxes/<agent-id>/outbox.jsonl
```

The outbox message has this shape:

```json
{
  "message_id": "RESULT-TASK-001-MSG-0001",
  "from_agent": "agent-repo-map",
  "to_agent": "agent-scheduler",
  "message_type": "runtime_result",
  "correlation_id": "TASK-001:TASK-001-ATTEMPT-001",
  "created_at": "2026-06-03T00:00:00Z",
  "payload": {
    "source_message_id": "TASK-001-MSG-0001",
    "task_id": "TASK-001",
    "attempt_id": "TASK-001-ATTEMPT-001",
    "lease_id": "TASK-001-LEASE-001",
    "result_status": "completed",
    "changed_files": ["generated/m0_generated_repo_index.json"],
    "output": {"adapter": "fake"}
  }
}
```

This establishes the file protocol for future long-lived worker processes while
keeping M14b deliberately small. The worker still runs in-process through the
adapter bridge, one scheduler step at a time. It does not yet spawn, supervise,
restart, or communicate with an independent OS worker process.

## M14c One-Shot Mailbox Subprocess Worker

M14c adds a real OS subprocess boundary for the mailbox worker:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.mailbox_worker \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --output-dir /tmp/agentteam-step \
  --agent-id agent-repo-map \
  --message-id TASK-001-MSG-0001 \
  --runtime fake
```

The worker CLI polls exactly one dispatch message, writes one `runtime_result`
to the selected agent outbox, prints a JSON summary, and exits:

```json
{
  "poll_status": "processed",
  "source_message_id": "TASK-001-MSG-0001",
  "result_status": "completed",
  "changed_files": ["generated/m0_generated_repo_index.json"],
  "outbox_path": "/tmp/agentteam-step/mailboxes/agent-repo-map/outbox.jsonl",
  "worker_pid": 12345
}
```

`FileMailboxSubprocessRuntimeAdapter` preserves the scheduler runtime adapter
interface. On each dispatch it launches:

```text
python -m agentteam_runtime.mailbox_worker --agent-pool ... --output-dir ... --agent-id ... --message-id ... --runtime fake
```

The adapter then reads the matching outbox `runtime_result` and attaches
subprocess metadata to the runtime output:

```json
{
  "adapter": "fake",
  "mailbox_subprocess": {
    "worker_pid": 12345,
    "exit_code": 0,
    "stdout": "{\"poll_status\":\"processed\",...}"
  }
}
```

This validates process launch, PID reporting, timeout handling, stdout parsing,
and file-mailbox result recovery. It is still one subprocess per dispatch. It
does not keep workers resident, restart failed workers, multiplex multiple
agents concurrently, or define backoff policy.

## M15a Long-Running Fake Mailbox Worker

M15a adds the first resident worker-process path. The worker CLI can run in
serve mode:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.mailbox_worker \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --output-dir /tmp/agentteam-run \
  --agent-id agent-repo-map \
  --runtime fake \
  --serve \
  --poll-interval-seconds 0.05 \
  --stop-file /tmp/agentteam-run/state/workers/agent-repo-map.stop
```

In serve mode the worker scans:

```text
<output-dir>/mailboxes/<agent-id>/inbox.jsonl
<output-dir>/steps/*/mailboxes/<agent-id>/inbox.jsonl
```

It processes the first unanswered dispatch it finds, writes the matching
`runtime_result`, and continues polling until the stop file appears.

`FileMailboxWorkerProcessSupervisor` owns the process lifecycle:

```python
supervisor = FileMailboxWorkerProcessSupervisor(
    agent_pool_path,
    output_dir,
    "agent-repo-map",
)
start = supervisor.start()
stop = supervisor.stop()
```

`FileMailboxExternalRuntimeAdapter` is the scheduler-side counterpart. It does
not start a process. For each runtime call it binds to the current step output
directory and waits for the long-running worker to write the matching outbox
result:

```python
summary = run_scheduler_loop(
    agent_pool_path,
    backlog_path,
    output_dir,
    runtime_adapter=FileMailboxExternalRuntimeAdapter(agent_pool_path),
)
```

M15a is intentionally narrow: one fake worker process, one agent id, sequential
scheduler execution, and explicit stop-file shutdown. It does not yet supervise
multiple workers, restart failed workers, allocate tasks across a pool, or run
Codex/Claude as resident processes.

## M15b Codex Long-Running Mailbox Worker

M15b keeps the M15a resident worker topology but allows the worker delegate
runtime to be Codex. The worker CLI now accepts:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.mailbox_worker \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --output-dir /tmp/agentteam-step \
  --agent-id agent-repo-map \
  --message-id TASK-001-MSG-0001 \
  --runtime codex \
  --codex-command-json '["codex","exec"]' \
  --codex-sandbox workspace-write \
  --codex-timeout-seconds 300
```

For one-shot or serving mode, the worker gets the writable worktree from the
dispatch payload when `--worktree-path` is not explicitly provided:

```json
{
  "payload": {
    "worktree_path": "/tmp/agentteam-run/steps/STEP-0001-TASK-001/worktrees/WT-TASK-001-ATTEMPT-001"
  }
}
```

The daemon CLI translates existing Codex options into supervisor settings. The
supervisor launches one process with `--runtime codex`; the worker then executes
each dispatch through `CodexRuntimeAdapter`, writes the normal
`runtime_result` outbox message, and keeps serving until the stop file appears.

M15b still has these limits:

- one resident worker process;
- one configured worker agent id per daemon CLI run;
- sequential scheduler execution;
- no worker restart/backoff;
- no multi-agent worker pool;
- no Claude delegate runtime.

## M15c Configurable Long-Worker Agent Id

M15c removes the hardcoded daemon CLI worker id. The default remains
`agent-repo-map` for fixture compatibility, but callers can select another
agent id from their agent pool:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m15c-worker-agent-id-run \
  --daemon-run-until-idle \
  --daemon-long-running-mailbox-worker \
  --daemon-long-running-worker-agent-id agent-custom-map
```

This option configures only which single mailbox worker process is started. It
does not add a worker pool or route multiple agents concurrently. The configured
worker id must match the agent selected by the scheduler for the ready task;
otherwise the external adapter waits on one outbox while the running worker
serves a different mailbox.

## M16 Static Multi-Worker Supervisor

M16 adds `FileMailboxWorkerPoolSupervisor`, a lifecycle wrapper around multiple
`FileMailboxWorkerProcessSupervisor` instances:

```python
pool = FileMailboxWorkerPoolSupervisor(agent_pool_path, output_dir)
start = pool.start()
stop = pool.stop()
```

The pool reads the agent pool, skips the scheduler agent, and starts one serving
worker process per remaining agent. Each worker serves only its own mailbox.
The pool writes a process registry:

```text
<output-dir>/state/worker_process_registry.json
```

The registry records the real OS worker process state:

```json
{
  "registry_status": "stopped",
  "worker_count": 2,
  "workers": [
    {
      "worker_agent_id": "agent-repo-map",
      "worker_runtime": "fake",
      "worker_status": "stopped",
      "worker_pid": 12345
    }
  ]
}
```

M16 intentionally keeps the scheduler path blocking and sequential. A daemon
run with `--daemon-long-running-worker-pool` still processes one scheduler step
at a time through `FileMailboxExternalRuntimeAdapter`; the matching resident
worker writes the outbox result, and idle workers remain alive until daemon
shutdown. True concurrent dispatch requires a later split between dispatch and
result collection.

## M17 Two-Phase Dispatch Collect

M17 adds `TwoPhaseFileScheduler`, a side-by-side scheduler that separates
dispatch from result collection:

```python
scheduler = TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    max_inflight=2,
)
dispatch = scheduler.dispatch_ready()
collect = scheduler.collect_ready_results()
```

`dispatch_ready()` selects ready tasks while respecting dependencies, skips
tasks already in `inflight_attempts`, writes each dispatch message to the
selected agent mailbox, and records `runtime_session_started` without waiting
for a runtime result. It also treats existing inflight agents and newly selected
agents as busy, so `max_inflight` never double-books one agent in the same
dispatch pass.

`collect_ready_results()` scans the outboxes for matching `runtime_result`
messages. When a result exists, it records runtime observation, validates
changed files against the task write scope, updates backlog state, appends root
events, rebuilds the SQLite state index, and removes the attempt from
`inflight_attempts`.

The two-phase state file is separate from the blocking scheduler state:

```text
<output-dir>/state/two_phase_scheduler_state.json
```

The root `events.jsonl` remains the authority for replay and the SQLite query
index. M17 keeps these limits:

- one attempt per task;
- no retry routing;
- no integration apply/verify/commit path;
- no lease timeout recovery;
- no worker restart/backoff.

## M18 Two-Phase Retry Timeout Recovery

M18 adds bounded recovery to `TwoPhaseFileScheduler`:

```python
scheduler = TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    max_inflight=2,
    max_attempts=2,
    lease_timeout_seconds=900,
)
```

`max_attempts` is per task. When an attempt returns a retryable runtime failure,
the scheduler appends `validation_rejected` and `recovery_routed`, removes the
attempt from `inflight_attempts`, leaves the task `ready`, and lets a later
dispatch pass create the next attempt. Attempt ids remain task-scoped:

```text
TASK-001-ATTEMPT-001
TASK-001-ATTEMPT-002
```

If an inflight attempt has no matching mailbox result and its lease expires,
`collect_ready_results()` synthesizes a `timed_out` runtime result. That timeout
uses the same outcome classifier as normal runtime output. A timeout is
retryable while attempts remain and blocks the task once the retry budget is
exhausted.

The two-phase CLI exposes the same controls:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m18-two-phase-retry-timeout-run \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --max-inflight 2 \
  --max-attempts 2 \
  --lease-timeout-seconds 900
```

M18 still does not restart or kill worker processes. It only makes scheduler
state advance when the worker reports a retryable failure or fails to report
before the lease deadline.

## M19 Two-Phase Integration Gate

M19 connects accepted two-phase worktree results to the existing integration
gate. The scheduler can now audit the actual worktree diff, write a patch
artifact, apply that patch to an isolated integration worktree, run verification
there, and optionally commit the integration worktree:

```python
scheduler = TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    project_root=repo,
    integrate_accepted_patch=True,
    integration_verification_command=[
        "python3",
        "-c",
        "import pathlib; assert pathlib.Path('generated/result.json').exists()",
    ],
    commit_verified_integration=True,
)
```

For accepted worktree-backed results, `collect_ready_results()` now includes
integration fields in each collected result:

```json
{
  "diff_audit": {"diff_status": "matched"},
  "patch_path": "/tmp/run/attempts/TASK-001-ATTEMPT-001/worktree.patch",
  "integration_status": "applied",
  "integration_verification_status": "passed",
  "integration_commit_status": "committed"
}
```

The two-phase CLI uses the existing integration flags:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m19-two-phase-integration-run \
  --project-root /path/to/git/repo \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --max-inflight 1 \
  --integrate-accepted-patch \
  --integration-verification-command-json '["python3", "-c", "import pathlib; assert pathlib.Path(\"generated/result.json\").exists()"]' \
  --commit-verified-integration
```

M19 still does not merge the integration commit back to the source branch, batch
several task patches into one integration branch, clean up integration
worktrees, or resolve patch conflicts automatically.

## M20 Worker Health And Restart Supervision

M20 adds process-level and pool-level health supervision for resident mailbox
workers. `FileMailboxWorkerProcessSupervisor.health()` reports `not_started`,
`running`, or `exited` from the underlying `Popen` state without changing the
process lifecycle. `restart_if_exited()` restarts only workers that are not
currently running.

`FileMailboxWorkerPoolSupervisor` now exposes:

- `health_check()` for a registry-backed pool snapshot;
- `restart_exited_workers()` for one restart pass across all workers;
- `supervise_once()` for the combined health, restart, and post-health cycle.

The worker process registry at `state/worker_process_registry.json` records the
latest worker status plus `restart_count` for each worker. The two-phase worker
pool CLI interleaves `supervise_once()` before and after each scheduler tick, so
the static pool can recover an exited worker while the scheduler continues to
use the existing dispatch/result protocol.

Two-phase CLI output now includes:

```json
{
  "worker_pool_health": {"pool_status": "running"},
  "worker_pool_supervision": [{"supervision_status": "running"}],
  "worker_pool": {"pool_status": "stopped"}
}
```

M20 intentionally does not add heartbeat files, restart backoff, quarantine
budgets, health-based task reassignment, or cancellation for workers stuck
inside long-running model calls.

## M21 Planner Proposal Decomposition

M21 adds an opt-in automatic decomposition loop for the two-phase worker-pool
path. The scheduler remains the authority: a planner agent can only return a
candidate JSON proposal, and the deterministic scheduler validates and appends
accepted tasks to `state["backlog"]["items"]`.

Planner proposals use this shape:

```json
{
  "milestone_id": "M21",
  "tasks": [
    {
      "task_id": "TASK-M21-GENERATED-001",
      "objective": "Run generated worker task for M21.",
      "read_scope": ["."],
      "write_scope": ["generated/"],
      "required_role": "repo_map_agent",
      "risk_target": "L0",
      "depends_on": [],
      "blockers": []
    }
  ]
}
```

`normalize_task_proposal()` rejects malformed proposals, duplicate task ids,
unknown dependencies, invalid scope fields, unsupported statuses, and generated
tasks that try to become planner tasks themselves.

The two-phase CLI exposes the feature through:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/empty_backlog.json \
  --output-dir /tmp/agentteam-m21-auto-decompose-run \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --auto-decompose-backlog \
  --decomposition-milestone-id M21 \
  --decomposition-planner-role task_planner \
  --decomposition-default-worker-role repo_map_agent
```

When the backlog has no ready tasks and no inflight attempts, the scheduler
creates `DECOMPOSE-<milestone>-001`, dispatches it to the planner role, applies
the returned proposal, and then dispatches generated worker tasks through the
existing mailbox path. `FakeRuntimeAdapter` can synthesize one deterministic
proposal for this planner task so local tests can exercise the full scheduler,
worker-pool, mailbox, proposal, and generated-task loop without a live model.

M21 intentionally does not read roadmap/design documents, recursively decompose
large goals, infer risk levels, package code-map context, or update semantic
architecture artifacts from implementation feedback.

## M22 Planner Context Package

M22 adds a bounded planner context package to the M21 decomposition path. When
auto-decomposition creates `DECOMPOSE-<milestone>-001`, the scheduler writes:

```text
<output-dir>/planner_contexts/DECOMPOSE-<milestone>-001.json
```

The synthetic planner task and mailbox dispatch payload include
`planner_context_path`, so a planner worker receives a file reference rather
than a large inline prompt. The context contains scheduler-state summaries and
permission boundaries:

```json
{
  "context_schema_version": "planner_context.v1",
  "milestone_id": "M22",
  "default_worker_role": "repo_map_agent",
  "allowed_read_scopes": ["."],
  "allowed_write_scopes": ["generated/"],
  "available_agent_roles": ["repo_map_agent", "task_planner"],
  "backlog_summary": {"total": 0, "ready": 0, "blocked": 0, "done": 0, "other": 0},
  "completed_task_ids": []
}
```

`normalize_task_proposal()` can now enforce context-derived constraints:

- generated `required_role` must be one of the available agent roles;
- every generated `write_scope` must be under an allowed write-scope prefix.

The scheduler reads the planner context when applying a proposal and passes
those constraints to the validator. Invalid proposals are marked
`decomposition_status=rejected` with `failure_category=invalid_task_proposal`
and are not appended to the backlog.

`FakeRuntimeAdapter` now reads `planner_context_path` for decomposition tasks
and uses the context milestone, default worker role, and first allowed write
scope to synthesize a deterministic proposal.

M22 intentionally does not read full roadmap/design documents, build a
language-aware code map, include source snippets, recursively refresh context,
or update semantic architecture artifacts.

## M23 Codex Planner Prompt Contract

M23 adds a planner-specific prompt path to `CodexRuntimeAdapter`. When a mailbox
payload has `task_kind=decompose_backlog`, Codex is instructed to act as an
AgentTeam planner, read `planner_context_path`, avoid file edits, and return one
JSON object through the existing `--output-last-message` contract:

```json
{
  "result_status": "completed",
  "changed_files": [],
  "output": {
    "task_proposal": {
      "milestone_id": "M23",
      "tasks": [
        {
          "task_id": "TASK-M23-CODEX-001",
          "objective": "Run generated Codex planner worker task.",
          "read_scope": ["."],
          "write_scope": ["generated/"],
          "required_role": "repo_map_agent",
          "risk_target": "L0",
          "depends_on": [],
          "blockers": []
        }
      ]
    }
  }
}
```

Ordinary implementation tasks still use the existing worker prompt and result
shape. The scheduler still treats Codex planner output as a proposal only:
`TwoPhaseFileScheduler` validates generated roles, write scopes, duplicate ids,
dependencies, and task shape before appending anything to backlog state.

Planner tasks normally have an empty `write_scope`, so they may not receive an
attempt worktree. `CodexRuntimeAdapter` now accepts an optional
`fallback_worktree_path`. If no attempt worktree is supplied, Codex can run in
that fallback path, normally the CLI `--project-root`. The adapter snapshots git
status before and after fallback execution and rejects the result with
`fallback_worktree_modified` if Codex dirties the fallback checkout outside
ignored `.agentteam/` control artifacts.

The two-phase worker-pool CLI can now exercise fake Codex planner
decomposition:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/empty_backlog.json \
  --output-dir /tmp/agentteam-m23-codex-planner-run \
  --project-root /path/to/git/repo \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --auto-decompose-backlog \
  --decomposition-milestone-id M23 \
  --decomposition-planner-role task_planner \
  --decomposition-default-worker-role repo_map_agent \
  --runtime codex \
  --max-steps 10 \
  --codex-command python3 /path/to/fake_codex_planner_and_worker.py
```

In this path, the CLI records `--project-root` as the Codex fallback workspace
and passes it through worker-pool process launch into `CodexRuntimeAdapter`.

M23 intentionally does not ingest roadmap/design artifacts, build a repo map,
score proposal quality beyond the existing validator, recursively decompose
milestones, or require live Codex planner calls in normal tests.

## M24 Semantic Artifact Context Ingestion

M24 lets the planner context include compact summaries of explicitly selected
design or implementation artifacts. This is still a bounded context package,
not a full repository dump and not an LLM-generated summary.

`build_planner_context()` now accepts:

```python
context = build_planner_context(
    agent_pool,
    state,
    milestone_id="M24",
    default_worker_role="repo_map_agent",
    context_artifact_paths=[
        "experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md",
    ],
    context_artifact_excerpt_chars=1200,
)
```

When artifact paths are supplied, the context includes:

```json
{
  "artifact_context": {
    "schema_version": "artifact_context.v1",
    "excerpt_budget_chars": 1200,
    "sources": [
      {
        "path": "experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md",
        "sha256": "hex digest",
        "size_bytes": 4096,
        "modified_at": "2026-06-03T00:00:00Z",
        "heading_count": 8,
        "headings": ["Native Runtime Long-Term Roadmap", "Artifact Role"],
        "excerpt": "bounded normalized text",
        "excerpt_chars": 1200,
        "omitted_chars": 2896
      }
    ],
    "warnings": []
  }
}
```

Missing, non-file, or non-UTF-8 artifacts produce warnings instead of invented
state. The context records no source body beyond the configured per-source
excerpt limit.

The two-phase worker-pool CLI exposes selected artifact ingestion through:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool /path/to/agent_pool.json \
  --backlog /path/to/empty_backlog.json \
  --output-dir /tmp/agentteam-m24-artifact-context-run \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --auto-decompose-backlog \
  --decomposition-milestone-id M24 \
  --decomposition-planner-role task_planner \
  --decomposition-default-worker-role repo_map_agent \
  --planner-context-artifact /path/to/roadmap.md \
  --planner-context-artifact /path/to/design.md \
  --planner-context-excerpt-chars 1200 \
  --max-steps 10
```

M24 intentionally does not discover artifacts automatically, build language-aware
code maps, summarize source files, infer proposal quality, or let planner agents
edit roadmap/design authority documents.

## M25 Proposal Quality Gate

M25 strengthens `normalize_task_proposal()` so planner output is rejected before
backlog insertion when the generated task graph is structurally unsafe or too
broad for its declared risk target.

New deterministic proposal checks:

- generated tasks may not depend on themselves;
- generated tasks may not form dependency cycles through `depends_on`;
- `risk_target` must be one of `L0`, `L1`, or `L2`;
- `L0` tasks may not declare multiple write scopes;
- `L0` tasks may not use repository-wide `write_scope=["."]`;
- `L1` tasks may not declare more than three write scopes.

`L2` generated tasks are accepted only as review-blocked backlog candidates:

```json
{
  "task_id": "TASK-M25-REVIEW-001",
  "backlog_status": "blocked",
  "risk_target": "L2",
  "blockers": ["requires_review"]
}
```

This lets the planner surface a high-risk task without allowing the scheduler to
dispatch it to an implementation worker before review policy exists.

When decomposition is rejected by the proposal quality gate, the scheduler still
uses `failure_category=invalid_task_proposal`. M25 also copies
`decomposition_status` and `decomposition_error` into the `validation_rejected`
event payload so event inspection can show the specific reason, such as
`self dependency` or `dependency cycle`.

M25 intentionally does not infer risk semantically, implement reviewer-agent
unblocking, support L3 authority-update routing, or perform learned task sizing.

## M26 Rolling Milestone Decomposition

M26 changes auto-decomposition from a single synthetic planner task into a
bounded wave loop. The default remains one wave, so existing
`--auto-decompose-backlog` behavior is preserved unless callers explicitly set a
higher wave limit through the scheduler API:

```python
scheduler = TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    auto_decompose=True,
    decomposition_milestone_id="M26",
    decomposition_max_waves=2,
)
```

The synthetic planner task id suffix is now treated as the decomposition wave:

```text
DECOMPOSE-M26-001
DECOMPOSE-M26-002
```

When a planner proposal is applied, generated tasks are tagged with lineage:

```json
{
  "task_id": "TASK-M26-WAVE-1",
  "generated_by_decomposition_task_id": "DECOMPOSE-M26-001",
  "decomposition_wave": 1
}
```

Scheduler state now includes milestone decomposition metadata separate from
worker task status:

```json
{
  "milestones": {
    "M26": {
      "milestone_id": "M26",
      "milestone_status": "active",
      "decomposition_status": "batch_active",
      "decomposition_wave_count": 1,
      "current_decomposition_task_id": "DECOMPOSE-M26-001",
      "generated_task_ids": ["TASK-M26-WAVE-1"]
    }
  }
}
```

A later wave is opened only when there are no ready tasks, no inflight attempts,
the latest decomposition task is done, every generated task from that wave is
terminal, and `decomposition_wave_count < decomposition_max_waves`.

When the configured wave limit is reached, the milestone is marked terminal:

- `milestone_status=completed` when generated tasks are done or there are no
  generated tasks;
- `milestone_status=blocked` when any generated task is blocked;
- `decomposition_status=max_waves_reached`;
- `terminal_reason=max_waves_reached`.

M26 intentionally does not let the planner decide whether another wave is
needed, choose the next milestone automatically, unblock review tasks, or merge
completed feature work back to the main branch.

## M27 Persistent Runtime Process Model

M27 lets a new worker-pool supervisor recover visibility over resident worker
processes that were started by an earlier supervisor instance. It keeps the
runtime state file-backed and does not move worker/session state into SQLite.

The worker pool already writes:

```text
<output-dir>/state/worker_process_registry.json
```

M27 adds:

```python
pool = FileMailboxWorkerPoolSupervisor(agent_pool_path, output_dir)
summary = pool.resume_from_registry()
```

`resume_from_registry()` reads existing worker rows, creates supervisor objects
for the current agent pool, attaches to recorded PIDs, and writes a refreshed
registry summary. Attached workers report health without needing the original
`subprocess.Popen` object:

```json
{
  "worker_status": "running",
  "worker_pid": 12345,
  "worker_agent_id": "agent-repo-map",
  "worker_runtime": "fake",
  "attached": true,
  "stop_file": "/tmp/run/state/workers/agent-repo-map.stop"
}
```

Stopping a resumed pool writes the existing stop file and waits for the attached
PID to exit. This separates logical agent identity, worker process health, and
the lifetime of the current supervisor object.

M27 intentionally does not daemonize the scheduler, add heartbeat/quarantine
policy, recover workers across hosts, or change the storage backend.

## M28 Integration Queue

M28 adds a durable queue view for accepted patch artifacts:

```text
state/integration_queue.json
```

The queue is a materialized view, not the authority. The canonical event log
still owns history; the queue file gives the scheduler and future CLI monitor a
small current-state object to read without replaying the whole run.

Queue entries are keyed by `task_id:attempt_id`:

```json
{
  "queue_item_id": "TASK-001:ATTEMPT-001",
  "queue_status": "pending",
  "task_id": "TASK-001",
  "attempt_id": "ATTEMPT-001",
  "patch_path": "/tmp/run/attempts/ATTEMPT-001/worktree.patch",
  "integration_status": "not_requested",
  "integration_verification_status": "not_requested",
  "integration_commit_status": "not_requested"
}
```

Status transitions are derived from existing gates:

- `pending`: accepted patch captured, automatic integration not yet applied;
- `applied`: patch applied to an integration worktree;
- `verified`: integration verification passed without a created integration
  commit;
- `blocked`: verification or integration commit failed;
- `committed`: verified integration commit was created.

`integration_queued` records the queue insertion in events. Replay now exposes a
lightweight `integration_queue` snapshot and updates it from the existing
`patch_integrated`, `integration_verified`, and `integration_commit_evaluated`
events.

M28 does not merge individual task integration branches into the main branch.
Task-level integration commits remain checkpoints for audit and debugging.

## M28b Integration Batch Verification

M28b adds a batch verification API over the integration queue:

```python
batch = verify_integration_batch(
    project_root,
    output_dir,
    "BATCH-001",
    ["python3", "-m", "pytest"],
)
```

The verifier reads `state/integration_queue.json`, selects non-blocked queued
patches, creates one batch worktree at:

```text
integration_batches/<batch_id>/worktree
```

It applies selected patches in queue order, runs the verification command in the
batch worktree, and writes the result to:

```text
state/integration_batches.json
```

Batch statuses:

- `empty`: no selected queue items exist;
- `blocked`: at least one patch failed to apply to the batch worktree;
- `failed`: all patches applied, but the verification command failed;
- `verified`: all patches applied and the verification command passed.

M28b does not create integration commits and does not merge into the source
branch. It only proves whether a queued patch set can coexist and pass a command
in one worktree.

## M28c Verified Batch Merge

M28c allows a verified batch to merge back into the source branch:

```python
batch = verify_integration_batch(
    project_root,
    output_dir,
    "BATCH-001",
    ["python3", "-m", "pytest"],
    merge_verified_batch=True,
)
```

The merge policy is feature-level batch merge:

1. queued accepted patches are applied together in one batch worktree;
2. the verification command must pass in that batch worktree;
3. the batch worktree is committed;
4. `project_root` fast-forwards to the batch commit with
   `git merge --ff-only`.

The merge is rejected if the batch is not `verified`, the source worktree is
dirty, the batch worktree cannot commit, or the source branch cannot
fast-forward.

Checkpoint means an intermediate integration record. A task-level checkpoint can
show that one task was accepted or committed on an integration branch. A
feature-level batch merge is the final delivery gate for a set of related
patches.

## M29a Worker Restart Budget And Quarantine

M29a adds a restart budget to the resident worker pool:

```python
pool = FileMailboxWorkerPoolSupervisor(
    agent_pool_path,
    output_dir,
    max_restart_count=1,
)
```

The daemon worker-pool CLI paths expose the same policy:

```text
--worker-max-restart-count 1
```

When a worker exits, the pool restarts it while its `restart_count` is below the
budget. Once the budget is reached, the worker is not restarted again. Health
checks and `state/worker_process_registry.json` report:

```json
{
  "worker_status": "quarantined",
  "quarantine_reason": "restart_budget_exceeded",
  "restart_count": 1
}
```

The default `max_restart_count=None` keeps the previous unlimited restart
behavior.

M29a intentionally does not reassign tasks away from quarantined workers yet.

## M29b Quarantined Agent Dispatch Avoidance

M29b connects worker-pool health to two-phase scheduler dispatch. The scheduler
now accepts unavailable agent ids:

```python
scheduler = TwoPhaseFileScheduler(
    agent_pool_path,
    backlog_path,
    output_dir,
    unavailable_agent_ids=["agent-unhealthy"],
)
```

Before dispatch, those agents are marked `unavailable`, so normal role matching
selects another compatible idle agent if one exists.

The supervised two-phase worker-pool CLI updates this set before each scheduler
tick from worker-pool health:

```text
worker_status == quarantined -> unavailable_agent_ids
```

This is conservative reassignment. It prevents new dispatches to quarantined
workers, but it does not move already inflight attempts.

## M29c Reassignment Event Lineage

M29c makes conservative reassignment explicit in the event log. When the
two-phase scheduler sees unavailable agents for the required role and dispatches
the task to another compatible idle agent, it emits:

```json
{
  "event_type": "task_reassigned",
  "payload": {
    "task_id": "TASK-001",
    "attempt_id": "TASK-001-ATTEMPT-001",
    "lease_id": "TASK-001-LEASE-001",
    "required_role": "repo_map_agent",
    "unavailable_agent_ids": ["agent-unhealthy"],
    "selected_agent_id": "agent-healthy",
    "reassignment_reason": "agent_unavailable"
  }
}
```

Replay stores the same lineage on
`attempts[attempt_id]["reassignment"]`. This makes health-driven routing
auditable without changing the execution policy.

M29c still does not move already inflight attempts. It only records why a new
attempt was dispatched to a replacement agent.

## M30a Runtime Observability Summary

M30a adds a read-only CLI summary for an existing runtime output directory:

```text
python -m agentteam_runtime.cli \
  --output-dir output/current \
  --show-runtime-observability
```

The command does not require `--agent-pool` or `--backlog`. It reads the
canonical event log, the rebuildable SQLite state index, the integration queue,
and worker registry files when present. The JSON output includes:

- `event_count` and `latest_event`;
- task, attempt, lease, runtime session, integration queue, and worker status
  counts;
- `blocked_task_ids`;
- bounded `latest_failures`.

This is the first M30 monitor slice. It is intentionally CLI-only and keeps
`events.jsonl` as the source of truth.

## M30b Runtime Observability Drilldown Views

M30b extends the same read-only CLI with a view selector:

```text
python -m agentteam_runtime.cli \
  --output-dir output/current \
  --show-runtime-observability \
  --observability-view events
```

Supported views are `summary`, `backlog`, `leases`, `events`, `sessions`,
`workers`, and `integration-queue`. Each view includes common metadata and then
adds the requested resource list.

`--observability-view` is only valid with `--show-runtime-observability`. The
default remains `summary`, so existing M30a usage is unchanged.

## M30c Roadmap And Decomposition Visibility

M30c adds current milestone and next decomposition visibility to every
observability view:

```json
{
  "current_milestone": {
    "milestone_id": "M30",
    "milestone_status": "active",
    "decomposition_status": "decomposition_ready",
    "current_decomposition_task_id": "DECOMPOSE-M30-001"
  },
  "next_decomposition": {
    "task_id": "DECOMPOSE-M30-001",
    "task_status": "ready",
    "milestone_id": "M30",
    "decomposition_wave": 1,
    "required_role": "task_planner"
  }
}
```

This data is read from the two-phase scheduler state when present. Older output
directories without scheduler milestone state report both fields as `null`.

## M31a Codex Role Runtime Profiles

M31a lets an agent pool define Codex runtime settings once per role:

```json
{
  "role_runtime_profiles": {
    "repo_map_agent": {
      "adapter": "codex",
      "model": "gpt-5.4-mini",
      "sandbox": "workspace-write",
      "timeout_seconds": 300
    }
  }
}
```

The scheduler and resident worker pool now use the same profile precedence:

1. `agent.runtime_profile`;
2. `agent_pool.role_runtime_profiles[agent.role]`;
3. runtime defaults from the CLI or caller;
4. fake runtime.

This keeps role configuration compact while preserving agent-level overrides for
special cases. CLI/default Codex command settings still act as local environment
defaults, so a role profile can declare model, sandbox, and timeout without
hard-coding the executable path.

M31a is still Codex-only for live LLM execution. Fake and shell profiles remain
test or local harnesses. Role-specific prompt contracts are added in M31b;
bounded role context packages remain deferred.

## M31b Role Prompt Contracts

M31b lets an agent pool define model-facing role guidance once per role:

```json
{
  "role_prompt_contracts": {
    "repo_map_agent": {
      "role_summary": "Implement bounded repository edits.",
      "instructions": ["Inspect read_scope before writing."],
      "required_output_keys": ["evidence"]
    }
  }
}
```

When a scheduler dispatches a task, the mailbox payload includes:

- `agent_role`;
- `required_role`;
- `role_prompt_contract`, when the selected role defines one.

`CodexRuntimeAdapter` now renders an explicit `Role prompt contract:` prompt
section before the fixed JSON result schema. This is model guidance, not new
authority: scope validation, result schema validation, leases, retries, and
merge policy remain scheduler-owned.

## M31c Role Context Packages

M31c lets an agent pool define bounded context packages per role:

```json
{
  "role_context_packages": {
    "repo_map_agent": {
      "context_artifacts": ["design/runtime.md"],
      "excerpt_chars": 1200,
      "context_notes": ["Prefer existing helper APIs."]
    }
  }
}
```

During dispatch, the scheduler writes a `role_context.v1` JSON file under
`role_contexts/` and adds these payload fields:

- `role_context_path`;
- `role_context_schema_version`.

The context file includes the selected agent id, role, context notes, and
bounded artifact summaries. It reuses the same digest, heading, excerpt, and
warning metadata shape as planner artifact context. The Codex prompt only points
to `role_context_path`; it does not inline the full context body.

## Intentional Fakes

M0/M3a intentionally fakes or simplifies:

- transcript parsing;
- production-grade non-fast-forward merge orchestration;
- worker heartbeat files, restart backoff, and inflight task migration;
- full roadmap/design/code-map context and live planner prompt quality;
- advanced retry backoff, batch merge queues, and cross-process recovery;
- schema validation through a JSON Schema engine.

M0 now performs actual git worktree creation when `project_root` is provided.
If `project_root` is omitted, it still emits a logical worktree id without
creating a filesystem worktree. M0 also includes a real process adapter through
`ShellRuntimeAdapter`. M1b adds `CodexRuntimeAdapter` for `codex exec` result
extraction through `--output-last-message`. M1c adds a live smoke entrypoint,
but normal committed verification still uses skip/fake paths rather than
spending live model calls. M2 adds bounded retry, outcome classification, and
opt-in accepted-worktree cleanup. M3a adds git diff auditing, M3b writes a patch
artifact, M4 applies accepted patches into an isolated integration worktree, M5
runs opt-in integration verification, and M6 can commit only a verified
integration worktree checkpoint. M7a adds a sequential file-backed scheduler
loop that can process multiple ready tasks until idle. M7b makes scheduler-loop
attempt/worktree ids task-scoped so worktree-backed loops can process more than
one task in a run. M7c exposes that loop through `--run-until-idle`. M8a adds a
canonical root event log for scheduler-loop replay. M8b scopes scheduler-loop
lease and message ids by task id so canonical replay can preserve per-step lease
state. M8c makes the loop CLI include a replayed snapshot. M9a adds a
rebuildable SQLite query index for scheduler-loop state while keeping JSONL as
the authority. M9b adds a read-only state-index API and CLI inspection mode.
M9c repairs missing or stale state indexes from canonical JSONL during reads.
M10a adds a lightweight executable artifact lint command. M11a records logical
runtime session lifecycle events around synchronous adapter calls. M11b adds
runtime sessions to the SQLite state index. M12a adds a gated live Codex
scheduler smoke that exercises scheduler, state index, and runtime session
mechanics through `CodexRuntimeAdapter`. M12b makes Codex a first-class CLI
runtime selector through `--runtime codex`, while retaining `--codex-command`
as a test/experiment override. M12c exposes Codex model, sandbox, and timeout
controls through the CLI. M13a adds agent-level `runtime_profile` resolution so
different role agents can carry different Codex runtime settings in
`agent_pool`. M13b records the effective runtime config on each runtime session
and exposes it through replay and the SQLite state index. M13c moves the
runtime profile resolver from CLI code into runtime core so daemon/API runners
can reuse it. M14a adds a file-backed daemon facade and durable worker registry
while keeping execution sequential and adapter invocations short-lived. M14b
adds file mailbox worker/result round-tripping through `FileMailboxRuntimeAdapter`
with an in-process fake delegate. M14c runs the same mailbox worker through a
one-shot OS subprocess via `FileMailboxSubprocessRuntimeAdapter`. M15a adds one
long-running fake mailbox worker process with explicit supervisor start/stop.
M15b lets that long-running worker execute through `CodexRuntimeAdapter` while
preserving the single-worker topology. M15c makes the long-worker daemon CLI
agent id configurable while still starting only one worker process. M16 adds a
static worker pool that starts one resident worker per agent while preserving
sequential scheduler execution. M17 adds a side-by-side two-phase scheduler
that can keep multiple attempts inflight and collect mailbox results in later
ticks. M18 adds retryable result recovery and lease-timeout recovery to that
two-phase path. M19 connects accepted two-phase worktree results to patch
artifact creation, integration verification, and optional integration commit.
M20 adds static worker-pool health checks, exited-worker restart, restart counts,
registry updates, and two-phase CLI supervision around scheduler ticks. M21 adds
opt-in planner-agent decomposition proposals, deterministic proposal validation,
backlog insertion, and a fake planner runtime path for local end-to-end tests.
M22 adds planner context files, context-derived role/write-scope enforcement,
and fake planner context reads. M23 adds a Codex planner prompt contract,
fallback workspace execution for no-worktree planner tasks, fallback dirty-check
rejection, and fake Codex planner worker-pool coverage. M24 adds selected
artifact summaries with digest, timestamp, heading, excerpt, and warning
metadata in planner context files. M25 adds deterministic proposal quality
checks, L2 review blocking, and decomposition rejection details in validation
events. M26 adds bounded rolling decomposition waves, generated task lineage,
and milestone decomposition state. M27 adds file-registry resume for resident
worker processes through attached PID supervisors. M28 adds a durable accepted
patch integration queue and replay snapshot. M28b adds batch worktree
verification for queued patch sets. M28c adds verified batch fast-forward merge
back to the source branch. M29a adds worker-pool restart budgets and quarantine.
M29b routes new work away from quarantined worker agents. M29c records explicit
reassignment event lineage for those conservative dispatches. M30a adds a
read-only runtime observability summary CLI. M30b adds resource drilldown views.
M30c adds current milestone and next decomposition visibility. M31a adds
role-level Codex runtime profiles for scheduler core and resident worker pools.
M31b adds role prompt contracts to dispatch payloads and Codex prompts. M31c
adds bounded role context package files referenced by dispatch payloads. Claude
Code is not an active backend target on the current route; live LLM work is
Codex-only for now. Future API-based models such as DeepSeek or Claude Opus
remain possible after credentials and result extraction contracts are defined.

These are not semantic omissions. They are deferred implementation mechanics.

## M37 Operator Control Plane

The local launcher now supports a project-scoped operator control plane:

- `agentteam status` reads the latest or selected run and reports both the raw
  scheduler status and `liveness_status`. Liveness is `running-alive` only when
  registered worker PIDs are alive; stale `running` state is reported as
  `running-stale`.
- `agentteam taskpack list` uses the same liveness-aware run status.
- `agentteam start` and `agentteam continue` print compact runtime progress to
  stderr only when the run summary changes or new run events appear. Stdout
  remains reserved for the final JSON result.
- `agentteam watch` is read-only. It prints compact progress lines from the run
  state and event log and can stop after `--max-lines`.
- `agentteam stop` writes registered stop files and signals only registered
  worker PIDs plus owned descendants. `--stale` performs state cleanup only and
  does not terminate live processes.
- Feishu notifications use a sparse allowlist. Manual gates remain supported,
  and two-phase `run_started`, `run_completed`, and `run_timed_out` events can
  produce run-level notification telemetry.
- `agentteam update` installs immutable releases under
  `<work_root>/releases/<release-id>` and changes only the active release
  pointer for future commands. Existing run bindings are preserved. Text status
  output lists only the active release id and known release ids; JSON status
  keeps full release roots, run bindings, and unmanaged run details.
- The repository launcher checks the target project profile and active release
  pointer before falling back to the development checkout.
- `agentteam taskpack delete` is scoped to `drafts/<id>`, `frozen/<id>`, and,
  only with `--delete-run --force`, `runs/<id>`.

Known M37 follow-up work: generate explicit `integration_blocked` and
`run_stale_detected` notification events, and broaden release lifecycle
telemetry around activate/rollback operations.

## Next Preconditions

Before the next backend milestone, the next design/code step should define:

- decide when live Codex smoke should run outside local opt-in, such as nightly
  or pre-release only;
- decide when role context packages should be generated automatically from a
  repository map instead of explicit artifact paths;
- decide when worker supervision should add heartbeat, backoff, and inflight
  migration beyond the current restart-budget/quarantine path;
- decide when planner context should ingest code-map and verification-summary
  context in addition to selected roadmap/design artifacts;
- decide whether lightweight artifact lint should grow into full JSON Schema
  validation;
- retry backoff, retry budget, and failure escalation policy;
- merge strategy and result diff review policy for complete task/system gates.

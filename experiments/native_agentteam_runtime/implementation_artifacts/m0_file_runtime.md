# M0 File Runtime Implementation

Status: implemented on branch `native-runtime-m0`.

This document records the first executable slice of the native AgentTeam
runtime experiment.

## What M0 Proves

The M0 runtime proves the local control-plane path without requiring Codex,
Claude Code, A2A, MCP, SQLite, or a persistent agent process. The current M1b
slice also includes a Codex process adapter so the same control-plane contract
can be exercised through `codex exec`.

Implemented path:

```text
sample backlog
  -> deterministic ready-task selection
  -> idle role-agent lookup
  -> attempt / lease / message / worktree id creation
  -> optional real git worktree creation
  -> mailbox dispatch JSONL
  -> append-only event JSONL
  -> fake, shell, or Codex runtime adapter result
  -> write-scope validation
  -> backlog completion event
  -> replay to task / attempt / lease snapshot
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
    m0_runtime.py
  tests/
    test_m0_runtime.py
```

## Public API

```python
from agentteam_runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    FileScheduler,
    ShellRuntimeAdapter,
    classify_attempt_outcome,
    replay_events,
    run_scheduler_loop,
    run_simulation,
)

result = run_simulation(agent_pool_path, backlog_path, output_dir)
snapshot = replay_events(result["events_path"])

loop_summary = run_scheduler_loop(agent_pool_path, backlog_path, output_dir)

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
```

`run_simulation` writes:

- `events.jsonl`
- `mailboxes/<agent-id>/inbox.jsonl`

The returned summary includes:

- `task_id`
- `attempt_id`
- `lease_id`
- `message_id`
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
  "state_path": "/tmp/agentteam-m7c-run/state/scheduler_state.json"
}
```

Use `--max-steps <n>` with `--run-until-idle` to cap the number of scheduler
steps. The default CLI path remains single-task and still prints the replayed
snapshot. The loop path prints the canonical root `events.jsonl` path; callers
can replay that file with `replay_events(...)`.

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

To run through the Codex adapter, put `--codex-command` last. The default API
command is `codex exec`; the CLI flag exists so tests and experiments can pass a
custom command prefix:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m0-run \
  --project-root /path/to/git/repo \
  --codex-command codex exec
```

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
attempt ids by task id. This keeps worktree-backed multi-step runs from reusing
the same git branch:

```text
TASK-001-ATTEMPT-001 -> agentteam/TASK-001-ATTEMPT-001
TASK-002-ATTEMPT-001 -> agentteam/TASK-002-ATTEMPT-001
```

Plain `run_simulation(...)` keeps the existing default ids such as
`ATTEMPT-001`, `WT-ATTEMPT-001`, and `agentteam/ATTEMPT-001`.

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

M7a is still intentionally sequential. It does not add concurrent workers,
database storage, a daemon process, long-lived Codex/Claude sessions, or
merge-to-main.

## Intentional Fakes

M0/M3a intentionally fakes or simplifies:

- transcript parsing;
- real code patch integration;
- persistent daemon loop;
- advanced retry backoff, queues, and cross-process recovery;
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
canonical root event log for scheduler-loop replay. Claude Code is not
integrated yet.

These are not semantic omissions. They are deferred implementation mechanics.

## Next Preconditions

Before the next backend milestone, the next design/code step should define:

- decide when live Codex smoke should run outside local opt-in;
- Claude Code adapter feasibility and result extraction contract;
- runtime session start/observe/stop interface for long-running workers;
- globally unique lease/message ids for multi-step loops;
- executable artifact/schema lint command;
- retry backoff, retry budget, and failure escalation policy;
- merge strategy and result diff review policy for complete task/system gates.

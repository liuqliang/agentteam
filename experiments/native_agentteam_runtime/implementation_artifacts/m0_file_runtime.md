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
    ShellRuntimeAdapter,
    replay_events,
    run_simulation,
)

result = run_simulation(agent_pool_path, backlog_path, output_dir)
snapshot = replay_events(result["events_path"])

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

## Intentional Fakes

M0/M1c intentionally fakes or simplifies:

- transcript parsing;
- real code patch integration;
- persistent daemon loop;
- retry handling;
- schema validation through a JSON Schema engine.

M0 now performs actual git worktree creation when `project_root` is provided.
If `project_root` is omitted, it still emits a logical worktree id without
creating a filesystem worktree. M0 also includes a real process adapter through
`ShellRuntimeAdapter`. M1b adds `CodexRuntimeAdapter` for `codex exec` result
extraction through `--output-last-message`. M1c adds a live smoke entrypoint,
but normal committed verification still uses skip/fake paths rather than
spending live model calls. Claude Code is not integrated yet.

These are not semantic omissions. They are deferred implementation mechanics.

## M1 Preconditions

Before the next backend milestone, the next design/code step should define:

- decide when live Codex smoke should run outside local opt-in;
- Claude Code adapter feasibility and result extraction contract;
- worktree cleanup policy;
- runtime session start/observe/stop interface for long-running workers;
- executable artifact/schema lint command;
- retry event handling.

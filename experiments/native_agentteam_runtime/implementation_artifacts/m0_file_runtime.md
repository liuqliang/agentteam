# M0 File Runtime Implementation

Status: implemented on branch `native-runtime-m0`.

This document records the first executable slice of the native AgentTeam
runtime experiment.

## What M0 Proves

The M0 runtime proves the local control-plane path without starting Codex,
Claude Code, A2A, MCP, SQLite, or a persistent agent process.

Implemented path:

```text
sample backlog
  -> deterministic ready-task selection
  -> idle role-agent lookup
  -> attempt / lease / message / worktree id creation
  -> optional real git worktree creation
  -> mailbox dispatch JSONL
  -> append-only event JSONL
  -> fake or shell runtime adapter result
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
from agentteam_runtime import FakeRuntimeAdapter, ShellRuntimeAdapter, replay_events, run_simulation

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

## Intentional Fakes

M0 intentionally fakes or simplifies:

- transcript parsing;
- real code patch integration;
- persistent daemon loop;
- retry handling;
- schema validation through a JSON Schema engine.

M0 now performs actual git worktree creation when `project_root` is provided.
If `project_root` is omitted, it still emits a logical worktree id without
creating a filesystem worktree. M0 also includes a real process adapter through
`ShellRuntimeAdapter`, but it does not yet integrate Codex or Claude Code.

These are not semantic omissions. They are deferred implementation mechanics.

## M1 Preconditions

Before M1 real backend integration, the next design/code step should define:

- backend choice for the first adapter, likely Codex or Claude Code;
- worktree cleanup policy;
- Codex or Claude Code runtime session start/observe/stop interface;
- Codex or Claude Code result extraction contract;
- executable artifact/schema lint command;
- retry event handling.

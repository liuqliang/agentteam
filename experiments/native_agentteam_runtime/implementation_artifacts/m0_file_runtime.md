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
  -> mailbox dispatch JSONL
  -> append-only event JSONL
  -> fake runtime result
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
from agentteam_runtime import replay_events, run_simulation

result = run_simulation(agent_pool_path, backlog_path, output_dir)
snapshot = replay_events(result["events_path"])
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
- `validation_status`
- output file paths

## CLI

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m0-run
```

The CLI prints one JSON summary containing the simulation result and replayed
snapshot.

## Intentional Fakes

M0 intentionally fakes or simplifies:

- runtime backend execution;
- transcript parsing;
- actual git worktree creation;
- real code patch integration;
- persistent daemon loop;
- timeout and retry handling;
- schema validation through a JSON Schema engine.

These are not semantic omissions. They are deferred implementation mechanics.

## M1 Preconditions

Before M1 real backend integration, the next design/code step should define:

- backend choice for the first adapter, likely Codex or Claude Code;
- actual worktree creation and cleanup policy;
- runtime session start/observe/stop interface;
- result extraction contract for the selected backend;
- executable artifact/schema lint command;
- timeout and retry event handling.

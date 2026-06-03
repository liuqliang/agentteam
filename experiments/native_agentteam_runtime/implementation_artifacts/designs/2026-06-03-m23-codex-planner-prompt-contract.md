# M23 Codex Planner Prompt Contract Design

Status: approved for implementation by standing roadmap authorization.

## Goal

M23 makes `CodexRuntimeAdapter` capable of executing a decomposition planner
task and returning the same structured `task_proposal` contract that the fake
planner already returns.

## Scope

M23 supports:

- a planner-specific Codex prompt for `task_kind=decompose_backlog`;
- explicit instructions that planner tasks must return
  `output.task_proposal`;
- reuse of the existing `--output-last-message` JSON result contract;
- Codex planner execution when the task has no writable attempt worktree;
- a fallback workspace safety check that rejects planner runs which dirty the
  fallback checkout;
- deterministic fake Codex tests for the planner prompt and two-phase worker
  pool path.

M23 deliberately defers:

- semantic artifact summarization;
- repository map ingestion;
- proposal quality rules beyond the existing role, scope, duplicate id, and
  dependency checks;
- recursive or rolling milestone decomposition;
- changing the scheduler's authority model;
- making live Codex planner calls mandatory in tests.

## Architecture

`CodexRuntimeAdapter` currently builds one generic implementation prompt. M23
keeps that prompt for ordinary implementation tasks and adds a separate planner
prompt when the mailbox payload has:

```json
{"task_kind": "decompose_backlog"}
```

The planner prompt tells Codex to read the bounded planner context from
`planner_context_path` and return exactly one JSON object through the existing
`--output-last-message` file:

```json
{
  "result_status": "completed",
  "changed_files": [],
  "output": {
    "task_proposal": {
      "milestone_id": "M23",
      "tasks": [
        {
          "task_id": "TASK-M23-001",
          "objective": "Implement one bounded task.",
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

Planner tasks usually have an empty `write_scope`, so the scheduler may not
create an attempt worktree. M23 adds an optional Codex adapter
`fallback_worktree_path`. If `run(..., worktree_path=None)` is called, the
adapter may run Codex in that fallback path. This is intended for read-only
planner work and is normally set to the CLI `--project-root`.

Because the fallback path can be the authority checkout, the adapter snapshots
git status before and after fallback execution. If Codex modifies tracked or
untracked files outside ignored `.agentteam/` control artifacts, the adapter
rejects the result with `error=fallback_worktree_modified`.

The scheduler remains the authority boundary. Codex planner output is only a
proposal. `TwoPhaseFileScheduler` still validates role, write scope, duplicate
task id, and dependency constraints before adding tasks to backlog state.

## CLI And Worker Pool

The existing CLI `--runtime codex --project-root <repo>` path already requires
a project root. M23 records that path as the Codex fallback workspace so a
planner worker can execute without a writable attempt worktree.

The mailbox worker process also receives the fallback path when it is launched
as part of a Codex worker pool. The worker process passes it into
`CodexRuntimeAdapter`, preserving the same behavior in direct adapter tests and
daemon two-phase worker-pool runs.

## Acceptance

M23 is accepted when:

- a planner mailbox message builds a prompt containing the `task_proposal`
  contract and the concrete `planner_context_path`;
- `CodexRuntimeAdapter` can run a fake Codex planner command with only a
  fallback workspace;
- a fallback planner run that changes the fallback checkout is rejected;
- the two-phase worker-pool CLI can auto-decompose through a fake Codex planner
  and complete the generated worker task;
- existing fake planner, worker pool, retry, integration, health, and planner
  context tests still pass.

# M21 Planner Proposal Decomposition Design

Status: approved for implementation.

## Goal

M21 adds the smallest automatic task decomposition loop: when the two-phase
scheduler has no runnable work, it can dispatch a planner agent task, receive a
structured task proposal, validate it deterministically, and append accepted
tasks to the scheduler backlog.

## Scope

M21 supports:

- a JSON proposal contract for planner-generated backlog tasks;
- deterministic proposal validation before any task is accepted;
- optional two-phase scheduler auto-decomposition;
- planner dispatch through the same mailbox and worker-pool mechanism as normal
  work;
- generated task insertion into scheduler state;
- CLI flags that enable the feature for the two-phase worker-pool path;
- a fake planner runtime path for deterministic local tests.

M21 deliberately defers:

- recursive decomposition;
- roadmap and semantic design document reading;
- code index construction for planner context;
- risk-level inference beyond accepting the planner-provided `risk_target`;
- model-specific planner prompt tuning;
- automatic architecture-document feedback.

## Architecture

The scheduler remains deterministic. Planner agents do not write the backlog and
do not dispatch worker agents directly. They only return a proposal:

```json
{
  "milestone_id": "M21",
  "tasks": [
    {
      "task_id": "TASK-M21-001",
      "objective": "Implement a bounded runtime change.",
      "read_scope": ["experiments/native_agentteam_runtime/"],
      "write_scope": ["experiments/native_agentteam_runtime/m0_runtime/"],
      "required_role": "repo_map_agent",
      "risk_target": "L1",
      "depends_on": [],
      "blockers": []
    }
  ]
}
```

`task_proposal.py` validates and normalizes this structure. It rejects duplicate
task ids, non-string scopes, missing objectives, unsupported statuses, generated
planner tasks, and dependencies that reference neither existing tasks nor tasks
inside the same proposal.

`TwoPhaseFileScheduler` gains optional decomposition settings. When enabled and
the scheduler has no ready tasks and no inflight attempts, it appends one
synthetic planner task:

```text
DECOMPOSE-<milestone>-001
```

That task uses `task_kind=decompose_backlog`, `required_role=<planner role>`,
empty write scope, and a payload that tells the planner which milestone and
default worker role to target. The existing dispatch path sends this task to a
planner worker.

When the planner result is collected, the scheduler reads
`runtime_result.output.task_proposal`, validates it, appends accepted generated
tasks to `state["backlog"]["items"]`, and records the generated task ids in the
planner step result. The next dispatch cycle can then send those generated tasks
to normal worker agents.

## CLI

The two-phase worker-pool CLI gains:

```text
--auto-decompose-backlog
--decomposition-milestone-id M21
--decomposition-planner-role task_planner
--decomposition-default-worker-role repo_map_agent
```

The feature remains opt-in. Existing two-phase runs are unchanged unless
`--auto-decompose-backlog` is supplied.

## Acceptance

M21 is accepted when:

- a valid planner proposal normalizes into executable backlog tasks;
- invalid duplicate or malformed proposals are rejected;
- an idle auto-decomposition scheduler dispatches a planner task;
- a collected planner result appends generated tasks to scheduler state;
- the two-phase worker-pool CLI can run a fake planner worker and complete a
  generated task;
- M16 to M20 worker, retry, integration, and supervision behavior still passes.

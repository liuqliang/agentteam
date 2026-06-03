# M26 Rolling Milestone Decomposition Design

Status: approved for implementation by standing roadmap authorization.

## Goal

M26 turns auto-decomposition from a single synthetic planner task into a bounded
milestone wave loop.

## Scope

M26 supports:

- decomposition wave numbering per milestone;
- a configurable maximum decomposition wave count;
- generated task lineage back to the decomposition task and wave;
- milestone decomposition state stored separately from worker task status;
- opening a new decomposition wave only after the previous generated batch is
  terminal;
- marking a milestone complete or blocked once the maximum wave count is
  reached.

M26 deliberately defers:

- planner judgment about whether more waves are needed;
- automatic milestone selection beyond the configured milestone id;
- cross-milestone dependency planning;
- review-agent unblocking for `L2` tasks;
- merge-to-main policy.

## Architecture

`TwoPhaseFileScheduler` currently creates at most one synthetic task:

```text
DECOMPOSE-<milestone>-001
```

M26 keeps the same id pattern and treats the numeric suffix as the decomposition
wave. The scheduler gains:

```python
decomposition_max_waves=1
```

The default preserves existing behavior. A caller can opt into rolling
decomposition by setting a higher limit.

The scheduler state gains a `milestones` map:

```json
{
  "milestones": {
    "M26": {
      "milestone_id": "M26",
      "milestone_status": "active",
      "decomposition_status": "batch_active",
      "decomposition_wave_count": 1,
      "current_decomposition_task_id": "DECOMPOSE-M26-001",
      "generated_task_ids": ["TASK-M26-001"]
    }
  }
}
```

When a planner proposal is applied, generated tasks receive:

```json
{
  "generated_by_decomposition_task_id": "DECOMPOSE-M26-001",
  "decomposition_wave": 1
}
```

The next wave is opened only when:

- auto-decomposition is enabled;
- there are no ready tasks;
- there are no inflight attempts;
- the previous decomposition task is terminal;
- every generated task from the previous wave is terminal;
- `decomposition_wave_count < decomposition_max_waves`.

If the maximum wave count is reached, the milestone is marked:

- `completed` when generated tasks are done or no generated tasks exist;
- `blocked` when any generated task is blocked.

This prevents infinite decomposition loops while allowing controlled multi-wave
operation in tests and later project runs.

## Acceptance

M26 is accepted when:

- existing single-wave auto-decomposition behavior still works by default;
- generated tasks record decomposition lineage;
- with `decomposition_max_waves=2`, a second decomposition task is created after
  the first generated batch completes;
- a third wave is not created when the max wave count is reached;
- milestone state records decomposition status separately from worker task
  status;
- existing M21-M25 planner tests still pass.

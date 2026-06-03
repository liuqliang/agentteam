# M30c Roadmap And Decomposition Visibility Design

## Goal

Show the current scheduler milestone and next decomposition task in the runtime
observability output.

## Design

M30c keeps `events.jsonl` as the behavioral source of truth and reads
`state/two_phase_scheduler_state.json` only as scheduler-owned context. The
observability base metadata now includes:

- `current_milestone`;
- `next_decomposition`.

`current_milestone` is selected from scheduler milestone state, preferring an
active milestone. `next_decomposition` is derived from
`current_milestone.current_decomposition_task_id` and the scheduler backlog.

When no two-phase scheduler state exists, both fields are `null`.

## Policy

This is a read-only view. It does not let observability mutate milestones,
planner context, backlog, or semantic roadmap artifacts.

## Non-Goals

M30c does not parse long-term markdown roadmap files, create new planning tasks,
or decide which longer-term route should run after M30.

# M29b Quarantined Agent Dispatch Avoidance Design

## Goal

Prevent the scheduler from assigning new work to quarantined resident workers.

## Design

`TwoPhaseFileScheduler` now accepts `unavailable_agent_ids`. During dispatch it
marks those agents as `unavailable` before selecting an idle agent. The existing
role matching then naturally selects another compatible idle agent when one is
available.

The supervised two-phase worker-pool CLI updates this set before each scheduler
tick:

1. run `worker_pool.supervise_once()`;
2. collect workers whose `worker_status == quarantined`;
3. call `scheduler.set_unavailable_agent_ids(...)`;
4. run `scheduler.tick()`.

## Policy

This is conservative reassignment. It does not move already inflight attempts.
It only prevents new dispatches to workers known to be quarantined before the
tick.

## Non-Goals

M29b does not add explicit reassignment events yet. Attempt lineage remains the
standard dispatch and attempt lineage already in scheduler state and events.

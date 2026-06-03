# M29c Reassignment Event Lineage Design

## Goal

Record explicit lineage when the scheduler routes new work away from an
unavailable same-role agent.

## Design

`TwoPhaseFileScheduler` now emits `task_reassigned` when both conditions hold:

- at least one agent with the required role is marked unavailable;
- the task is successfully dispatched to another compatible idle agent.

The event payload records:

- `task_id`;
- `attempt_id`;
- `lease_id`;
- `required_role`;
- `unavailable_agent_ids`;
- `selected_agent_id`;
- `reassignment_reason`.

The replay snapshot stores this under
`attempts[attempt_id]["reassignment"]`, so diagnostic code can inspect the
lineage without rescanning raw JSONL.

## Policy

This event describes conservative reassignment before dispatch. It does not
mean an already inflight attempt was moved.

## Non-Goals

M29c does not add inflight migration, new retry budgets, heartbeat policy, or
automatic escalation policy. Those remain separate health-driven scheduling
decisions.

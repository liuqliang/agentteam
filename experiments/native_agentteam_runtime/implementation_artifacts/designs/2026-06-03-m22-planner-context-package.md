# M22 Planner Context Package Design

Status: approved for implementation.

## Goal

M22 gives the planner agent a bounded context package so automatic decomposition
is based on current scheduler state, available roles, and explicit scope
permissions instead of a thin milestone id.

## Scope

M22 supports:

- deterministic planner context construction;
- a `planner_context_path` attached to synthetic decomposition tasks and mailbox
  dispatch payloads;
- context fields for milestone id, backlog summary, completed steps, available
  roles, runtime capabilities, allowed read scopes, allowed write scopes, and
  proposal contract hints;
- proposal validation that rejects unknown `required_role` values;
- proposal validation that rejects generated `write_scope` entries outside the
  allowed write scopes;
- fake planner behavior that reads the context file and generates a task inside
  the allowed scope.

M22 deliberately defers:

- reading full design documents;
- building language-specific repository indexes;
- embedding source code or large file contents;
- model-specific prompt tuning;
- recursive context refresh across multiple decomposition waves;
- semantic architecture feedback from implementation results.

## Architecture

`planner_context.py` owns context construction. It receives the scheduler state,
agent pool, milestone id, default worker role, and allowed scopes, then returns a
small JSON-serializable object:

```json
{
  "context_schema_version": "planner_context.v1",
  "milestone_id": "M22",
  "default_worker_role": "repo_map_agent",
  "allowed_read_scopes": ["."],
  "allowed_write_scopes": ["generated/"],
  "available_agent_roles": ["repo_map_agent", "task_planner"],
  "backlog_summary": {
    "total": 1,
    "ready": 0,
    "blocked": 0,
    "done": 1
  },
  "completed_task_ids": ["DECOMPOSE-M22-001"],
  "proposal_contract": {
    "required_fields": ["task_id", "objective", "read_scope", "write_scope", "required_role", "risk_target"],
    "forbidden_task_kind": "decompose_backlog"
  }
}
```

`TwoPhaseFileScheduler` writes the context to:

```text
<output-dir>/planner_contexts/DECOMPOSE-<milestone>-001.json
```

The synthetic decomposition task stores the same path in `planner_context_path`.
The dispatch message payload includes this field, so a planner worker can read
the file without receiving a large inline prompt payload.

`task_proposal.normalize_task_proposal()` accepts optional `allowed_roles` and
`allowed_write_scopes`. Scheduler proposal application passes values from the
context package. A generated task is accepted only when its role exists and all
write scopes are under an allowed prefix.

`FakeRuntimeAdapter` reads `planner_context_path` for planner tasks and returns a
deterministic proposal using the context milestone, default worker role, and
first allowed write scope. This keeps local end-to-end tests deterministic while
preserving the same result contract that a real Codex planner must produce.

## Acceptance

M22 is accepted when:

- context construction returns stable role, backlog, scope, and contract fields;
- auto-decomposition writes a planner context file and includes its path in the
  dispatch payload;
- a proposal with an unknown role is rejected;
- a proposal with a write scope outside the context allowance is rejected;
- the two-phase worker-pool CLI can run the fake planner using the context file
  and complete the generated task;
- M16 to M21 worker, retry, integration, supervision, and decomposition behavior
  still passes.

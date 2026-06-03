# M31b Role Prompt Contracts Design

## Goal

Make role-specific execution guidance travel with each dispatched task, without
requiring a runtime worker to read the full agent pool.

## Design

The agent pool may define prompt contracts keyed by role:

```json
{
  "role_prompt_contracts": {
    "repo_map_agent": {
      "role_summary": "Implement bounded repository edits.",
      "instructions": ["Inspect read_scope before writing."],
      "required_output_keys": ["evidence"]
    }
  }
}
```

When the scheduler dispatches a task, the mailbox payload now includes:

- `agent_role`;
- `required_role`;
- `role_prompt_contract`, when the selected role has a contract.

`CodexRuntimeAdapter` renders an explicit `Role prompt contract:` prompt
section before the fixed JSON result contract. The full mailbox message is still
included after that, so older runtimes remain compatible.

## Policy

The scheduler remains the authority for role selection, scope, leases, retries,
and validation. Role prompt contracts are guidance for the model, not permission
to expand scope or alter result schemas.

## Non-Goals

M31b does not add code-map context, MCP tool exposure, model selection policy,
or semantic authority updates. It also does not make role contracts a validation
gate beyond JSON shape checks.

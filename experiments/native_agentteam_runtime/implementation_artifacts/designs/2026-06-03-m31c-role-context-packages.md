# M31c Role Context Packages Design

## Goal

Give role agents bounded context files without embedding large source or design
material directly in the Codex prompt.

## Design

The agent pool may define role context packages:

```json
{
  "role_context_packages": {
    "repo_map_agent": {
      "context_artifacts": ["design/runtime.md"],
      "excerpt_chars": 1200,
      "context_notes": ["Prefer existing helper APIs."]
    }
  }
}
```

At dispatch time, the scheduler writes a role context JSON file under
`role_contexts/` for the selected agent. The mailbox payload carries:

- `role_context_path`;
- `role_context_schema_version`.

The context file has schema version `role_context.v1`, includes the selected
agent id and role, preserves compact context notes, and reuses the existing
artifact summary builder for bounded artifact excerpts.

`CodexRuntimeAdapter` renders an explicit `Role context package:` prompt section
that points to `role_context_path`. The prompt still carries only the path, not
the full context body.

## Policy

Role context packages are advisory context. They do not expand read/write scope,
change validation rules, or give workers authority over semantic artifacts.

## Non-Goals

M31c does not build a language-aware repository map, run LSP/compiler analysis,
or choose context automatically. Role package source artifacts remain explicitly
configured.

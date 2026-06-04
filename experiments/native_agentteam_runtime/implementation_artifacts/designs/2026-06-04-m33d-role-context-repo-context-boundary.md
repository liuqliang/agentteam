# M33d Role Context And Repo Context Boundary Decision

## Decision

Keep role context packages and repo context packages as separate prompt
sections and separate artifact files.

Do not automatically fold repo context summaries into `role_context.v1` in the
current route.

## Rationale

Role context and repo context have different lifetimes and authority:

- role context is role-level guidance configured from the agent pool;
- repo context is attempt-level navigation generated from a specific task,
  repository state, and selected agent role;
- role context may include durable instructions and selected design artifacts;
- repo context may become stale after a commit, dirty worktree change, or cache
  invalidation.

Automatically embedding repo context into role context would make it harder to
tell whether a worker followed role instructions or task-local repository
selection. It would also make context invalidation harder because a role package
could appear durable while containing attempt-scoped repository metadata.

## Current Contract

Dispatch payloads may carry both fields:

```json
{
  "role_context_path": "run/role_contexts/ATTEMPT-001-repo_map_agent.json",
  "repo_context_path": "run/repo_contexts/ATTEMPT-001-repo_map_agent.json"
}
```

`CodexRuntimeAdapter` renders them as separate prompt sections:

```text
Role context package:
<role_context_path>

Repo context package:
<repo_context_path>
```

Workers may read both, but neither expands read scope, write scope, lease
authority, validation policy, or merge policy.

## Allowed Future Change

A later milestone may add an explicit bridge field such as
`related_repo_context_path` inside `role_context.v1`, but only as a pointer. It
should not inline repo context bodies into role context packages unless a
separate context-budget design proves that the benefit outweighs the ambiguity.

## Consequence

Observability is responsible for joining role, repo, attempt, and diff-audit
evidence. The model prompt remains explicit and inspectable, with separate
sections for durable role guidance and attempt-local repository navigation.

# Codex-Only Runtime Backend Decision

## Decision

The current implementation route uses Codex as the only live LLM runtime
backend. The user does not have Claude API access, so Claude Code compatibility
is not an active implementation target.

## Implications

- Role differentiation should use Codex runtime profiles, not vendor-specific
  backends.
- Planner, implementer, reviewer, and integrator roles may use different Codex
  model, sandbox, timeout, command, prompt, and worktree settings.
- Scheduler dispatch should continue to route by role, capability, health, and
  scope, but the executable runtime adapter for live LLM work remains Codex.
- MCP/tool/context compatibility should be treated as plumbing around Codex
  runtime sessions, not as a separate agent backend requirement.

## Deferred Work

Claude Code or other backend adapters can be reconsidered only if an API,
local executable contract, or result extraction contract becomes available.
Until then, they should not appear on the near-term roadmap.

## Non-Goals

This decision does not remove fake or shell adapters used for deterministic
tests and local harnesses. It also does not change the semantic runtime model,
which can remain backend-agnostic for future portability.

# Current Codex Runtime Backend Decision

## Decision

The current implementation route uses Codex as the only live LLM runtime
backend. This is a current execution constraint, not a permanent architecture
ban on other models. The user may later introduce API-based models such as
DeepSeek or Claude Opus, but those backends are not active implementation
targets now.

## Implications

- Role differentiation should use Codex runtime profiles in the current route,
  not vendor-specific backends.
- Planner, implementer, reviewer, and integrator roles may use different Codex
  model, sandbox, timeout, command, prompt, and worktree settings.
- Scheduler dispatch should continue to route by role, capability, health, and
  scope, but the executable runtime adapter for current live LLM work remains
  Codex.
- MCP/tool/context compatibility should be treated as plumbing around Codex
  runtime sessions, not as a separate agent backend requirement.

## Deferred Work

DeepSeek, Claude Opus, Claude Code, or other backend adapters can be
reconsidered when API credentials, executable contracts, and result extraction
contracts are available. Until then, they should not appear on the near-term
implementation roadmap.

## Non-Goals

This decision does not remove fake or shell adapters used for deterministic
tests and local harnesses. It also does not change the semantic runtime model,
which can remain backend-agnostic for future portability.

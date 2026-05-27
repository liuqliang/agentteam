# Codex, Claude Code, and Claw Code Architecture Notes

Snapshot date: 2026-05-20

This document combines source-level architecture notes with the multi-agent
mechanics conclusions from `research/codex_vs_claude_agent_mechanics.md`. The
research document remains in place as the higher-level orchestration guidance.

## Local Checkouts

- Codex: `srccode/codex/`
  - Remote: `https://github.com/openai/codex.git`
  - Main implementation: `srccode/codex/codex-rs/`
  - License: Apache-2.0 at repository/workspace level.

- Official Claude Code public repository: `srccode/claude/claude-code/`
  - Remote: `https://github.com/anthropics/claude-code.git`
  - Public contents are mainly plugins, commands, hooks, examples, and docs.
  - This is not the full core Claude Code CLI source.

- Claude Agent SDKs and actions:
  - `srccode/claude/claude-agent-sdk-python/`
  - `srccode/claude/claude-agent-sdk-typescript/`
  - `srccode/claude/claude-code-action/`
  - `srccode/claude/claude-code-base-action/`

- Claw Code: `srccode/claude/claw-code/`
  - Remote: `https://github.com/ultraworkers/claw-code.git`
  - Main implementation: `srccode/claude/claw-code/rust/`
  - GitHub license detection was previously null; the Rust workspace declares `license = "MIT"` in `rust/Cargo.toml`, but there is no root `LICENSE` file in this checkout. Treat licensing carefully.
  - Non-official project. It explicitly says it is not affiliated with Anthropic and does not claim ownership of original Claude Code source material.

## High-Level Shape

Codex is a large production workspace with many small crates. The core design separates:

- `cli`: top-level command dispatcher.
- `tui`: fullscreen terminal UI.
- `exec`: headless/non-interactive automation path.
- `core`: business logic for sessions, turn loop, tools, permissions, model client, sandboxing, MCP, skills, rollouts.
- `protocol`: shared event/op/config protocol types.
- `tools`: shared Responses API tool definitions and tool schema helpers.
- many support crates for app server, MCP server, cloud tasks, plugins, skills, state, sandboxing, etc.

Official Claude Code public materials expose plugins, commands, hooks, agent
prompts, SDK options, transcript storage types, and GitHub Actions integration.
They do not expose the core CLI implementation. The useful public signal is
therefore about the shape of subagent configuration, lifecycle hooks, transcript
handling, and plugin workflows rather than the internal scheduler.

Claw Code is a much smaller Rust rewrite/harness. The main runtime is intentionally concentrated:

- `rust/crates/rusty-claude-cli`: CLI binary named `claw`.
- `rust/crates/runtime`: conversation runtime, session persistence, config, permissions, file/bash/MCP/runtime primitives.
- `rust/crates/tools`: tool specs and tool dispatch.
- `rust/crates/api`: Anthropic/OpenAI-compatible provider clients and streaming.
- `rust/crates/commands`: slash command registry and rendering.
- `rust/crates/plugins`: plugin lifecycle.
- `rust/crates/mock-anthropic-service`: local deterministic parity harness.

## Entrypoints

Codex:

- `srccode/codex/codex-rs/cli/src/main.rs`
- Uses `clap` and dispatches to subcommands including `exec`, `review`, `login`, `mcp`, `mcp-server`, `app-server`, `resume`, `fork`, `cloud`, sandbox/debug helpers, and TUI fallback.
- Interactive operation ultimately routes into `codex_tui::run_main`.
- Headless operation routes into `codex_exec::run_main`.

Claw Code:

- `srccode/claude/claw-code/rust/crates/rusty-claude-cli/src/main.rs`
- Own CLI parser and dispatcher around a `CliAction` enum.
- Routes direct local commands such as status, sandbox, agents, mcp, skills, plugins, system-prompt, version, config, diff, export, init, doctor.
- One-shot prompts create `LiveCli` and call `run_turn_with_output`.
- Interactive mode goes through `run_repl`.

## Turn Loop / Session Runtime

Codex:

- Main abstraction is thread/session oriented.
- `codex-core/src/codex_thread.rs` exposes `CodexThread`, a bidirectional stream facade around an internal `Codex` session.
- Clients submit protocol operations (`Op`) and receive structured protocol events (`Event`/`EventMsg`).
- This event protocol makes it easier to support TUI, app server, remote control, MCP server, and external clients over one core.

Claw Code:

- Main abstraction is `ConversationRuntime<C, T>` in `srccode/claude/claw-code/rust/crates/runtime/src/conversation.rs`.
- It owns `Session`, API client, tool executor, permission policy, system prompt, usage tracker, hook runner, compaction threshold, and session tracer.
- It is simpler and more direct: one runtime object drives turns, invokes tools, updates session, and emits output through the CLI layer.

## Multi-Agent Mechanics

The key shared limitation across Codex and Claude-style systems is that a
subagent ultimately tends to return a natural-language report rather than a
stable structured execution result. A parent agent that trusts the report alone
does not know which files changed, which tests actually ran, whether the output
matches a schema, or whether failure came from implementation, task definition,
or environment.

For AgentTeam, this means platform-native subagent semantics are not enough.
The framework must define its own structured input bundle, output contract,
validation protocol, and integration rule above the tool runtime.

### Codex

Codex has the strongest production-grade multi-agent control plane in the
checked-out source:

- `spawn_agent` creates a separate thread/session.
- `AgentPath` gives agents canonical paths such as `/root/worker`.
- parent/child metadata is stored through `SessionSource::SubAgent`.
- `InterAgentCommunication` carries `author`, `recipient`, `content`, and `trigger_turn`.
- each session has a mailbox; `send_message` queues, while `followup_task` wakes the target turn.
- `wait_agent` observes mailbox sequence changes rather than treating a child report as verified completion.
- `AgentControl` owns spawn, close, resume, list, path resolution, and parent-child lifecycle.

This design is close to an actor tree: independent sessions communicate through
structured envelopes, and the root orchestrator remains responsible for
integration.

Codex is therefore the best source reference for:

- hierarchical agent identity
- resumable subagent sessions
- mailbox-based communication
- parent-child lifecycle events
- forked context modes
- central orchestration over worker threads

Its risk is that it still does not prove task correctness. A subagent status or
final assistant message must be passed through AgentTeam's own validator,
CR/trace process, and artifact integration rules.

### Claude Code

The official Claude Code core implementation is not visible in the public
repository, so the public evidence comes from plugins, commands, hooks, and the
Agent SDKs.

Useful public mechanics:

- SDK `AgentDefinition` defines subagent prompt, tools, model, skills, memory,
  MCP servers, initial prompt, max turns, background mode, effort, and
  permission mode.
- SDK options expose `agents`, meaning custom subagents can be defined
  programmatically and invoked through the Agent tool.
- hook types include `SubagentStart` and `SubagentStop`.
- tool lifecycle hooks can include `agent_id` and `agent_type`, which is
  essential when multiple subagents run in parallel over one control channel.
- session stores distinguish main transcripts from subagent transcripts through
  subpaths such as `subagents/agent-{id}`.
- public plugins, especially feature-development workflows, show a pattern of
  launching explorer, architect, and reviewer agents in parallel, then having a
  parent agent consolidate outputs.

Claude Code is therefore a better reference for:

- isolated specialist agents
- lifecycle hook gating
- external or heterogeneous review
- transcript separation
- plugin-level multi-agent workflows

Its risk is the same: without an upper-layer schema and validator, the final
subagent report is still only a natural-language summary. `SubagentStop` style
hooks should call validators or inspect structured output, not perform vague
subjective approval.

### Claw Code

Claw Code is not official Claude Code, but it is useful because its Rust source
is compact enough to study end-to-end.

Its `Agent` tool:

- accepts `description`, `prompt`, `subagent_type`, `name`, and `model`.
- creates an `agent-{timestamp}` id.
- writes a markdown output file and JSON manifest under `.clawd-agents`.
- spawns a background Rust thread for the subagent job.
- builds a fresh `ConversationRuntime`.
- restricts tools through `SubagentToolExecutor`.
- persists terminal state, blocker classification, lane events, and completion
  summaries.

It also has in-memory registries for:

- `TaskCreate`, `TaskGet`, `TaskUpdate`, `TaskOutput`
- `WorkerCreate`, `WorkerObserve`, `WorkerSendPrompt`, `WorkerObserveCompletion`
- `TeamCreate`
- `CronCreate`

Claw Code is therefore a good reference for a lightweight task/worker control
surface, file-backed manifests, and simple worker state machines. It is not the
same as Codex's mailbox/session-tree architecture.

## Framework Implications For AgentTeam

Regardless of whether the underlying runtime is Codex, Claude Code, or a
Claw-like harness, AgentTeam should impose the same upper-layer protocol.

### Subagent Input Bundle

Every delegated task should include:

- `role`
- `objective`
- `read_scope`
- `write_scope`
- `forbidden_actions`
- `expected_output_schema`
- `validation_commands`
- `escalation_conditions`

### Subagent Output Contract

Every subagent result should include:

- `status`
- `changed_files` or `proposed_changes`
- `validation_run`
- `findings`
- `blockers`
- `assumptions`
- `confidence`
- `next_action`

### Integration Rule

Central artifacts must only be modified by the orchestrator or a dedicated
integration agent in a serial path.

Subagents may return:

- review findings
- finding classification
- CR drafts
- scoped patches
- validation reports

They should not directly rewrite authoritative design state unless their
write_scope explicitly grants that authority and the change still goes through
validation and trace recording.

### Practical Role Split

Claude-style agents are most useful for:

- external independent review
- adversarial risk analysis
- CR drafts
- isolated semantic judgment
- hook-backed quality gates

Codex-style agents are most useful for:

- the local root orchestrator
- parallel bounded subtasks
- resumable worker repair loops
- integration against local artifacts and git history

Claw-style mechanisms are most useful for:

- compact implementation examples
- file-backed agent manifests
- worker readiness and prompt-delivery state machines
- simple task/team registries

## Tool System

Codex:

- Tool schema definitions are centralized in `codex-rs/tools`.
- Tool execution logic is spread across `codex-core`, especially `exec`, shell, MCP, web search, skills, plugins, and related modules.
- Tool exposure is protocol-aware and provider-aware: tools are transformed into Responses API tool specs or dynamic/deferred MCP/plugin tools.
- Strong separation between tool definition, permission config, protocol events, and actual execution backend.

Claw Code:

- Tool specs and execution are centralized in `srccode/claude/claw-code/rust/crates/tools/src/lib.rs`.
- `mvp_tool_specs()` returns built-in tool schemas and required permissions.
- `execute_tool()` is a large name-based dispatcher.
- Built-in surface includes bash, read/write/edit file, glob/grep, web fetch/search, todo, skill, agent, tool search, notebook edit, plan mode, structured output, tasks, workers, team/cron, LSP, MCP, and testing helpers.
- This makes Claw easier to read end-to-end, but the dispatcher is more monolithic.

## Permissions And Sandboxing

Codex:

- Permission and sandboxing are first-class across many crates.
- `codex-core` models permission profiles and maps them to platform sandbox policies.
- Linux uses bubblewrap/Landlock paths; macOS uses Seatbelt; Windows has restricted/elevated sandbox backends.
- Exec handling is asynchronous, cancellation-aware, output-capped, and sandbox-aware.

Claw Code:

- `PermissionMode` and `PermissionPolicy` live in `runtime`.
- Tool specs carry a required permission.
- `tools/src/lib.rs` dynamically classifies bash/PowerShell commands before enforcing.
- Runtime has sandbox detection and command execution helpers, but it is a thinner and less platform-deep design than Codex.

## Provider Layer

Codex:

- Built around OpenAI/Codex auth and model provider abstractions, with additional support crates for model provider info, LM Studio, Ollama, network proxy, realtime, and Responses API proxy.
- Provider logic is integrated with core protocol/event flow.

Claw Code:

- `rust/crates/api` contains provider clients.
- `providers/anthropic.rs` supports `ANTHROPIC_API_KEY`, bearer token via `ANTHROPIC_AUTH_TOKEN`, configurable base URL, retry policy, prompt cache tracking, and streaming SSE.
- Also has OpenAI-compatible provider support.
- It is easier to inspect provider behavior in isolation.

## Config / Project Memory

Codex:

- Uses `~/.codex/config.toml`, AGENTS.md handling, skills, plugins, collaboration modes, feature flags, permissions, MCP, memories, and app/server configuration.
- Strong schema/config system and many compatibility layers.

Claw Code:

- Uses `.claw.json` / `.claw/settings.json` style config.
- Supports CLAUDE.md/project memory, hooks, plugin config, MCP config, model aliases, permission modes, and session resume.
- Simpler but less mature.

## Testing / Parity Harness

Codex:

- Broad production test suite across core, TUI, sandboxing, realtime, permissions, MCP, exec, etc.
- Many crates and integration paths.

Claw Code:

- Has a dedicated mock Anthropic-compatible service and parity harness.
- `rust/MOCK_PARITY_HARNESS.md`, `rust/mock_parity_scenarios.json`, and `rust/crates/mock-anthropic-service` are important for understanding intended behavior.
- The project explicitly tracks "parity" against Claude-Code-like behavior in `PARITY.md`.

## Practical Study Order

Study Claw Code first if the goal is to understand a compact Claude-Code-like harness:

1. `srccode/claude/claw-code/rust/README.md`
2. `srccode/claude/claw-code/rust/Cargo.toml`
3. `srccode/claude/claw-code/rust/crates/rusty-claude-cli/src/main.rs`
4. `srccode/claude/claw-code/rust/crates/runtime/src/lib.rs`
5. `srccode/claude/claw-code/rust/crates/runtime/src/conversation.rs`
6. `srccode/claude/claw-code/rust/crates/tools/src/lib.rs`
7. `srccode/claude/claw-code/rust/crates/api/src/providers/anthropic.rs`
8. `srccode/claude/claw-code/PARITY.md`

Then compare Codex production design:

1. `srccode/codex/codex-rs/README.md`
2. `srccode/codex/codex-rs/Cargo.toml`
3. `srccode/codex/codex-rs/cli/src/main.rs`
4. `srccode/codex/codex-rs/core/README.md`
5. `srccode/codex/codex-rs/core/src/lib.rs`
6. `srccode/codex/codex-rs/core/src/codex_thread.rs`
7. `srccode/codex/codex-rs/core/src/exec.rs`
8. `srccode/codex/codex-rs/tools/src/lib.rs`

## Key Takeaway

Claw Code is useful as a compact map of how a Claude-Code-style terminal agent can be assembled:

CLI -> config/session -> provider streaming -> model tool calls -> permission gate -> tool execution -> session persistence.

Codex is useful as the production-grade version of the same general problem:

CLI/TUI/app-server clients -> protocol operations/events -> thread/session runtime -> provider/model layer -> permissions/sandboxing -> tools/MCP/plugins/skills -> rollout/state/telemetry.

If the goal is learning design, Claw gives the shorter path. If the goal is production engineering quality, Codex is the stronger reference.

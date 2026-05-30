# Open Source Landscape For Native AgentTeam Runtime

Status: research note for the native AgentTeam runtime experiment.

This note scans adjacent open-source systems and protocol work relevant to a
lightweight invocation and communication layer for mature coding agents such as
Codex, Claude Code, Aider, OpenCode, Gemini CLI, and similar tools.

It is not an implementation plan. Its purpose is to clarify what should be
copied, avoided, or adapted before this experiment grows more code.

## Research Question

Can AgentTeam build a general-purpose, lightweight agent invocation and
communication system around existing mature agent CLIs without becoming tied to
one vendor, one task domain, or the current Codex subagent mechanism?

The target shape is:

```text
AgentTeam scheduler / mailbox / event log / artifact gate
  -> runtime adapters
       -> Codex, Claude Code, Aider, OpenCode, Gemini CLI, etc.
  -> shared communication substrate
       -> durable messages, leases, events, results, validation
```

## Primary References

### ComposioHQ Agent Orchestrator

Repository: https://github.com/ComposioHQ/agent-orchestrator

Agent Orchestrator is the closest reference for production-style parallel
coding sessions. Its core model is:

- one orchestrator process and dashboard;
- each issue or task gets an isolated git worktree;
- workers use pluggable agent adapters such as Claude Code, Codex, Aider,
  Cursor, OpenCode, and others;
- runtime backends are pluggable, including tmux, process/ConPTY, and Docker;
- feedback from CI and review comments is routed back to the agent;
- session state is tracked through lifecycle states and metadata.

Design signals worth borrowing:

- Plugin slots are a good abstraction boundary. AO separates runtime, agent,
  workspace, tracker, SCM, notifier, and terminal concerns instead of building
  a single hardcoded runner.
- The spawn path is explicit: validate task, reserve session id, create
  workspace, build prompt, launch agent, persist metadata.
- Worktree isolation is the practical minimum for parallel code modification.
- Flat metadata can be a valid M0 choice because it is easy to inspect and
  recover during local development.

Limits for AgentTeam:

- AO is primarily optimized around coding issues, PRs, CI, and review loops.
  That is valuable but narrower than AgentTeam's desired general role-agent
  runtime.
- The orchestration unit is mostly a coding session, not a durable role agent
  with its own long-lived mailbox, subscriptions, and semantic artifact duties.
- It gives us strong runtime and plugin lessons, but not a complete answer for
  hierarchical knowledge governance or semantic feedback into design artifacts.

### Overstory

Repository: https://github.com/jayminwest/overstory

Overstory is the closest reference for our native runtime direction. Its
public README describes a persistent coordinator, role workers, isolated git
worktrees, SQLite mail, typed protocol messages, web UI, runtime adapters, and
watchdog layers.

Design signals worth borrowing:

- Treat "agent" as a persistent role with identity and lifecycle, not merely as
  a single model call.
- Use durable mail as the communication substrate. This is closer to our
  mailbox/event-log idea than simply passing conversation history between
  subagents.
- Keep runtime adapters behind an interface. Overstory's `AgentRuntime`
  separates spawn command construction, config deployment, readiness detection,
  transcript parsing, environment construction, and direct/headless process
  support.
- Keep role access separate from runtime. Read-only scout/reviewer roles and
  read-write builder/merger roles should have different enforcement paths.
- Watchdog should be tiered: mechanical liveness first, AI-assisted diagnosis
  only when mechanical signals are not enough.
- Headless mode and structured event streams are much better integration
  targets than terminal scraping when a CLI supports them.

Limits for AgentTeam:

- Overstory is still coding-agent-centric. Its roles and merge flow are
  naturally oriented toward repository worktrees.
- It is already fairly large. AgentTeam should borrow the concepts but keep M0
  much thinner.
- Its README says active development has moved toward Warren, so Overstory
  should be treated as a design reference, not as a stable dependency.

## Protocol And Framework References

### MCP

Reference: https://modelcontextprotocol.io/docs/learn/architecture

MCP is useful for tool and context access, not as the whole multi-agent
coordination layer. It gives us:

- JSON-RPC style request/response and notifications;
- tool/resource/prompt discovery;
- stdio and Streamable HTTP transports;
- a widely adopted way to expose external capabilities to Codex, Claude Code,
  and other hosts.

AgentTeam should use MCP as a tool/context adapter layer where possible. It
should not overload MCP to represent task ownership, leases, role identity, or
artifact authority unless those semantics are explicitly modeled above MCP.

### A2A

Reference: https://google-a2a.github.io/A2A/specification/

A2A is relevant because it targets communication between independent, opaque
agent systems. It is more appropriate than MCP when the counterparty is another
agent application rather than a tool server.

For AgentTeam, A2A is worth tracking for external interoperability. M0 should
not require it. A simpler local mailbox protocol is easier to debug, easier to
evolve, and closer to our immediate need.

### LangGraph / LangChain Multi-Agent Patterns

Reference: https://docs.langchain.com/oss/python/langchain/multi-agent/index

LangChain's current docs distinguish subagents, handoffs, skills, routers, and
custom workflows. The useful lesson is not the library dependency; it is the
taxonomy:

- subagents give context isolation and parallelism, but repeat work across
  invocations;
- handoffs preserve state across turns, but are more sequential and require
  careful context engineering;
- routers are cheap for classification, but stateless by default;
- custom workflows give deterministic control when routing must be explicit.

AgentTeam should use this taxonomy to avoid calling every delegation pattern
"multi-agent." Long-lived role agents need durable state and mailbox semantics,
not only tool-style subagent calls.

### OpenAI Agents SDK

Reference: https://openai.github.io/openai-agents-python/handoffs/

The Agents SDK handoff model is useful conceptually because handoffs are exposed
to the model as tools, and input filters can control what context transfers to
the next agent.

For AgentTeam, the important design signal is selective context transfer. A
role agent should receive a compact context pack and structured task envelope,
not the full parent conversation by default.

### CrewAI

Reference: https://docs.crewai.com/en/learn/hierarchical-process

CrewAI's hierarchical process shows the common manager-worker pattern: a manager
agent plans, delegates, and validates work. This is useful as vocabulary, but it
is too high-level for our runtime problem because it does not directly solve
long-running CLI sessions, workspace isolation, mailbox durability, or artifact
authority.

## Initial Conclusions

1. The closest open-source references are AO and Overstory, but they solve
   different halves of the problem.

   AO is stronger on production coding-session orchestration, plugin slots,
   worktree lifecycle, tracker integration, and CI/review feedback.

   Overstory is stronger on persistent coordinator/worker semantics, durable
   mail, typed messages, runtime adapters, role access control, and watchdogs.

2. AgentTeam should not directly clone either architecture.

   The desired system is more general than AO's issue-to-PR lifecycle and should
   stay lighter than Overstory's full local swarm stack in M0.

3. The likely core should be:

   ```text
   scheduler
   + durable agent state
   + mailbox
   + append-only event log
   + artifact validator gate
   + runtime adapters
   + workspace/sandbox policy
   ```

4. The runtime adapter should be the main compatibility layer for Codex and
   Claude Code.

   A useful adapter is not just "run this command." It should normalize:

   - spawn mode: interactive, headless, background, one-shot;
   - prompt/config deployment: AGENTS.md, CLAUDE.md, system prompt files;
   - message delivery: stdin, tmux send, background session command, RPC;
   - readiness and liveness;
   - transcript and structured event parsing;
   - model/profile selection;
   - permission and sandbox settings.

5. The communication layer should start simpler than A2A.

   A local durable mailbox plus event log is enough for M0 and better aligned
   with debugging. A2A can be a future external protocol bridge.

6. MCP should be treated as tool and context plumbing, not as the agent team
   control plane.

   This keeps task ownership, leases, authority, artifact updates, and result
   validation inside AgentTeam rather than hiding them in MCP tools.

7. The main design risk is confusing role semantics with process mechanics.

   A role agent is a durable responsibility boundary. A process is only one
   runtime incarnation used to perform a step. The scheduler may wake the same
   role through Codex today and Claude Code tomorrow if the role contract and
   mailbox protocol stay stable.

## Proposed Research Backlog

M1 research should inspect source-level details in this order:

1. AO plugin interfaces and session lifecycle:
   - runtime interface;
   - agent interface;
   - workspace interface;
   - lifecycle manager;
   - prompt builder;
   - session metadata format.

2. Overstory runtime and messaging internals:
   - `src/runtimes/types.ts`;
   - Claude/Codex runtime adapters;
   - `src/mail`;
   - `src/watchdog`;
   - agent overlay generation;
   - guard enforcement.

3. Codex and Claude Code integration surfaces:
   - headless execution and output formats;
   - background session management;
   - MCP configuration;
   - project instruction files;
   - sandbox and permission profiles.

4. Protocol bridge feasibility:
   - local mailbox as native protocol;
   - MCP as tool/context adapter;
   - A2A as optional external agent bridge.

## Design Bias For AgentTeam

Build the smallest native layer that mature agent tools can plug into:

```text
AgentTeam owns:
  identity, mailbox, leases, events, artifact authority, scheduling policy

Codex / Claude Code / other tools own:
  model interaction, code editing, shell/tool execution, MCP access, local UI

Adapters normalize:
  spawn, send, wait, stop, transcript, permissions, workspace policy
```

This keeps AgentTeam generic while still reusing the strongest parts of mature
coding agents.

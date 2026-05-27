# AgentTeam

AgentTeam is a research and design workspace for building a hierarchical multi-agent framework that can turn ambiguous complex tasks into structured artifacts, validated plans, and bounded implementation work.

The core idea is simple:

```text
Do not build a virtual company meeting.
Build an artifact workflow with validators, bounded agents, change requests, traces, and empirical probes.
```

## Repository Structure

```text
.
├── design/      # Current framework architecture, workflow SOPs, and archived design history
├── research/    # Multi-agent literature, tool mechanics, and design guidance
└── srccode/     # Local reference source code and architecture notes
```

## Design Documents

- `design/README.md`
  - Entry point for the current design docs.
  - Defines the reading order and authority map.

- `design/architecture.md`
  - Current high-level architecture.
  - Defines AgentTeam positioning, layered system structure, agent hierarchy, validation philosophy, model allocation, implementation-pack boundary, risks, and success criteria.

- `design/artifact_workflow_sop.md`
  - Current execution SOP for AgentTeam semantic-design runs.
  - Defines the `output/current/` artifact structure, canonical registry, CR queue, integration pass, lint gates, semantic/adversarial review, trace requirements, and implementation handoff.
  - Owns concrete schemas and gates such as versioning, content hashes, `INDEX.json` routing, CR baseline metadata, conflict detection, and domain check schema.

- `design/implementation_workflow_sop.md`
  - Current execution SOP for turning an approved semantic design into bounded code changes.
  - Defines repo grounding, language packs, localization, context packs, task cards, workspace sandbox policy, verification, integration, and failure routing.
  - Treats repo indexes as navigation layers, not full project understanding.

- `design/archive/`
  - Historical design notes, including the early problem definition, original system blueprint, and experiment revision document.
  - These explain how the current architecture evolved but are no longer current execution authority.

## Research Documents

- `research/multi_agent_field_work.md`
  - Surveys related work such as MetaGPT, ChatDev, AgentVerse, AutoGen, AgentEval, DyLAN, GPTSwarm, FrugalGPT, and MasRouter.
  - Keeps both directly useful mechanisms and longer-term background context.

- `research/codex_vs_claude_agent_mechanics.md`
  - Compares Codex and Claude Code style subagent mechanics.
  - Concludes that both need an upper-layer structured protocol because natural-language subagent reports are not reliable execution facts.

- `research/design_guidance_insights.md`
  - Distills research and experiments into direct design guidance.
  - Emphasizes artifact-first workflow, validator gates, CR/trace governance, bounded subagent tasks, and implementation packs.

## Source References

`srccode/` contains local reference repositories used for studying existing agent systems:

- `srccode/codex/`
  - OpenAI Codex source.
  - Main reference for thread/session based multi-agent control, agent paths, mailbox delivery, wait semantics, and parent-child agent lifecycle.

- `srccode/claude/claude-code/`
  - Official Anthropic Claude Code public repository.
  - Useful for plugins, commands, hooks, and agent prompts.
  - It is not the full core Claude Code CLI source.

- `srccode/claude/claude-agent-sdk-python/`
  - Official Python Agent SDK reference.
  - Useful for subagent definitions, lifecycle hooks, transcript storage, and session store concepts.

- `srccode/claude/claude-agent-sdk-typescript/`
  - Official TypeScript Agent SDK reference and examples.

- `srccode/claude/claude-code-action/`
  - GitHub Action integration for Claude Code.

- `srccode/claude/claude-code-base-action/`
  - Lower-level Claude Code Action implementation.

- `srccode/claude/claw-code/`
  - Public Rust implementation of a Claude-style agent harness.
  - Useful as a lightweight reference for agent tools, task registries, worker state machines, and file-backed agent output.

- `srccode/codex-vs-clawcode-architecture.md`
  - Local architecture comparison between Codex and Claw Code.

## Core Framework Direction

The intended framework should use these units as first-class artifacts:

- `task_brief`: task goal, success definition, and non-goals
- `constraints`: hard constraints, preferences, assumptions, and budget
- `acceptance_contract`: objective criteria for completion
- `registry`: stable IDs, paths, owners, versions, and dependencies
- `authority_class`: semantic contract, implementation authority, derived observation, or evidence note classification for each artifact
- `validation_plan`: mechanical checks, semantic reviews, and empirical probes
- `change_request`: the only entry point for design changes
- `trace`: replayable record of inputs, steps, outputs, validation, and evidence
- `implementation_pack`: source layout, build contract, test harness, prerequisites, milestone outline, first task-card seed, task-card generation policy, and progress schema
- `agent_dispatch`: structured subagent invocation packet with scope, tools, schema, budget, and stop conditions
- `agent_result`: structured subagent return packet with status, evidence, changed files or findings, and next action
- `design_finding`: implementation evidence that requests controlled design escalation without granting write authority to the worker
- `risk_assessment`: orchestrator-owned classification that chooses the evidence level for an implementation result
- `repo_index`: derived tool-generated navigation facts with provenance, confidence, and stale conditions
- `context_pack`: task-local source context selected from the repo index and real files

The framework should combine:

- Program validators for mechanical invariants.
- Agent reviewers for semantic risks and hidden gaps.
- CR and trace for cross-document synchronization.
- Empirical probes for early runtime falsification.
- Bounded worker task cards for implementation.
- Repo grounding and context packs for large-codebase implementation.

## Design Principles

1. Prefer artifacts over chat history.
2. Prefer structured input and output over natural-language reports.
3. Let validators check mechanical consistency before agent review.
4. Let reviewers produce classified findings, not direct edits to authority documents.
5. Keep central artifacts under serial integration.
6. Give subagents explicit read scope, write scope, output schema, validation commands, and escalation conditions.
7. Invoke subagents through recorded dispatch packets and accept only structured result packets.
8. Do not treat a subagent `done` message as proof of correctness.
9. Generate an implementation pack before assigning code work.
10. Start implementation with a minimal empirical probe before scaling parallel work.
11. Treat repository indexes as navigation aids, not source-of-truth understanding.
12. Let implementation workers report design gaps, but route authority changes through orchestrator-gated CR integration.
13. Separate semantic architecture from implementation structure documents with explicit `authority_class`.
14. Default implementation evidence to L1 and escalate only when the Risk Classifier Gate requires L2/L3.
15. Give workers task-local context packs instead of whole repositories.
16. Use trace data for future model routing, agent replacement, and workflow optimization.

## Recommended Next Step

The current documents point to one concrete next step: build the minimum
implementation workflow for the framework itself.

That minimum workflow should define:

- repo inventory and project detection
- verification contract
- source layout contract
- build contract
- test harness contract
- environment prerequisites
- M0 empirical probe
- task context pack format
- first milestone task card
- implementation progress schema

Only after that should implementation be delegated to worker agents, starting
with a single bounded M0 slice in the current workspace or an isolated worktree.

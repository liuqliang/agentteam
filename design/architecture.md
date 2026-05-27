# AgentTeam Architecture

## Positioning

AgentTeam is a hierarchical multi-agent framework for complex, ambiguous tasks.
Its purpose is not to simulate a company meeting. Its purpose is to turn a vague
goal into governed artifacts, validate those artifacts mechanically and
semantically, then hand bounded work to implementation agents.

The core design direction is:

```text
vague task
  -> artifact contract
  -> validator gates
  -> semantic review
  -> change requests
  -> serial integration
  -> replayable trace
  -> implementation pack
  -> repo grounding
  -> task context packs
  -> bounded worker tasks
```

Exact semantic-design workflow details live in `artifact_workflow_sop.md`.
Exact code-implementation workflow details live in
`implementation_workflow_sop.md`.

## Revision Basis

The original design assumed a Proposer + Reviewer + Devil's Advocate loop could
produce a reliable design. The compiler experiment showed that this is useful
but insufficient.

The stable unit of collaboration must be an artifact set, not a long document.
Reviewers can find semantic risks, but they cannot reliably enforce every
cross-document invariant. Program validators can enforce structure, references,
versions, hashes, and CR closure, but they cannot judge all task semantics.

The resulting architecture separates responsibilities:

- agents propose, challenge, classify, and repair semantic content;
- validators enforce mechanical consistency;
- change requests are the only design-change entry point;
- integration is serial for authoritative artifacts;
- traces make the path replayable;
- empirical probes test whether the paper design survives runtime contact;
- implementation packs convert semantic design into executable worker tasks;
- repo indexes guide context selection but do not replace reading real source.

## Layered System

```text
Layer 0: Infrastructure
  Validators, schema checks, content hashing, state storage, trace logging,
  execution harnesses, and empirical probes.

Layer 1: Artifact Knowledge
  Task brief, constraints, acceptance contract, domain pack, canonical
  registry, validation plan, CR queue, traces, implementation pack, repo index,
  context packs, and task cards.

Layer 2: Orchestration
  Phase control, subagent dispatch, context packaging, agent routing, budget
  control, CR ordering, integration decisions, and final gatekeeping.

Layer 3: Specialist Agents
  Constraint extractor, domain classifier, registry architect, domain
  proposer, semantic reviewer, adversarial reviewer, validation planner, and
  implementation workers.
```

The orchestrator owns phase transitions and authority boundaries. Specialist
agents own bounded judgments or proposals. They do not silently mutate shared
truth.

## Agent Hierarchy

The hierarchy is artifact-centered:

| Role | Responsibility | Authority |
|---|---|---|
| Orchestrator | Controls phases, context bundles, budgets, CR queue, implementation-to-design escalation, trace logging, and final gate. | May accept/reject transitions, escalation requests, and integration results. |
| Constraint Agent | Extracts goals, hard constraints, assumptions, non-goals, and missing dimensions. | Produces draft constraints; does not decide hidden assumptions silently. |
| Domain Classifier | Selects or drafts the domain pack for the task. | Proposes required artifact classes and domain checks. |
| Registry Architect | Defines canonical symbols, ownership, dependencies, and shared facts. | Owns registry structure. |
| Domain Proposer | Designs bounded parts of the target system. | Produces change requests, not direct final edits. |
| Integration Agent | Applies accepted CRs to current artifacts. | Updates authoritative artifacts serially. |
| Spec Linter | Runs mechanical checks. | Blocks the process on structural failures. |
| Semantic Reviewer | Checks implementability, hidden contradictions, readiness, and missing probes. | Produces findings and verdicts. |
| Adversarial Reviewer | Searches for likely failure modes and brittle assumptions. | Produces blockers or redesign risks. |
| Validation Planner | Converts acceptance criteria into checks and empirical probes. | Owns the validation plan draft. |
| Repo Grounding Agent | Builds or refreshes repository inventory, verification contract, test surface, and language-pack facts. | Produces derived repo index artifacts, not semantic authority. |
| Context Pack Builder | Selects task-local source context from the repo index and real files. | Produces bounded context packs and task cards. |
| Worker Agent | Implements bounded task cards from an implementation pack and reports implementation evidence. | Writes only within declared scope; may request design escalation but cannot launch authority-writing agents. |
| Risk Classifier Gate | Classifies implementation risk from the task card, context pack, changed files, worker result, and verification status. | Assigns evidence level and review route; cannot edit code or semantic artifacts. |
| Patch Integration Agent | Reviews and integrates code patches returned from workers. | May integrate code changes serially; design changes still go through artifact CRs. |

This is deliberately stricter than a free-form multi-agent chat. Subagents can
be parallel only when their read scope, write scope, output schema, and
escalation path are explicit.

## Authority Classes

Do not use "architecture document" as an authority boundary. It is too broad.
AgentTeam classifies artifacts by what kind of truth they carry:

| Authority class | Meaning | Typical artifacts | Write path |
|---|---|---|---|
| `semantic_contract` | Defines what the system promises: behavior, responsibilities, external contracts, invariants, acceptance criteria, and canonical shared facts. | task brief, constraints, acceptance contract, registry, domain specs, validation plan | Design CR plus serial Integration Agent update. |
| `implementation_authority` | Defines how the current implementation run realizes the semantic contract: task-local decisions, workspace policy, implementation ADRs, module mapping, verification objects, progress. | implementation pack, task cards, context packs, workspace policy, verification objects, progress, implementation structure docs | Implementation workflow update within declared write scope; design CR only if the semantic contract changes. |
| `derived_observation` | Records generated or observed repository facts used for navigation. It can become stale and may be regenerated. | repo index, file inventory, module cards, language packs, dependency edges | Tool or grounding agent regeneration with provenance and stale conditions. |
| `evidence_note` | Records raw or summarized evidence from commands, reviews, worker results, or probes. | command output summaries, test logs, agent findings, trace attachments | Appended by the producing tool or agent; cannot redefine authority by itself. |

The boundary test is simple:

- changing what the system promises requires a design CR;
- changing how this implementation slice realizes the promise belongs to
  implementation authority;
- recording what the repository currently looks like is derived observation;
- recording why a decision was made is evidence.

Workers may write implementation structure documents only when their dispatch
or task card grants that write scope. Those documents must describe the current
implementation or a local ADR. They must not redefine semantic module
responsibilities, public behavior, acceptance criteria, or registry-owned
symbols without a design CR.

## Communication Model

Natural-language summaries are useful for humans, but they are not authoritative
state. Agent-to-agent and agent-to-orchestrator communication should be
structured:

- dispatch packets define role, goal, input artifacts, read scope, write scope,
  expected output schema, validation commands, budget, and escalation rules;
- returned results include status, output payload, assumptions, blockers,
  validation evidence, and trace references;
- implementation results carry risk signals; the orchestrator or Risk
  Classifier Gate assigns the formal evidence level;
- `semantic_contract` changes become change requests before integration;
- `implementation_authority` changes become task-card, context-pack,
  implementation-pack, or structure-doc updates with trace evidence;
- `derived_observation` changes are regenerated with provenance and stale
  conditions;
- implementation findings that question the design become escalation requests
  before any design agent is dispatched;
- implementation changes become task-card updates or code patches with test
  evidence.

Semantic workflow fields belong in `artifact_workflow_sop.md`.
Implementation workflow fields belong in `implementation_workflow_sop.md`.

## Subagent Dispatch

Subagent invocation is a first-class workflow event, not an informal chat turn.
Every subagent run that can affect an authority artifact must have a dispatch
record, a result record, and a trace reference.

Dispatch records define:

- role and purpose;
- input artifacts and their hashes;
- read scope and write scope;
- allowed tools and command policy;
- expected output schema;
- stop conditions;
- parent task, CR, context pack, or review round;
- budget and timeout.

Result records define:

- status: completed, blocked, failed, or cancelled;
- structured payload;
- assumptions and unknowns;
- changed files or proposed artifact changes, if any;
- verification evidence;
- recommended next action.

Subagents are read-only by default. A subagent may write only when its dispatch
record names an explicit write scope and the relevant SOP permits that role to
write. Authority artifacts are still integrated serially.

Subagent delegation is disabled by default. A worker may report a design gap or
request escalation in its result packet, but only the orchestrator may dispatch
a design CR agent, semantic reviewer, or Integration Agent. Any worker-started
agent output is evidence only until the orchestrator adopts it into a recorded
dispatch/result pair.

Platform-native agent tools are transport adapters, not authority mechanisms.
If the host tool cannot enforce read scope, write scope, output schema, or
lifecycle gates, AgentTeam must enforce them at the framework layer through
dispatch artifacts, workspace policy, result validation, trace checks, and
post-run diff/snapshot inspection. A raw subagent completion message is never a
valid result by itself.

## Validation Philosophy

AgentTeam uses three complementary validation types:

| Type | Purpose | Examples |
|---|---|---|
| Mechanical validator | Catch objective structure failures before expensive review. | JSON/schema validity, registry coverage, reference resolution, version/hash checks, CR closure, trace coverage. |
| Semantic reviewer | Catch reasoning failures that scripts cannot understand. | Missing assumptions, unrealistic scope, contradictory design, unimplementable interfaces, missing implementation pack. |
| Empirical probe | Falsify the design with the smallest runtime evidence. | Minimal end-to-end slice, smoke test, simulator run, compiled artifact, data-pipeline dry run. |

The validator should remain mostly domain-independent. Domain-specific checks
belong in domain packs unless they reveal a reusable validation pattern.

## Evidence Budget

Evidence is a control surface, not the work product. Implementation work should
default to lightweight evidence and escalate only when risk justifies it.

| Level | Use when | Evidence shape |
|---|---|---|
| `L0` | Trivial local edit with no interface or behavior contract change. | Changed files, diff summary, one verification result; documentation-only tasks may record a not-applicable reason. |
| `L1` | Default bounded task card. | L0 plus key files read, verification output summary, assumptions, and next action. |
| `L2` | Cross-module, internal API, non-contract schema, build/test/config, broad refactor, unavailable verification, or uncertain worker result. | L1 plus explicit risk classification, context/task hashes, patch review, and milestone trace. |
| `L3` | Semantic contract, externally visible API or protocol, registry-owned schema, security, permission, payment, data migration, concurrency, or blocking design finding. | Full CR/review/trace path and serial integration gates. |

The worker may report risk signals and self-assessed risk, but it does not set
the final evidence level. The orchestrator or Risk Classifier Gate sets the
level before integration. A worker can raise risk by providing evidence; it
cannot lower a rule-triggered level.

## State And Persistence

The current design set should be addressable through `output/current/` during a
run. Current artifacts should have stable IDs, versions, owners, statuses, and
content hashes. The current routing table is `INDEX.json`; consumers should not
infer authority from filenames.

Older versions should move to an archive before replacement. CRs and traces
should record which artifact versions and hashes were used so that a future
reviewer or worker can reconstruct the path.

## Model Allocation

Model choice should follow cognitive difficulty, not role prestige:

| Work type | Preferred capability |
|---|---|
| Orchestration and system design | strongest reasoning model available |
| Adversarial review and deep risk analysis | strongest or heterogeneous reviewer |
| Structured lint, routing, formatting, indexing | programmatic checks or smaller model |
| Bounded implementation tasks | mid-strength coding model, upgraded on repeated failure |
| Simple mechanical tasks | small model or script |

Escalation should be triggered by evidence: repeated validation failure,
high-complexity modules, unresolved semantic blockers, or missing empirical
probe coverage.

## Implementation Pack Boundary

A semantic contract says what the correct system is. It is not enough to hand
work to implementation agents.

Before implementation, AgentTeam should generate an implementation pack with:

- source layout contract;
- build contract;
- test harness contract;
- environment prerequisites;
- milestone outline;
- first task-card seed;
- task-card generation policy;
- write-scope boundaries;
- progress schema;
- error-handling and resource-management policy where relevant;
- the first empirical probe.

Workers should not infer directory structure, build commands, or ownership from
the whole design corpus.

After the implementation pack exists, AgentTeam should ground it in the real
repository through a lightweight repo index. That index is a navigation layer:
it stores paths, hashes, manifests, symbols, dependencies, tests, provenance,
confidence, and stale conditions. It must not be treated as complete project
understanding. A worker receives a task-local context pack, reads the relevant
source files, edits only its write scope, and verifies the change.

## Main Risks

| Risk | Mitigation |
|---|---|
| Proposer produces plausible but shallow plans. | Require artifact completeness, semantic review, and empirical probes. |
| Reviewers miss cross-artifact drift. | Run mechanical validators before semantic review. |
| Subagents overwrite shared truth independently. | Route all design changes through CRs and serial integration. |
| Implementation starts from an underspecified design. | Generate an implementation pack before worker assignment. |
| Repo index is mistaken for full understanding. | Treat it as navigation only; require workers to read real source before editing. |
| Context window overflows on large repositories. | Use task-local context packs and expand scope incrementally. |
| Parallel workers conflict. | Parallelize writes only when task cards have disjoint, explicit, hashed read/write closures; otherwise keep workers read-only or integrate serially. |
| Runtime failures are hidden by document approval. | Start with a minimal end-to-end probe before scaling. |

## Success Criteria

The framework is healthy when:

- current artifacts are registered, versioned, hashable, and indexed;
- shared facts live in the canonical registry;
- every accepted CR has integration evidence and a replayable trace;
- mechanical lint runs before semantic review;
- semantic findings become CRs or implementation task cards;
- every complex task has an early empirical probe;
- implementation workers receive bounded context packs and task cards, not a
  whole repository or pile of documents;
- repo index facts record provenance, confidence, and stale conditions;
- progress can be reconstructed from artifacts, CRs, traces, and validation
  output.

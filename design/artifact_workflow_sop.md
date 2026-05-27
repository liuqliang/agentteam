# Artifact Workflow SOP

Status: current execution authority for AgentTeam semantic-design runs.

This document defines the operational process. Conceptual architecture,
responsibility boundaries, model allocation, and design rationale live in
`architecture.md`.

You are orchestrating a multi-agent design workflow for complex, ambiguous tasks. The goal is not to let agents freely write a large design document. The goal is to turn a vague task into a set of structured, versioned, machine-checkable artifacts that can be reviewed, validated, replayed, and eventually executed.

This workflow must work for different domains: software systems, data pipelines, research reports, simulations, product prototypes, compiler designs, and other complex projects. Domain-specific details belong in domain packs. The orchestration pattern is domain-independent.

## Core Principle

Agents may propose designs and identify risks, but they must not freely invent shared facts in isolated documents.

Shared facts must live in a canonical registry. Domain specs may reference those facts, but must not redefine them. Subagents do not directly patch final specs. They submit structured change requests, which are integrated centrally and checked before semantic review.

The workflow is artifact-centric:

```
vague task
  -> constraints.json
  -> domain_pack/domain_pack.json
  -> registry/*.json
  -> specs/*.json or specs/*.md
  -> change_requests/*.json
  -> validation/validation_plan.json
  -> spec_lint report
  -> semantic review
  -> adversarial review
  -> probe plan / implementation handoff
```

## Required Output Structure

Create and maintain this structure under `output/`:

```
output/
├── current/
│   ├── INDEX.json
│   ├── task_brief.json
│   ├── constraints.json
│   ├── acceptance_contract.json
│   ├── domain_pack/
│   │   ├── domain_pack.json
│   │   └── checks.json
│   ├── registry/
│   │   ├── artifacts.json
│   │   ├── canonical_symbols.json
│   │   ├── constraints.json
│   │   └── ownership.json
│   ├── change_requests/
│   │   └── CR-<N>-<short_name>.json
│   ├── validation/
│   │   └── validation_plan.json
│   ├── traces/
│   │   └── TRACE-<N>-<short_name>.json
│   ├── agent_dispatches/
│   │   └── DISPATCH-<N>-<short_name>.json
│   ├── agent_results/
│   │   └── RESULT-<N>-<short_name>.json
│   └── specs/ or domain-specific payload artifacts
├── lint/
│   └── spec_lint_round_<N>.json
├── reviews/
│   ├── semantic_review_round_<N>.json
│   └── adversarial_review_round_<N>.json
├── traces/
│   └── run_trace.jsonl  (optional chronological event log)
└── archive/
    └── v<N>/
```

`output/current/` is the only authoritative design set. Archive older versions before replacing current artifacts.

## Artifact Identity, Versioning, And Indexing

Every authoritative artifact must be addressable, versioned, and attributable.
This is not just for documentation convenience. It is required for replay,
conflict detection, validation attribution, and routing future agent work.

JSON artifacts should include a `_meta` object:

```json
{
  "_meta": {
    "artifact_id": "stable_id",
    "artifact_type": "task_brief | constraints | acceptance_contract | domain_pack | registry | spec | change_request | validation_plan | review | trace | agent_dispatch | agent_result | implementation_pack | verification_object | context_pack | task_card | workspace_policy | progress",
    "version": 1,
    "status": "draft | current | archived | superseded",
    "owner": "orchestrator | registry_architect | integration_agent | patch_integration_agent | context_pack_builder | validator",
    "source_cr": "CR-001-example",
    "created_at": "ISO-8601",
    "updated_at": "ISO-8601",
    "last_modified_by": "agent_or_tool_id",
    "content_hash": "sha256:<hash>"
  }
}
```

Markdown specs may use equivalent frontmatter or a sidecar `.meta.json`, but
the same fields are still required. `content_hash` should be computed over the
artifact content excluding the hash field itself.

`INDEX.json` is the authoritative routing table for the current design set. It
must identify:

- every current artifact id and path
- artifact type, owner, status, version, and content hash
- registry-owned namespaces
- latest lint report path
- latest semantic review path
- latest adversarial review path
- latest validation plan path
- latest implementation index path, if implementation has started
- latest agent dispatch and result records used by current artifacts
- latest accepted trace ids
- current archive version

Consumers must not infer "latest" from filenames alone. They must read
`INDEX.json`.

## Artifact Roles

### `INDEX.json`

The entrypoint for every reviewer, validator, integration pass, and worker
task. It states which artifacts are authoritative and where their latest
validated versions live.

### `task_brief.json`

Human-readable summary of the task, project intent, domain, and execution assumptions.

### `constraints.json`

Structured constraints extracted from the user request. It must distinguish:

- `hard_constraints`: must be satisfied
- `soft_preferences`: should be satisfied if possible
- `inferred_defaults`: assumptions made by agents
- `open_questions`: missing or risky dimensions
- `non_goals`: explicitly excluded scope

Each inferred default must include `confidence`, `impact`, and `requires_user_confirmation`.

### `domain_pack/domain_pack.json`

Names the chosen domain pack and the artifact templates/checks it requires. Examples:

- `software_system`
- `data_pipeline`
- `research_report`
- `frontend_product`
- `simulation`
- `compiler_or_language_tooling`
- `custom`

The pack defines which registry files, spec files, and domain lint checks are required.

### `acceptance_contract.json`

Maps every acceptance criterion to a verification method:

```json
{
  "criteria": [
    {
      "id": "AC-001",
      "statement": "User-visible pass/fail condition",
      "verification_type": "automated_test | static_check | empirical_probe | human_review",
      "verification_artifact": "path or command",
      "owner": "validator | reviewer | human",
      "blocking": true
    }
  ]
}
```

No acceptance criterion may remain unmapped.

### `registry/*.json`

Canonical shared facts. Any concept referenced by more than one spec must be defined here exactly once.

Typical examples:

- software: modules, APIs, route names, database tables, message schemas
- compiler: token kinds, AST nodes, IR opcodes, ABI constants, frame layout
- data pipeline: datasets, column schemas, transforms, quality rules
- research report: claims, sources, evidence IDs, section IDs
- simulation: entities, state variables, actions, transition rules

### `specs/*`

Domain-specific design artifacts. Specs may contain detailed prose, pseudocode, diagrams, examples, or tables, but all shared identifiers must reference registry entries.

### `change_requests/*.json`

Subagents propose changes through change requests. They do not directly rewrite authoritative artifacts.

Each change request must include enough metadata to make ordering, conflict
detection, rollback, and review attribution possible:

- `change_id`
- `status`: `proposed | accepted | rejected | integrated | superseded`
- `author`
- `created_at`
- `depends_on`
- `supersedes`
- `base_artifact_versions`
- `base_artifact_hashes`
- `affected_artifacts`
- `validation_required`
- `rollback_plan`
- `integration_result`

### `validation/validation_plan.json`

Defines the minimum proof needed before execution. It must include at least one early end-to-end probe, not just isolated unit checks.

## Roles

Use agents by artifact responsibility, not by human-company titles.

### Orchestrator

Owns phase transitions, context bundles, budgets, trace logging, and final gating. The orchestrator does not silently resolve design contradictions. It either routes a change request or marks a blocker.

### Constraint Agent

Extracts `constraints.json`, `task_brief.json`, and open questions from the user request.

### Domain Classifier

Selects `domain_pack/domain_pack.json`. If multiple packs apply, it must state the primary pack and secondary packs.

### Registry Architect

Creates or updates `registry/*.json`. It decides which shared facts are canonical and which files own which concepts.

### Domain Proposer Agents

Produce local design proposals for specific specs. They receive context bundles that include relevant registry entries and impacted specs. They return change requests, not direct final edits.

### Integration Agent

Applies accepted change requests to the registry and specs. It must update every impacted artifact in the same integration pass.

### Spec Linter

Runs deterministic checks before semantic review. If lint fails, do not invoke semantic reviewer except to diagnose unclear lint rules.

### Semantic Reviewer

Reviews engineering realism, hidden complexity, missing domain logic, and whether the design is actually implementable. It should not spend effort on undefined symbols, duplicate definitions, or API drift; those are linter responsibilities.

### Adversarial Reviewer

Finds fatal failure modes, ambiguous interpretations, and missing empirical probes.

### Validation Planner

Turns acceptance criteria and reviewer concerns into concrete checks, tests, probes, or human review points.

## Subagent Dispatch Protocol

The orchestrator invokes subagents through dispatch records. A subagent is not
considered part of the workflow unless its dispatch and result are recorded.

Semantic-design subagents are read-only by default. They may propose findings,
draft specs, or draft CRs, but they must not patch authoritative artifacts
directly. The Integration Agent is the only role that serially applies accepted
CRs to `output/current/`.

Implementation-originated design gaps enter this same protocol. An
implementation worker may be the source of evidence for a CR, but it does not
gain authority to dispatch design agents or edit semantic artifacts. The
orchestrator must convert the finding into a recorded design subagent dispatch
or CR draft before the artifact workflow can integrate it.

Minimum dispatch record:

```json
{
  "_meta": {
    "artifact_id": "DISPATCH-001-example",
    "artifact_type": "agent_dispatch",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "agent_role": "constraint_agent | domain_classifier | registry_architect | domain_proposer | semantic_reviewer | adversarial_reviewer | validation_planner",
  "purpose": "bounded reason for invoking this subagent",
  "input_artifacts": [
    {
      "artifact_id": "constraints",
      "path": "constraints.json",
      "content_hash": "sha256:<hash>"
    }
  ],
  "read_scope": ["artifact ids or paths"],
  "write_scope": [],
  "expected_output_schema": "schema id or inline shape",
  "allowed_tools": ["read", "rg", "review"],
  "stop_conditions": ["needs missing artifact", "finds scope contradiction"],
  "parent_phase": "Phase 3",
  "budget": {"max_tokens": 50000, "timeout_minutes": 20}
}
```

Minimum result record:

```json
{
  "_meta": {
    "artifact_id": "RESULT-001-example",
    "artifact_type": "agent_result",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "dispatch_id": "DISPATCH-001-example",
  "status": "completed | blocked | failed | cancelled",
  "output": {},
  "proposed_change_requests": ["CR draft ids or inline drafts"],
  "findings": [],
  "assumptions": [],
  "unknowns": [],
  "trace_refs": [],
  "recommended_next_action": "accept_cr | revise_dispatch | run_lint | semantic_review | stop"
}
```

If a subagent result is used to create or integrate a CR, the CR and trace must
reference both `dispatch_id` and `result_id`.

## Phase Flow

### Phase 0: Intake and Constraint Extraction

Produce:

- `task_brief.json`
- `constraints.json`
- `acceptance_contract.json`

Gate:

- Every hard constraint is represented.
- Every acceptance criterion has a verification method or is marked as requiring user clarification.
- High-impact inferred defaults must be surfaced before design proceeds.

### Phase 1: Domain Pack Selection

Produce:

- `domain_pack/domain_pack.json`
- initial `INDEX.json`

Gate:

- Required artifact set is explicit.
- Required lint checks are explicit.
- Scope boundaries are explicit.

### Phase 2: Canonical Registry Construction

Produce:

- `registry/artifacts.json`
- `registry/canonical_symbols.json`
- `registry/constraints.json`
- `registry/ownership.json`

Gate:

- Any shared identifier belongs to exactly one registry file.
- Specs are not allowed to redefine registry-owned facts.

### Phase 3: Local Design Proposal

Launch domain proposer agents with scoped context bundles. Each agent gets:

- task brief
- relevant constraints
- relevant registry entries
- assigned spec scope
- impacted artifact list
- prior related review gaps

Each proposer returns a structured `agent_result`; any CR must be included in
`agent_result.output` or emitted as a referenced CR draft before integration:

```json
{
  "_meta": {
    "artifact_id": "CR-001-short-name",
    "artifact_type": "change_request",
    "version": 1,
    "status": "proposed",
    "owner": "orchestrator",
    "source_cr": null,
    "created_at": "ISO-8601",
    "updated_at": "ISO-8601",
    "last_modified_by": "agent_id",
    "content_hash": "sha256:<hash>"
  },
  "change_id": "CR-001-short-name",
  "status": "proposed | accepted | rejected | integrated | superseded",
  "author": "agent_id",
  "created_at": "ISO-8601",
  "depends_on": ["CR-000-previous"],
  "supersedes": [],
  "base_artifact_versions": {
    "artifact_id": 1
  },
  "base_artifact_hashes": {
    "artifact_id": "sha256:<hash>"
  },
  "intent": "What this change is trying to accomplish",
  "rationale": "Why it is needed",
  "canonical_changes": [
    {
      "target_artifact": "canonical_symbols",
      "operation": "add | update | delete",
      "path": "$.namespaces.example",
      "value": {}
    }
  ],
  "spec_changes": [
    {
      "target_artifact": "example_spec",
      "operation": "add | update | delete",
      "path": "$.sections.example",
      "value": {}
    }
  ],
  "affected_artifacts": ["paths"],
  "validation_required": ["lint rule ids or probes"],
  "rollback_plan": "How to revert this change if integration or validation fails",
  "integration_result": {
    "status": "not_integrated | integrated | failed",
    "integrated_by": null,
    "integrated_at": null,
    "updated_artifacts": [],
    "trace_id": null
  },
  "risks": [
    {
      "risk": "What might break",
      "mitigation": "How the integration pass should guard against it"
    }
  ]
}
```

Gate:

- Subagents may write only non-authoritative local drafts. Any draft that should
  affect `output/current/` must become a CR and be integrated by the Integration
  Agent.
- CRs originating from implementation evidence must reference the worker
  `agent_result`, the implementation trace, and the affected implementation
  artifacts that will need invalidation or rebase.
- Every cross-artifact change must include `affected_artifacts`.
- Every change request must declare its base artifact versions and hashes.
- A change request that touches a stale artifact version must be rebased or rejected before integration.

### Phase 4: Integration Pass

The Integration Agent applies change requests to `output/current/`.

Rules:

- Acquire the serial integration lock before modifying `output/current/`.
- Process accepted change requests through an explicit CR queue.
- Reject or rebase any CR whose `base_artifact_versions` or `base_artifact_hashes` do not match current artifacts.
- Detect overlapping writes to the same registry key, spec path, or ownership entry before applying changes.
- Update registry before specs.
- Update all impacted specs in the same pass.
- Do not leave patch files as authoritative documents.
- Archive the previous current version before replacing it.
- Append a trace event for every accepted or rejected change request.
- Update `INDEX.json` with new artifact versions, hashes, latest validation outputs, and archive version.
- Write `integration_result` back to the CR.

The integration lock is conceptual in early manual runs and should become a
real file lock or transactional store once the workflow is automated.

### Phase 5: Spec Lint

Run core lint and domain lint. If no executable linter exists yet, perform a structured manual lint pass and save it under `output/lint/` using the same JSON result shape.

Core lint checks:

- `SCHEMA_VALID`: all JSON artifacts parse and match expected top-level shape
- `UNDEFINED_REFERENCE`: all shared identifiers resolve to registry entries
- `DUPLICATE_AUTHORITY`: no concept has multiple authoritative definitions
- `API_DRIFT`: signatures, return values, ownership, and parameter names are consistent
- `DEPENDENCY_CYCLE`: dependency graph has no unintended cycles
- `ACCEPTANCE_COVERAGE`: all acceptance criteria map to validation methods
- `CHANGE_IMPACT_COVERAGE`: every accepted change updated all impacted artifacts
- `INDEX_TRUTHFULNESS`: `INDEX.json` claims match actual artifact ownership
- `ARTIFACT_VERSION_VALID`: artifact versions are monotonic and match `INDEX.json`
- `ARTIFACT_HASH_VALID`: recorded content hashes match artifact content
- `CR_BASELINE_VALID`: accepted CRs were integrated against the artifact versions and hashes they declared
- `CR_CONFLICT_FREE`: no integrated CR silently overwrote another CR's touched paths
- `LATEST_POINTER_VALID`: `INDEX.json` latest lint/review/validation pointers exist and are current

Domain lint checks are declared in `domain_pack/checks.json` using reusable check types. Add Python code only when a new reusable check type is needed, not for each task-specific rule.

`domain_pack/checks.json` should use this shape:

```json
{
  "checks": [
    {
      "id": "DOMAIN-001",
      "name": "human readable check name",
      "check_type": "required_field | required_reference | enum_coverage | pattern_coverage | custom_reusable",
      "target_artifacts": ["artifact_id_or_glob"],
      "parameters": {},
      "blocking": true,
      "owner": "validator",
      "rationale": "Why this check matters for this domain"
    }
  ]
}
```

Gate:

- If any blocking lint check fails, return to Phase 3 or Phase 4.
- Do not run semantic review on known mechanical inconsistency unless the purpose is to debug the framework.

### Phase 6: Semantic Review

Invoke GPT/Codex or another heterogeneous reviewer only after lint passes.

Reviewer prompt requirements:

- Read `INDEX.json`, registry, specs, acceptance contract, validation plan, and latest lint report.
- Do not re-litigate mechanical issues already covered by lint unless lint missed them.
- Focus on implementability, realism, hidden complexity, missing domain logic, and unsafe assumptions.

Save to:

`output/reviews/semantic_review_round_<N>.json`

### Phase 7: Adversarial Review

The adversarial reviewer must identify:

- top fatal failure modes
- ambiguous interpretations that can split implementations
- required probes that would falsify the design early
- scope reductions if the budget is not credible

Save to:

`output/reviews/adversarial_review_round_<N>.json`

### Phase 8: Empirical Probe Plan

Produce or update `validation/validation_plan.json`.

Every complex task needs an early probe. Examples:

- software: minimal vertical slice, API smoke test, deployment dry run
- compiler: source -> output artifact -> simulator run
- data pipeline: sample input -> transformed output -> quality checks
- research report: one section -> claims -> citations -> adversarial fact check
- simulation: one scenario -> state transition log -> invariant checks

Gate:

- At least one probe must exercise the full critical path before 50 percent of the budget.

### Phase 9: Implementation Handoff

Only after the final deliverable gate below is satisfied, produce an
implementation handoff package. Code task cards are governed by
`implementation_workflow_sop.md`, not by this semantic-design SOP.

```
output/current/implementation/
  implementation_pack.json
  handoff_notes.md
```

The handoff must include:

- exact input artifacts
- acceptance contract and validation plan references
- expected source layout or unknowns
- build, test, and environment assumptions
- first empirical probe reference from `validation/validation_plan.json`
- repository-grounding questions
- rollback or failure handling

After this handoff, follow `implementation_workflow_sop.md` to build repo
indexes, context packs, bounded task cards, workspace policy, verification
traces, and integration records.

## Codex Review Invocation

When using Codex/GPT as semantic or adversarial reviewer, write the full prompt to a file and pipe via stdin:

```bash
cat output/.semantic_review_prompt.txt | codex exec \
  -s read-only \
  --skip-git-repo-check \
  -o output/reviews/semantic_review_round_<N>.json \
  -
```

Use the same pattern for adversarial review:

```bash
cat output/.adversarial_review_prompt.txt | codex exec \
  -s read-only \
  --skip-git-repo-check \
  -o output/reviews/adversarial_review_round_<N>.json \
  -
```

## Semantic Reviewer Prompt Template

```
You are a senior domain reviewer. The design has already passed mechanical lint checks unless the lint report says otherwise. Do not spend your review on simple undefined-symbol or duplicate-definition issues unless they are present in the lint report.

Your job is to determine whether this artifact set is actually implementable and realistic for the stated constraints.

Review:
1. Does the design cover the full acceptance contract?
2. Are the key algorithms, interfaces, states, data models, or domain rules sufficiently specified?
3. Are there hidden assumptions that would force an implementation agent to invent behavior?
4. Is the budget credible after accounting for integration and debugging?
5. Does the validation plan prove the critical path early enough?
6. Are there semantic contradictions that lint is unlikely to catch?

Output only valid JSON:

{
  "verdict": "APPROVE" | "CONDITIONAL_APPROVE" | "REJECT",
  "confidence_percent": 0,
  "scores": {
    "completeness": 1,
    "semantic_consistency": 1,
    "implementability": 1,
    "realism": 1,
    "validation_strength": 1
  },
  "blocking_gaps": [
    {
      "id": "G-001",
      "title": "",
      "severity": "HIGH|MEDIUM|LOW",
      "evidence": "Specific artifact path and quoted or summarized evidence",
      "required_change": "Concrete change needed"
    }
  ],
  "recommended_scope_reductions": [],
  "approved_aspects": []
}
```

## Adversarial Reviewer Prompt Template

```
You are an adversarial reviewer. Assume the project will fail. Find the most likely ways it fails despite passing mechanical lint.

Focus on:
- false confidence from incomplete validation
- expensive hidden complexity
- ambiguous instructions that produce incompatible implementations
- missing probes
- overbroad scope

Output only valid JSON:

{
  "overall_verdict": "PROCEED" | "REDESIGN",
  "confidence": 1,
  "fatal_flaws": [
    {
      "title": "",
      "scenario": "",
      "why_current_artifacts_do_not_prevent_it": "",
      "probability": "low|medium|high",
      "impact": "low|medium|high",
      "required_probe_or_change": ""
    }
  ],
  "ambiguities": [
    {
      "artifact": "",
      "ambiguous_reference": "",
      "interpretation_a": "",
      "interpretation_b": "",
      "consequence": "",
      "required_resolution": ""
    }
  ]
}
```

## Trace Requirements

For authoritative accepted changes, write replay traces to `output/current/traces/TRACE-<N>-<short_name>.json`.

Optionally append chronological JSONL events to `output/traces/run_trace.jsonl` for:

- phase start/end
- agent invocation
- change request proposed
- change request accepted/rejected
- artifact integrated
- lint result
- review result
- probe result

Each event must include:

```json
{
  "timestamp": "ISO-8601",
  "phase": "phase name",
  "event_type": "string",
  "artifact_paths": ["paths"],
  "artifact_versions": {
    "artifact_id": 1
  },
  "artifact_hashes": {
    "artifact_id": "sha256:<hash>"
  },
  "change_request": "CR-001-short-name",
  "agent_or_tool": "name",
  "summary": "short factual summary",
  "inputs": ["artifact paths or prompt paths"],
  "outputs": ["artifact paths"],
  "status": "started|completed|failed|blocked"
}
```

## Stop Conditions

Stop and ask the user only when:

- a high-impact constraint is missing and cannot be safely inferred
- the acceptance contract cannot be made verifiable
- the requested scope is incompatible with the stated budget
- a required external tool or domain pack is unavailable

Otherwise proceed with explicit assumptions and record them in `constraints.json`.

## Final Deliverable

The final design is acceptable only if:

- `output/current/INDEX.json` identifies all authoritative artifacts
- `INDEX.json` records current artifact versions, hashes, owners, statuses, and latest review/validation pointers
- shared facts are in `registry/`
- every spec references registry-owned facts instead of redefining them
- latest lint report has no blocking failures
- all current artifact hashes match the files on disk
- all accepted CRs have an integration result and trace id
- every subagent result used by a CR, review, or validation plan has a matching
  dispatch record and trace reference
- no integrated CR has unresolved baseline or write-conflict issues
- semantic review is `APPROVE` or `CONDITIONAL_APPROVE` with no unresolved high-severity blockers
- adversarial review is `PROCEED` or all `REDESIGN` blockers have corresponding change requests
- validation plan contains an early end-to-end probe
- trace contains enough information to replay the artifact/review sequence

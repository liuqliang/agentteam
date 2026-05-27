# AgentTeam Design Docs

This directory keeps the current design authority for AgentTeam. The active
documents are intentionally small in number:

```text
design/
├── README.md
├── architecture.md
├── artifact_workflow_sop.md
├── implementation_workflow_sop.md
└── archive/
```

## Reading Order

1. Read `architecture.md` to understand the framework shape, agent hierarchy,
   governance model, validation layers, and design risks.
2. Read `artifact_workflow_sop.md` when you need the exact operational process:
   output layout, artifact metadata, registry rules, CR integration, lint gates,
   semantic review, adversarial review, trace requirements, and final acceptance.
3. Read `implementation_workflow_sop.md` when an approved semantic design must
   become bounded code changes in a real repository.
4. Read `archive/` only for historical context.

## Authority Map

| Document | Status | Authority |
|---|---|---|
| `architecture.md` | Current | System positioning, layered architecture, agent-team hierarchy, validation philosophy, model allocation, risks, and success metrics. |
| `artifact_workflow_sop.md` | Current | Exact workflow, artifact structure, metadata, `INDEX.json`, semantic subagent dispatch, CR schema, integration rules, lint checks, review prompts, trace records, and final gates. |
| `implementation_workflow_sop.md` | Current | Exact implementation workflow after semantic design: repo grounding, language packs, localization, context packs, task cards, implementation subagent dispatch, workspace sandbox policy, verification, integration, and failure routing. |
| `archive/` | Historical | Early problem framing, original blueprint, and experiment revision notes. These files explain how the current design evolved but are not execution authority. |

## Maintenance Rule

Do not duplicate operational schemas across documents.

- If a detail changes semantic design execution, update
  `artifact_workflow_sop.md`.
- If a detail changes code implementation execution, update
  `implementation_workflow_sop.md`.
- If a detail changes the conceptual structure, responsibility boundaries, or
  design rationale, update `architecture.md`.
- If a detail only explains past decisions, keep it in `archive/` or summarize
  it as rationale in `architecture.md`.

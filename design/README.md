# AgentTeam Design Docs

This directory keeps the current design authority for AgentTeam. The active
documents are intentionally small in number:

```text
design/
├── README.md
├── architecture.md
├── artifact_workflow_sop.md
├── artifact_migration_guide.md
├── autonomous_implementation_loop_sop.md
├── implementation_workflow_sop.md
└── archive/
```

## Reading Order

1. Read `architecture.md` to understand the framework shape, agent hierarchy,
   governance model, validation layers, and design risks.
2. Read `artifact_workflow_sop.md` when you need the exact operational process:
   output layout, artifact metadata, registry rules, CR integration, lint gates,
   semantic review, adversarial review, trace requirements, and final acceptance.
3. Read `artifact_migration_guide.md` when existing or older AgentTeam
   artifacts need to be migrated into the current artifact model.
4. Read `autonomous_implementation_loop_sop.md` when an approved semantic
   design must drive long-running implementation without the user manually
   splitting every task.
5. Read `implementation_workflow_sop.md` when a selected task must become a
   bounded code change in a real repository.
6. Read `archive/` only for historical context.

## Authority Map

| Document | Status | Authority |
|---|---|---|
| `architecture.md` | Current | System positioning, layered architecture, agent-team hierarchy, validation philosophy, model allocation, risks, and success metrics. |
| `artifact_workflow_sop.md` | Current | Exact workflow, artifact structure, metadata, `INDEX.json`, semantic subagent dispatch, CR schema, integration rules, lint checks, review prompts, trace records, and final gates. |
| `artifact_migration_guide.md` | Current | Migration rules for converting older AgentTeam artifacts into the current authority classes, metadata schema, backlog/event model, and archive/supersession flow. |
| `autonomous_implementation_loop_sop.md` | Current | Long-running implementation control after semantic design: optional implementation roadmap, backlog generation, task slicing, compact layout, event log, map freshness, agent role specs, resume behavior, and semantic feedback routing. |
| `implementation_workflow_sop.md` | Current | Bounded task execution after the autonomous loop selects a task: repo grounding, language packs, localization, context packs, task cards, implementation subagent dispatch, workspace sandbox policy, verification, integration, and failure routing. |
| `archive/` | Historical | Early problem framing, original blueprint, and experiment revision notes. These files explain how the current design evolved but are not execution authority. |

## Maintenance Rule

Do not duplicate operational schemas across documents.

- If a detail changes semantic design execution, update
  `artifact_workflow_sop.md`.
- If a detail changes migration from older artifacts to the current model,
  update `artifact_migration_guide.md`.
- If a detail changes code implementation execution, update
  `implementation_workflow_sop.md`.
- If a detail changes long-running implementation control, backlog selection,
  implementation roadmap policy, task slicing, compact layout, map freshness,
  or resume behavior, update `autonomous_implementation_loop_sop.md`.
- If a detail changes the conceptual structure, responsibility boundaries, or
  design rationale, update `architecture.md`.
- If a detail only explains past decisions, keep it in `archive/` or summarize
  it as rationale in `architecture.md`.

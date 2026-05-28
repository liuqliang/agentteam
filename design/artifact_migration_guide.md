# Artifact Migration Guide

Status: current migration guidance for moving older AgentTeam artifacts into the
current artifact model.

This guide is for repositories or runs that already have earlier AgentTeam
artifacts and need to adopt the current split between semantic artifacts,
autonomous implementation control, and bounded implementation execution.

## Migration Principle

Migrate by classification, not by renaming files.

Older artifacts should first be classified by authority class, then mapped into
the current layout. Do not rewrite historical evidence unless it is necessary to
make the current `INDEX.json` valid. Preserve old files in `archive/` or mark
them `archived`/`superseded` with hashes.

## Current Artifact Families

| Current family | Authority class | Main owner |
|---|---|---|
| Semantic design artifacts | `semantic_contract` | Artifact workflow Integration Agent |
| Implementation control artifacts | `implementation_authority` | Implementation Orchestrator |
| Repo navigation artifacts | `derived_observation` | Repo Grounding Agent or Repo Map Manager |
| Evidence artifacts | `evidence_note` | Producing tool, agent, or reviewer |

The key new distinction is that long-running implementation state is not part
of the semantic contract. Backlog, event log, current task, progress, and map
freshness belong to implementation control.

## Old-To-New Mapping

| Older artifact or concept | Current destination | Notes |
|---|---|---|
| Free-form project/design notes | `task_brief`, `constraints`, `acceptance_contract`, or archived rationale | Split promises from rationale. Only promises become `semantic_contract`. |
| System architecture/spec docs | `spec`, `registry`, `validation_plan`, or archived rationale | Registry-owned terms should be centralized instead of repeated. |
| Informal review notes | `review` or `evidence_note` | Findings that change authority must become CRs. |
| Ad hoc change notes | `change_request` | CRs need source artifact ids, baseline hashes, and integration result. |
| Old implementation handoff | `implementation_pack.json` | Keep only source layout, build/test contract, milestones, and task policy. |
| Manual task list | `backlog.json` | Convert items into backlog records with dependencies, status, risk target, and blockers. |
| Chat progress summary | `INDEX.json` progress field or `progress.json` logical record | Treat chat as evidence only if copied into an artifact with provenance. |
| Subagent prompt/result in chat | `agent_dispatch` and `agent_result` | Hidden chat context is not authoritative. |
| Test command notes | `verification_object` plus evidence event | Use `verification_mode=command` or `verification_mode=no_command`. |
| File list or repo map | `repo_index.json` or expanded `repo_index/` | Mark provenance, confidence, and stale conditions. |
| Implementation notes or local ADRs | `implementation_structure_doc` | Must not redefine semantic contract facts. |
| Execution transcript | `events.jsonl` and optional trace artifacts | L0/L1 may use event ids as trace carriers; L2/L3 need explicit traces. |

## Migration Steps

1. Inventory existing artifacts and assign stable artifact ids.
2. Classify each artifact as `semantic_contract`, `implementation_authority`,
   `derived_observation`, or `evidence_note`.
3. Build or repair `_meta` for every current artifact.
4. Move obsolete originals into `design/archive/` or mark them superseded in
   `INDEX.json`.
5. Create the current semantic artifact set:
   `task_brief`, `constraints`, `acceptance_contract`, registry/specs,
   `validation_plan`, reviews, CRs, and traces as applicable.
6. Create or update `implementation_pack.json` only after semantic artifacts are
   current enough for implementation.
7. Create autonomous implementation control artifacts:
   `INDEX.json`, `backlog.json`, `repo_index.json`, `current_task.json`, and
   `events.jsonl`.
8. Convert old task/progress information into backlog records and event entries.
9. Convert old subagent work into dispatch/result records when it is still used
   by the current plan.
10. Run artifact validation and record a migration trace or event.

## Backlog And Event Reconstruction

When migrating old implementation progress, prefer conservative status:

| Old evidence | New backlog status |
|---|---|
| No accepted result | `ready` or `blocked` |
| Worker started but no terminal result | `running`, then immediately route through resume policy |
| Result exists but not verified | `blocked` |
| Verified and integrated | `done` |
| Superseded by design or repo changes | `rebase_required` |
| No longer relevant | `cancelled` |

Every migrated item should get at least one `events.jsonl` entry with
`event_status=applied`, a source pointer, and an idempotency key. If exact
history is unavailable, record a migration event that states which facts are
known and which are reconstructed.

## Validation Checklist

A migration is acceptable only if:

- every current artifact has `_meta`, version, owner, status, and content hash;
- `_meta.status` is lifecycle status only, not execution state;
- semantic-contract changes have CR or integration provenance;
- implementation control artifacts exist if implementation has started;
- backlog status can be reconstructed from `backlog.json` and `events.jsonl`;
- repo index artifacts are marked as derived observations with stale rules;
- L0/L1 implementation evidence has an event id or compact result;
- L2/L3 implementation evidence has explicit trace references;
- old artifacts are either archived, superseded, or intentionally still current.

## Common Mistakes

- Treating old chat summaries as authoritative state.
- Copying old task status into `_meta.status`.
- Moving implementation ADRs into semantic specs without a CR.
- Rebuilding repo maps without recording stale conditions.
- Preserving every old artifact as current instead of archiving or superseding.

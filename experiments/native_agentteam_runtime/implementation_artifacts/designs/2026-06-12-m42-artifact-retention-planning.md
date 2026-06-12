# M42 Artifact Retention Planning Design

## Purpose

M42 adds an explicit artifact retention planning view to `agentteam gc`. The
goal is to answer: which projected artifacts are authoritative, which are
derived and rebuildable, and which rebuildable files might become cleanup
candidates later?

This milestone does not delete run artifacts. It only exposes a bounded,
auditable candidate list.

## Command Shape

`agentteam gc --artifacts` adds an `artifact_retention_plan` section to the
existing GC summary. The plan is available in JSON output and summarized in
text output.

The plan should:

- require a fresh projection DB for exact candidate paths;
- mark `deletion_enabled: false`;
- list only rebuildable artifacts as cleanup candidates;
- explain that authoritative artifacts remain protected;
- limit candidate rows by a configurable `--artifact-limit` value.

## Retention Classes

- `authoritative`: events, state, reports, patches, and frozen taskpacks. These
  are audit records and remain protected.
- `rebuildable`: role context and repo context packages. These are derived from
  task and repository context, so they may become future cleanup candidates.
- `protected`: reserved for active/nonterminal or policy-pinned artifacts.

## Non-Goals

- no deletion of run artifacts;
- no `--force` behavior for artifact candidates;
- no DB-primary artifact storage;
- no retention policy DSL;
- no background cleanup worker.

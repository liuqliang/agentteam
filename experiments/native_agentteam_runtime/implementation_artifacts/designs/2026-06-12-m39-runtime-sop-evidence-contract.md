# M39 Runtime SOP Evidence Contract Design

## Purpose

M39 adapts the single-agent implementation SOP into a native AgentTeam runtime
contract. The goal is not to copy the outer `design/` SOP file layout. The goal
is to preserve its risk, evidence, and authority rules in scheduler-owned
runtime state.

This milestone exists before the artifact projection database because the
runtime must first define which facts are authoritative. The database can then
index and summarize those facts without changing their meaning.

## Decisions

### L3 Is Semantic Architecture Work

`L3` is not an ordinary writable implementation-worker task.

An `L3` finding means the implementation path has encountered semantic
architecture risk: public behavior, protocol, authority artifact, security,
permission, data migration, concurrency, or a blocking design contradiction.

The runtime routes this to a dedicated `semantic_architecture_agent`. That
agent may:

- inspect the taskpack, context, event log, worker result, and relevant design
  artifacts;
- draft a semantic design proposal or authority-update task;
- split the architecture decision into lower-risk implementation work;
- mark the issue unresolved and request user input.

If the semantic architecture agent cannot resolve the question, the runtime
pauses the run through a manual gate and waits for the user. Ordinary
implementation workers do not edit semantic authority artifacts directly.

### L2 Missing Evidence Blocks Integration

A worker may return an `L2` result even when evidence is incomplete. The
runtime may store its result, diff, report, and events. It must not move that
result into verified integration until the required `L2` evidence exists.

This keeps work recoverable without allowing uncertain or under-evidenced
patches to silently merge.

### L0/L1 Stay Lightweight

Normal implementation work should not produce long trace files. For `L0` and
`L1`, the trace carrier is the append-only event log plus the compact worker
result and report summary.

Manual trace writing should be reserved for `L2` or higher-risk cases.

## Runtime Mapping From SOP Concepts

| SOP concept | Native runtime carrier |
|---|---|
| implementation pack | frozen taskpack |
| current task | backlog item plus inflight attempt |
| dispatch file | mailbox dispatch message and dispatch event |
| result file | worker outbox result, patch artifact, and result event |
| progress file | scheduler state plus `agentteam status` summary |
| trace file | `events.jsonl`, worker report, and optional evidence artifact |
| milestone trace | generated report over events, task lineage, reviews, and integration results |
| semantic design CR | semantic architecture proposal plus authority-update task |
| verification object | task verification policy and verification result event |
| roadmap | native runtime roadmap or project roadmap, not worker-owned state |

## Evidence Levels

| Level | Runtime meaning | Minimum runtime evidence |
|---|---|---|
| `L0` | Tiny local edit or documentation correction. | changed files, diff summary, verification status or not-applicable reason, next action. |
| `L1` | Default bounded implementation task. | L0 plus key files read, assumptions, worker result, and event ids. |
| `L2` | Cross-module, internal API, build/config, broad refactor, uncertain result, skipped verification, or review-required work. | L1 plus formal risk summary, context/task/base identifiers, review or orchestrator acceptance, and milestone trace carrier. |
| `L3` | Semantic architecture or authority risk. | Semantic architecture agent finding, proposal or unresolved manual gate, authority-update path, and serial integration decision. |

## Result Contract Additions

Worker and scheduler-visible result summaries should support these fields:

```json
{
  "evidence_level": "L0 | L1 | L2 | L3",
  "evidence_status": "complete | incomplete | escalated | blocked",
  "trace_carrier": [
    {
      "type": "event | report | patch | review | semantic_proposal",
      "path": "optional/path/or/event-id"
    }
  ],
  "missing_evidence": [
    "review_result",
    "verification_result"
  ]
}
```

The fields are summaries, not a new authority layer. Source events, reports,
patches, and semantic proposals remain the evidence carriers.

Dispatch messages include an `evidence_policy` object that names the required
result key and fields. Codex worker prompts must surface this policy so workers
return `output.evidence_summary` instead of leaving the scheduler to infer
missing evidence after the fact.

## Scheduler Policy

Task proposal validation may accept `L0`, `L1`, `L2`, and `L3` as declared risk
targets, but dispatch policy differs by level:

- `L0` and `L1` can be dispatched to ordinary writable workers when scope and
  dependency rules pass.
- `L2` can be dispatched only when its review/evidence policy is known. Missing
  evidence blocks integration.
- `L3` is routed to semantic escalation state and assigned to
  `semantic_architecture_agent`; it is not dispatched to implementation
  workers.

The scheduler should emit inspectable events for:

- `evidence_incomplete`;
- `integration_blocked_by_evidence`;
- `semantic_escalation_required`;
- `semantic_escalation_resolved`;
- `manual_gate_opened`.

## Reporting Policy

`agentteam status` should remain compact. It should show evidence only when it
affects progress, for example:

```text
evidence: 3 complete, 1 integration-blocked, 1 semantic-escalated
```

`agentteam report` should provide the semantic explanation:

- what changed;
- what result was achieved;
- which evidence is complete;
- which evidence is missing;
- whether a semantic architecture decision was made, queued, or blocked.

Sparse notifications, including Feishu notifications, should use the same
high-level report summary rather than raw logs.

## Non-Goals

- no database schema changes in M39;
- no full copy of outer SOP trace files into runtime work directories;
- no direct authority-document edits by implementation workers;
- no live-model requirement in normal unit tests;
- no automatic resolution of architecture questions when the semantic
  architecture agent marks them unresolved.

## Test Strategy

Use fake and shell workers.

Core tests:

- `L3` task proposals produce semantic escalation state instead of ordinary
  worker dispatch;
- unresolved semantic escalation opens a manual gate;
- `L2` results missing required evidence are captured but integration-blocked;
- completed `L0` and `L1` tasks keep compact evidence summaries;
- `status` and `report` summarize evidence state without reading a database;
- existing proposal validation and integration tests remain compatible.

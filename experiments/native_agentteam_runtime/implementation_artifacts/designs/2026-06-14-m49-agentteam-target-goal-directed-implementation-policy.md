# M49 AgentTeam-Target Goal-Directed Implementation Policy

## Purpose

M49 defines how AgentTeam should handle implementation work when the target
repository is the AgentTeam runtime itself.

This is not a special self-improvement command and not an autonomous search for
things to improve. It is ordinary goal-directed implementation: the operator
provides a functional or semantic requirement, the runtime reads and reasons
over the repository, the taskpack author decomposes the work, workers implement
bounded changes, and the operator reviews before source-branch integration.

## Decision

Do not add `agentteam self-improve` in this milestone.

AgentTeam-as-target work should use the same operator commands as any other
repository:

- `agentteam start`
- `agentteam next`
- `agentteam taskpack new`
- `agentteam report`
- `agentteam integrate`, only when the operator chooses to integrate a reviewed
  run

The only special handling is policy: when the target repository is AgentTeam
itself, generated taskpacks and reports must make the no-merge/no-push operator
review gate explicit.

## Requirement Types

The taskpack author should distinguish three input shapes.

### Concrete Code Request

The operator names a specific file, function, bug, failing test, or code-level
change. The taskpack may generate a direct implementation task with narrow
read/write scope and focused verification.

### Functional Or Semantic Requirement

This is the normal case. The operator describes a desired capability or workflow
semantics without naming exact files. The taskpack author must first translate
the requirement into repo-directed implementation work:

- read relevant docs and source files;
- identify affected modules and contracts;
- decide whether the work should be one task or a small batch;
- assign bounded read scopes, write scopes, required deliverables, and
  verification commands;
- preserve the original operator requirement in the taskpack goal and
  `goal_alignment` fields.

Example:

```text
Implement the M49 policy so AgentTeam-as-target tasks are ordinary tasks, but
reports and taskpacks make the no-merge/no-push operator review gate explicit.
```

This should produce implementation tasks, not an open-ended audit.

### Open-Ended Improvement Request

If the operator asks for something broad such as "improve AgentTeam" or
"optimize the framework" without a concrete goal, the taskpack author should
not jump directly to source edits. It should generate an audit or planning task
whose deliverable is a bounded recommendation with evidence, or it should block
for clarification if the scope is too large.

## Workflow

The operator starts AgentTeam-as-target work as a normal task:

```bash
agentteam start --goal "Implement M49 policy hardening for AgentTeam-as-target tasks; do not merge or push"
```

or continues from a completed report:

```bash
agentteam next --from-taskpack <taskpack-id> --goal "Continue implementing the next bounded M49 policy gap"
```

The runtime follows the ordinary lifecycle:

1. taskpack author drafts and freezes a bounded taskpack;
2. worker reads selected repo context and performs the task in its worktree;
3. worker returns `operator_summary`, `evidence_summary`, changed files, and
   patch evidence;
4. scheduler validates scope, deliverables, evidence, and integration baseline;
5. report and Feishu notification summarize the result in operator-facing
   Chinese;
6. operator reviews the report, diff, and tests before deciding whether to
   merge, commit, push, or create a follow-up task.

## Taskpack Constraints

When the target repository is AgentTeam itself, taskpacks should include these
constraints in the objective or acceptance criteria:

- this is an AgentTeam-as-target implementation task;
- source branch merge is operator-gated;
- do not run `git merge`, `git push`, or source-branch mutation commands from
  the worker;
- keep the task small enough for one reviewable patch or a clearly bounded
  batch;
- preserve the operator's functional or semantic requirement in
  `goal_alignment`;
- avoid broad refactors unless the operator explicitly asks for them;
- include `required_deliverables` covering repository understanding, evidence
  paths, implemented changes or no-safe-change rationale, verification summary,
  and recommended next implementation tasks;
- include Chinese zh-CN `operator_summary` fields;
- include structured `evidence_summary.trace_carrier` objects.

## Authority Boundaries

AgentTeam-as-target work does not change artifact authority:

- workers produce patches and evidence; they do not own final source control
  decisions;
- run artifacts remain authoritative for runtime evidence;
- `agentteam.db` remains a rebuildable projection;
- roadmap and design docs remain operator-maintained authority artifacts unless
  the task explicitly targets documentation updates;
- Feishu remains outbound notification only and cannot approve a merge;
- release activation remains a separate operator action after source changes
  are reviewed and pushed.

## Reporting Contract

Reports for AgentTeam-as-target tasks should make the review gate explicit:

- identify the work as an AgentTeam-as-target implementation task;
- restate the operator's functional or semantic requirement;
- summarize what changed in natural Chinese;
- list changed files and patch path;
- list verification commands and results;
- state whether integration verification passed;
- state that source merge, commit, push, and release activation require
  operator review;
- recommend a next action such as reviewing the diff, rerunning tests, merging
  the verified baseline, or creating a follow-up task.

The scheduler should not treat this label as merge approval. It is a reporting
and task-authoring constraint.

## Failure And Pause Conditions

The worker should block rather than guess when:

- the operator requirement is too broad to translate into a bounded taskpack;
- the task requires changing architecture policy or roadmap direction;
- the requested change would require broad source restructuring;
- verification cannot be run or gives conflicting evidence;
- the patch needs credentials, network access, external services, or sandbox
  permissions not already available;
- applying the patch would require merging into the source branch.

The operator can then inspect the report and continue with an explicit follow-up
goal.

## Implementation Scope

M49 should be implemented as taskpack authoring, prompt, and reporting policy
hardening, not as a new CLI command.

Implementation may add:

- taskpack author guidance for AgentTeam-as-target functional and semantic
  requirements;
- policy text that distinguishes concrete code requests, goal-directed
  implementation, and open-ended audits;
- report wording that makes the operator review gate visible;
- tests that prove AgentTeam-as-target taskpacks do not request automatic merge
  or push authority;
- documentation in the roadmap and command reference.

Implementation should not add:

- `agentteam self-improve`;
- autonomous repository scanning without an operator-provided goal;
- automatic merge, commit, push, or release activation;
- long-running autonomous self-maintenance;
- direct worker authority over design or roadmap artifacts;
- Feishu inbound approval controls.

## Acceptance

M49 is complete when:

- the design and roadmap state that AgentTeam-as-target work is ordinary
  goal-directed implementation;
- taskpack authoring can preserve the original functional or semantic
  requirement while generating repo-directed implementation tasks;
- open-ended improvement requests are routed to audit/planning or clarification
  rather than broad source edits;
- generated AgentTeam-as-target taskpacks do not request automatic source merge
  or push authority;
- completion reports or operator summaries clearly tell the operator that
  source integration requires review;
- tests cover the policy without live model calls or real network access;
- existing native runtime unit tests continue to pass.

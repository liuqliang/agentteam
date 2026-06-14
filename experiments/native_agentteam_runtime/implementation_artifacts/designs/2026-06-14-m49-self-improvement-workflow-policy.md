# M49 Self-Improvement Workflow Policy

## Purpose

M49 defines how AgentTeam can use its ordinary runtime workflow to improve
AgentTeam itself without adding a special self-improvement command or giving
workers merge authority.

Self-improvement is a task type and review policy, not a new scheduler mode.
The existing commands remain the operator interface:

- `agentteam start`
- `agentteam next`
- `agentteam taskpack new`
- `agentteam report`
- `agentteam integrate`, when the operator chooses to integrate a reviewed run

## Decision

Do not add `agentteam self-improve` in this milestone.

The operator can submit a normal goal such as "audit the AgentTeam runtime and
fix one concrete issue." The taskpack author and worker prompts must treat this
as an ordinary implementation task with extra review constraints:

- workers may inspect and patch AgentTeam source files inside the declared
  write scope;
- workers may produce patches, reports, evidence summaries, and verification
  results;
- workers must not merge back to the source branch;
- workers must not push to a remote;
- workers must not edit roadmap, design authority, or architecture artifacts
  unless the task explicitly requests documentation synchronization;
- final source-branch merge, commit, and push remain operator decisions.

This keeps the control model consistent: workers produce evidence and patches,
the scheduler validates and integrates into a run baseline, and the operator
decides whether source control changes should land.

## Workflow

The operator starts self-improvement as a normal task:

```bash
agentteam start --goal "Audit AgentTeam runtime and fix one concrete issue; do not merge or push"
```

or continues from a completed report:

```bash
agentteam next --from-taskpack <taskpack-id> --goal "Continue with the next safe AgentTeam runtime improvement"
```

The run then follows the same lifecycle as other implementation tasks:

1. taskpack author drafts and freezes a bounded taskpack;
2. worker reads selected repo context and performs the task in its worktree;
3. worker returns `operator_summary`, `evidence_summary`, changed files, and
   patch evidence;
4. scheduler validates scope, deliverables, evidence, and integration baseline;
5. report and Feishu notification summarize the result in operator-facing
   Chinese;
6. operator reviews the report, diff, and tests before deciding whether to
   merge, commit, or push.

## Taskpack Constraints

When the goal is about AgentTeam itself, the taskpack should include these
constraints in the task objective or acceptance criteria:

- self-improvement task: source branch merge is operator-gated;
- do not run `git merge`, `git push`, or source-branch mutation commands from
  the worker;
- keep the task small enough for one reviewable patch;
- prefer one concrete bugfix, missing validation, documentation correction, or
  focused test gap;
- avoid broad refactors unless the operator explicitly asks for them;
- include `required_deliverables` covering repository understanding, evidence
  paths, implemented changes or no-safe-change rationale, verification summary,
  and recommended next implementation tasks;
- include Chinese zh-CN `operator_summary` fields;
- include structured `evidence_summary.trace_carrier` objects.

## Authority Boundaries

Self-improvement does not change artifact authority:

- source files are changed only through worker patches and integration
  baselines;
- run artifacts remain authoritative for runtime evidence;
- `agentteam.db` remains a rebuildable projection;
- roadmap and design docs remain operator-maintained authority artifacts unless
  the task explicitly targets documentation updates;
- Feishu remains outbound notification only and cannot approve a merge.

## Reporting Contract

Reports for self-improvement tasks should make the review gate explicit:

- identify the task as an AgentTeam self-improvement task;
- summarize what changed in natural Chinese;
- list changed files and patch path;
- list verification commands and results;
- state whether integration verification passed;
- state that source merge, commit, and push require operator review;
- recommend a next action such as reviewing the diff, rerunning tests, or
  creating a follow-up task.

The scheduler should not treat this label as merge approval. It is a reporting
and task-authoring constraint.

## Failure And Pause Conditions

The worker should block rather than guess when:

- the task requires changing architecture policy or roadmap direction;
- the requested change would require broad source restructuring;
- verification cannot be run or gives conflicting evidence;
- the patch needs credentials, network access, external services, or sandbox
  permissions not already available;
- applying the patch would require merging into the source branch.

The operator can then inspect the report and continue with an explicit follow-up
goal.

## Implementation Scope

M49 should be implemented as prompt, authoring, and reporting policy hardening,
not as a new CLI command.

Implementation may add:

- taskpack author guidance for AgentTeam self-improvement goals;
- report wording that makes the operator review gate visible;
- tests that prove self-improvement taskpacks do not request automatic merge or
  push authority;
- documentation in the roadmap and command reference.

Implementation should not add:

- `agentteam self-improve`;
- automatic merge, commit, push, or release activation;
- long-running autonomous self-maintenance;
- direct worker authority over design or roadmap artifacts;
- Feishu inbound approval controls.

## Acceptance

M49 is complete when:

- the design and roadmap state that self-improvement is a normal task workflow;
- taskpack authoring can preserve the no-merge/no-push operator gate for
  AgentTeam self-improvement goals;
- completion reports or operator summaries clearly tell the operator that
  source integration requires review;
- tests cover the policy without live model calls or real network access;
- existing native runtime unit tests continue to pass.

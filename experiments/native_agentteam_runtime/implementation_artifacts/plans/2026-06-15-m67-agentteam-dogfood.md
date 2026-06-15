# M67 AgentTeam Dogfood Post-M66 Route Note

Status: implementation-stage route note for taskpack authors.

This note turns the post-M66 roadmap evidence into pursue-ready implementation
tasks. It is intentionally documentation-facing: the current dispatch asks for
roadmap usability, and `semantic_artifacts/current/` remains semantic authority
owned by the registry and architecture roles.

## repository_understanding_summary

The native runtime has moved beyond a file-format prototype into a local,
file-authoritative multi-process runtime with Codex-backed worker execution,
bounded repo context, review-gated integration, Chinese operator summaries, a
read-only follow-up queue, and bounded `pursue` rounds. M62 made the follow-up
queue inspectable with `agentteam queue show` and `agentteam queue next`; M61
and M45 made completion reports and Feishu messages useful to Chinese-speaking
operators; M66 made `agentteam pursue` consume the same `follow_up_queue.v1`
summary between rounds.

The useful gap after M66 is no longer basic queue plumbing. The roadmap now
needs taskpack-author guidance that turns "continue the next thing" into
specific, measurable, review-gated goals that can survive multiple pursue
rounds without broadening into unsafe orchestration, DB-primary storage, M68
model adapters, or semantic authority edits.

## baseline_or_current_behavior

Current behavior from the roadmap and command reference:

- `agentteam queue show` and `agentteam queue next` are read-only and build
  suggestions from structured report `next_steps`, `follow_up_recommendation`,
  and long-goal `goal_memory.follow_up_queue`.
- `agentteam pursue` records the compact queue selection in each round/recap
  and uses the selected `next_goal` as the raw goal for the next follow-up
  taskpack.
- `agentteam report` and Feishu `run_completed` notifications render Chinese
  operator summaries from structured fields, including next actions when the
  report supports them.
- AgentTeam-as-target work must preserve an operator review gate. Workers may
  produce patches, reports, evidence, and integration baselines, but merge,
  push, and release activation stay with the operator.
- The semantic artifact registry marks current semantic artifacts as
  `semantic_contract` authority with restricted writers, so ordinary
  implementation workers should not edit those files.

## optimization_candidate_matrix

| Candidate | Evidence | Why it matters now | Safe shape | Recommendation |
| --- | --- | --- | --- | --- |
| Next-goal specificity gate | M66 says pursue uses selected `next_goal`, and warns that generic next steps should be hardened before broader orchestration. | A vague `next_goal` makes every later pursue round less bounded. | Validate or score report/queue goals for concrete file scope, success criteria, verification command, and review gate. | Do first. |
| Chinese next-step rationale | M45/M61 provide Chinese summaries, but M66 continuation depends on understanding why a queue item was selected. | Operators need to decide whether a recommended continuation is worth starting without reading long logs. | Add bounded rationale fields derived from existing structured evidence; do not make localized prose authority. | Do second. |
| Queue provenance and readiness | M62/M66 share one queue summary, but taskpack authors need source and readiness context to avoid re-litigating selection. | The next taskpack should inherit the evidence path, not just a goal string. | Expose selected item source fields, readiness status, blockers, and suggested verification in text/JSON output. | Do third. |
| Taskpack-author route template | The roadmap contains many implemented milestones, but taskpack authors need a reusable task shape. | Future dogfood work should be easier to author consistently from roadmap evidence. | Add prompt/template guidance and tests that require evidence paths, non-goals, deliverables, and review gate text. | Do fourth. |
| Live dogfood calibration pack | M66 asks for more live target-repository loops to judge specificity. | Runtime behavior should be tested against real reports after the quality gates exist. | Run bounded AgentTeam-as-target or sample-repo loops without auto-merge, push, release, or semantic authority edits. | Do after the first three quality tasks. |

## post_m66_route_table

| Order | Pursue-ready task | Objective | Read/write scope hint | Verification hint | Stop condition |
| --- | --- | --- | --- | --- | --- |
| 1 | Harden `next_goal` specificity for reports and queue summaries. | Reject or downgrade follow-up goals that lack concrete scope, measurable result, and verification plan before `pursue` consumes them. | Read `agentteam.py`, report helpers, queue helper, command reference; write focused runtime/tests/docs only. | Focused unit tests for generic versus specific `next_goal`, plus `git diff --check`. | Stop at review gate if changing report semantics affects existing output contracts. |
| 2 | Add Chinese "why this next step" rationale to report and Feishu summaries. | Make the operator-facing Chinese brief explain the evidence-backed reason for the next action without treating Chinese prose as authority. | Read M45/M61 report aggregation code and notification rendering; write report/notification tests and docs. | Tests for `agentteam report`, concise completion text, and Feishu dry-run content. | Stop if the rationale cannot be derived from structured fields. |
| 3 | Expose queue selection provenance and readiness in `agentteam queue next`. | Show source report fields, selected item provenance, readiness status, blockers, and suggested verification command before a continuation starts. | Read queue helper and command reference; write CLI output/JSON tests and docs. | Tests for text and JSON output using report-only, goal-memory-only, and merged queues. | Stop if output would mutate run artifacts or start work. |
| 4 | Add taskpack-author route-template guidance for roadmap-derived follow-ups. | Give taskpack authors a reusable checklist for turning roadmap evidence into bounded tasks with evidence paths, non-goals, deliverables, metrics, and review gate. | Read taskpack author prompt/profile files and implementation plans; write prompt/docs/tests only. | Validation tests that generated implementation taskpacks include evidence paths and non-goal constraints. | Stop if the change would edit semantic authority documents directly. |
| 5 | Run a bounded dogfood calibration pack after tasks 1-3. | Measure whether the strengthened queue/report outputs produce concrete follow-up taskpacks on AgentTeam-as-target or sample repositories. | Read generated reports/queues; write only calibration reports or implementation notes unless a separately scoped safe fix is found. | Compare queue specificity before/after with counts of concrete goals, verification commands, and review-gate mentions. | Stop at any merge, push, release, permission, manual, or semantic authority gate. |

## evidence_paths

- `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
  - M61 records multi-task Chinese operator report aggregation.
  - M62 records read-only follow-up queue inspection.
  - M64 records semantic feedback proposals as the review-gated path for
    semantic authority updates.
  - M65 records read-only repository grounding and explicitly postpones M68
    multi-model adapters.
  - M66 records pursue queue consumption and the remaining risk around generic
    next steps.
- `docs/agentteam-command-reference.md`
  - `agentteam queue` documents read-only queue inspection.
  - `agentteam pursue` documents queue consumption between rounds and max-round
    continuation through `agentteam queue next`.
  - `agentteam report` documents Chinese operator summaries and review-gate
    fields.
  - AgentTeam-as-target work documents that merge, push, and release activation
    remain operator decisions.
- `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-14-m66-pursue-queue-consumption.md`
  - The completed M66 plan shows the implemented queue-selection contract,
    pursue loop integration, operator visibility, and verification commands.
- `experiments/native_agentteam_runtime/semantic_artifacts/current/registry/artifacts.json`
  - The registry marks current semantic artifacts as `semantic_contract`
    authority with restricted writers and design change requirements.

## implemented_changes_or_no_safe_change_rationale

Implemented change in this task: added this implementation-stage route note and
linked it from the roadmap. No runtime code change is made here.

No-safe-code-change rationale: the dispatch objective is documentation-facing,
the write scope is limited to roadmap/plan/command-reference files, and the
highest-confidence improvement is to make roadmap evidence directly usable by
future taskpack authors. The post-M66 code candidates above touch report
contracts, queue selection, CLI output, and notification rendering; those
changes are safe only as separately scoped taskpacks with focused tests.

This task also does not edit `semantic_artifacts/current/` authority documents.
If implementation evidence later shows a semantic contract gap, use the
existing semantic feedback proposal route instead of direct mutation.

## metric_delta_or_no_safe_change_evidence

- Runtime code files changed: 0.
- Semantic authority files changed: 0.
- Implementation artifact files changed by this task: 2 files, this note and
  the roadmap pointer.
- Pursue-ready tasks mapped from roadmap evidence: 5.
- Operator review gates preserved: merge, push, release activation, permission
  requests, manual gates, and semantic authority updates remain out of worker
  authority.

Because this is a documentation-only route note, there is no runtime metric
delta. The measurable result is a concrete post-M66 task map with evidence
paths, non-goals, verification hints, and stop conditions for later taskpack
authors.

## verification_summary

Planned verification for this task:

- run `git diff --check`;
- inspect the final diff for write-scope compliance;
- confirm the route note includes all required deliverable sections;
- confirm no files under `experiments/native_agentteam_runtime/semantic_artifacts/current/`
  changed.

Unit tests are not required for this documentation-only change. The route table
above lists the focused tests that should accompany later runtime changes.

## recommended_next_implementation_tasks

1. Harden `next_goal` specificity for reports and queue summaries.
2. Add Chinese "why this next step" rationale to report and Feishu summaries.
3. Expose queue selection provenance and readiness in `agentteam queue next`.
4. Add taskpack-author route-template guidance for roadmap-derived follow-ups.
5. Run a bounded dogfood calibration pack after the quality tasks land.

Do not start M68 multi-model adapters, DB-primary storage, or direct semantic
authority edits as part of this route.

## agentteam_target_review_gate

AgentTeam is the target repository for this dogfood route. Any future worker may
prepare patches, reports, evidence, and integration baselines, but the operator
must review before source merge, push, release activation, or semantic authority
activation. `agentteam pursue` and `agentteam queue` may suggest continuation
commands, but those suggestions are not merge or release authority.

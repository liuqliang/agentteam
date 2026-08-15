# Phase 3 controller verification handoff

Status: implemented and provider-free verified

## Problem

The bounded Requests retest exposed two independent completion hazards:

1. a direct worker produced a non-empty in-scope patch and structured
   `verification_additions`, but reported `failed` solely because its final
   tool slots were exhausted before post-patch tests; the scheduler rejected
   the result before its existing controller-owned verification could run;
2. a full worker added a repository test beside a valid production change;
   the complete candidate then conflicted with the evaluator-only test patch,
   so no quality score was available even though common acceptance passed.

The existing cost boundary remains useful. This task changes completion and
candidate publication semantics rather than removing the boundary or simply
raising its numeric limit.

## Frozen design

### Verification-deferred handoff

A worker may return `result_status=failed` with
`output.verification_deferred=true` only when implementation is complete but
worker-side verification could not finish. The scheduler may recover that
result as a provisional candidate only when all of these conditions hold:

- the worktree contains a non-empty, scope-valid, declaration-matched patch;
- the runtime output contains a valid non-empty `verification_additions` list;
- automatic patch integration is enabled;
- a controller-owned primary integration verification command is configured;
- required semantic deliverables are complete;
- no permission, manual, timeout, or environment failure is being hidden.

The original worker status remains retained. Recovery is explicitly recorded
as `controller_verification_recovery`; it does not turn missing evidence into
worker evidence. The controller applies the provisional patch to the isolated
integration worktree and runs the frozen primary command followed by worker
verification additions. Only a fully passing result can update the integration
baseline. Any failure uses the existing rollback and retry/review policy.

L2/L3 evidence gates remain unchanged. Verification deferral cannot bypass
their complete-evidence requirement.

### Candidate test separation

New Phase 3 protocols freeze
`production_with_supplemental_tests.v1` before provider launch. Candidate
publication emits three immutable artifacts from the same staged diff:

- `candidate.patch`: complete worker output and audit authority;
- `scored-candidate.patch`: non-test paths submitted to official scoring;
- `supplemental-tests.patch`: candidate-authored test paths retained for local
  review and common verification but not submitted to the evaluator.

Classification uses only frozen public path rules. It must not inspect the
evaluator test patch, gold patch, score, or conflict result. The fixed rule
classifies conventional test directory components and conventional test file
names/suffixes. Every mode uses the same rule and complete patch publication.

The candidate reference records all three digests, byte counts, and sorted path
sets. Old protocols retain `complete_candidate_patch` behavior. New reports
must identify the scored-patch policy; a score under this policy is an
AgentTeam Phase 3 result and must not be represented as an unmodified stock
SWE-EVO submission when supplemental test changes were excluded.

## Implementation tasks

1. Extend the worker prompt and tool-budget warnings with the exact
   verification-deferred contract.
2. Add guarded scheduler recovery and explicit events/results without changing
   ordinary failed-result behavior.
3. Add deterministic patch path classification and three-artifact publication.
4. Select the scored artifact from the frozen protocol during compatibility
   checks and official evaluation.
5. Preserve legacy protocol and candidate-reference behavior.
6. Add focused unit and integration tests, then run the complete provider-free
   suite and artifact lint.

## Acceptance

- An ordinary failed worker remains rejected.
- A failed worker with only a flag but no patch, no additions, no primary
  controller check, invalid scope, or missing deliverables remains rejected.
- A qualifying deferred result is integrated only after primary and additional
  verification pass; a failed check rolls the baseline back.
- L2/L3 incomplete evidence still blocks integration.
- A mixed production/test diff publishes reproducible complete, scored, and
  supplemental artifacts with disjoint path sets whose union is complete.
- Classification is independent of evaluator-only inputs.
- Legacy complete-candidate protocols still score `candidate.patch`.
- New protocols score `scored-candidate.patch` and retain the complete patch.
- Focused and complete provider-free tests pass with zero model calls.

## Non-goals

- Do not raise the 12/16 tool limits in this task.
- Do not rerun Requests or select a new SWE-EVO instance.
- Do not rewrite retained result bundles or historical decisions.
- Do not allow a controller to invent, edit, or repair a candidate patch.
- Do not use post-hoc evaluator conflicts to choose which hunks are scored.

## Verification result

Implemented without provider calls. Focused scheduler, candidate publication,
protocol bridge, terminal projection, and legacy compatibility tests pass. The
complete provider-free suite passed `1215` tests with `7` environment skips in
`181.535` seconds. Artifact lint passed over `203` JSON files and `1` JSONL
file, and the decision record validates against the packaged schema.

# Phase 3 Post-Calibration Runtime Repairs

Status: completed

## Decision

Repair the deterministic runtime-contract defects exposed by the DVC
post-repair calibration before another provider-backed benchmark. Preserve the
completed calibration and its retained worker patches as immutable evidence.

## Scope

1. Keep a terminal-only invocation in flight long enough for the worker to
   publish its richer mailbox result. A mailbox result that arrives during the
   bounded publication window remains authoritative for semantic output; the
   terminal remains authoritative for usage.
2. Treat `expected_output_artifacts` as framework-generated,
   repository-relative file paths without whitespace. Natural-language
   acceptance clauses belong in `required_deliverables` or evidence policy and
   must fail taskpack validation. Existing input and source paths are not
   restricted by this generated-artifact naming rule.
3. Strengthen semantic authoring instructions so cross-layer API changes name
   every required implementation layer in read and write scopes. Do not grant
   mechanically inferred source write permissions.

## Non-Goals

- no new live model call;
- no mutation or rescoring of completed DVC candidates;
- no automatic expansion of a frozen taskpack's write scope;
- no scored-pilot promotion;
- no attempt to implement mid-turn Codex token interruption.

## Acceptance

1. A provider terminal observed five seconds before its outbox result does not
   trigger terminal-only reconciliation; the later Chinese operator summary is
   retained.
2. Terminal-only recovery still occurs after the full bounded grace interval.
3. Taskpack validation rejects descriptive prose in
   `expected_output_artifacts` and continues accepting ordinary relative paths.
4. The author prompt explicitly separates artifact paths from semantic
   deliverables and requires complete cross-layer scopes.
5. Focused experiment, taskpack, and scheduler suites pass provider-free.

## Outcome

The worker outbox publication grace is now 30 seconds. A regression test
replays the observed five-second terminal-to-outbox delay and proves that the
Chinese semantic result remains authoritative, while the existing terminal-only
test proves recovery still occurs when the complete grace expires.

Taskpack validation now rejects whitespace-bearing generated artifact entries,
and the author prompt separates artifact paths from semantic deliverables while
requiring cross-layer scope coverage. This rejected all three descriptive DVC
entries without changing input/source-path handling or the established
`.agentteam/generated/` artifact authority.

Provider-free verification passed: 205 experiment-harness tests with 5 skips,
401 taskpack tests with 2 skips, 294 runtime tests, and 38 Phase 3 pilot-runner
tests. Python compilation and `git diff --check` also passed.

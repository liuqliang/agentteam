# Phase 3 controller verification handoff

## Status

The provider-free repair is complete. It preserves the shared `12/16` tool
boundary and does not authorize a new model call or scored SWE-EVO instance.

## Implemented behavior

A worker that has completed an in-scope patch but exhausts its tool budget
before verification can now publish an explicit `verification_deferred`
handoff. The scheduler retains the original failed worker status and only
recovers the candidate when the patch, declared files, semantic deliverables,
primary controller verification, and non-empty worker verification additions
all satisfy the frozen contract. The isolated integration controller then runs
both verification layers. Existing rollback, evidence, and retry gates decide
the result; ordinary worker failures remain rejected.

New Phase 3 protocols now publish three immutable patch artifacts:

- `candidate.patch` retains the complete worker output for audit;
- `scored-candidate.patch` contains production paths for official scoring;
- `supplemental-tests.patch` retains candidate-authored conventional test
  paths without composing them with evaluator-only tests.

The classifier is a public path rule frozen before execution. It does not read
gold material, evaluator patches, conflicts, or scores. Legacy protocols still
submit their complete `candidate.patch`. New terminal results record the exact
candidate policy and scored artifact path, so an AgentTeam-policy result cannot
be mistaken for an unmodified stock benchmark submission.

## Verification

- Scheduler recovery: explicit valid handoff passes; unverifiable handoff is
  rejected; original failed status remains visible.
- Candidate publication: complete, production, and supplemental patches are
  immutable and disjoint by path; the original repository index is unchanged.
- Protocol bridge: single, direct, and full modes receive the same public
  candidate-test policy; old and new schema shapes remain valid.
- Phase 3 runner module: `38` tests passed.
- Complete provider-free suite: `1215` tests passed, `7` skipped, in
  `181.535` seconds.
- Artifact lint: `202` JSON and `1` JSONL files checked with no errors.
- Provider calls: `0`.

## Conclusion

The two Requests failure mechanisms now have deterministic runtime handling.
This closes the provider-free repair, not Phase 3 calibration. A later,
separately authorized run must freeze a runtime release and protocol from this
implementation before measuring cost or quality on another benchmark run.

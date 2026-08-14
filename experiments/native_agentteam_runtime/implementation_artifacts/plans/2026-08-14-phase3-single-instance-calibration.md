# Phase 3 Single-Instance Calibration

## Decision

Run `psf__requests_v2.12.2_v2.12.3` exactly once in each Phase 3 mode to
measure real token and wall-time cost before authorizing another formal pilot.
This is a calibration run, not a statistically sufficient benchmark result.

## Frozen Boundary

- modes: `single_codex`, `agentteam_direct`, `agentteam_full`
- repetitions: one per mode
- execution order: the first preregistered counterbalanced order
- continuation: forbidden after the three terminal mode executions
- model: the same approved Codex model and reasoning profile for all modes
- worker-visible verification: the digest-bound public Requests environment
- final score: the frozen SWE-EVO/SWE-bench evaluator only
- resources: the approved Phase 3 resource envelope

The formal pilot keeps its two initial repetitions and optional third
repetition. The calibration runner is separate so this smaller measurement
cannot silently weaken the formal research contract.

## Acceptance

1. Provider-free freeze proves the source, public environment, evaluator,
   runtime release, model, budget, and one-repetition schedule are immutable.
2. The unmodified base fails the public assertion and the independently
   derived candidate fix passes it.
3. Exactly three mode entries reach a terminal result, with 100% provider
   usage coverage or a fail-closed stop.
4. The report compares total, input, cached input, uncached work, output,
   reasoning, provider wall time, end-to-end wall time, and official score.
5. No result from this calibration is promoted as a general quality claim.

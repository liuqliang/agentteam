# Phase 3 DVC Post-Repair Calibration

Status: completed without scored-pilot promotion

## Decision

Repeat the DVC medium-instance calibration under runtime commit `21cb46e` to
measure the runtime repairs identified by the completed v7 calibration. This is
a new treatment and a new immutable bundle. The v7 run and its report remain
unchanged historical evidence.

This run tests runtime mechanism changes, not a general AgentTeam quality claim
and not a scored-pilot promotion.

## Frozen Comparison

- instance: `iterative__dvc_2.19.0_2.20.0`;
- source commit: `78dd045d29f274960bcaf48fd2d055366abaf2c1`;
- source tree: `ddbe1d3231306250970a0b6ff9384838643d31d3`;
- runtime implementation commit: `21cb46e`;
- model: `gpt-5.6-sol`, reasoning profile `high`;
- modes: `single_codex`, `agentteam_direct`, `agentteam_full`, once each and
  serially in that order;
- per-mode ceiling: `600000` provider-reported total tokens and `1800` seconds;
- aggregate ceiling for this calibration: `1800000` tokens and `5400` seconds;
- official evaluator and public dependency environment: reuse the digest-bound
  v7 DVC authorities without exposing benchmark gold to workers.

All three modes rerun because role/risk tool routing changes the direct and full
treatments, while a fresh single-mode result provides a same-release control.

## Treatment Changes

Compared with v7:

1. tool-call soft and hard limits are selected deterministically by worker role
   and benchmark risk instead of using the universal `12/16` route;
2. full mode seeds a deterministic, digest-checked repository handoff and does
   not launch a model repo-map worker when `semantic_gaps` is empty;
3. full mode binds a 60% taskpack-authoring limit and preserves 40% of its mode
   budget for implementation and later stages;
4. sealed results report actual token use by stage and bounded authoring
   overshoot.

The Codex CLI still has no exact per-turn token interrupt. One already admitted
authoring turn may exceed the 60% stage boundary; that overshoot must be
reported, and no later authoring invocation may be admitted.

## Stop Conditions

Stop before another provider launch on authority drift, incomplete usage,
resource cleanup failure, cross-mode visibility, global or per-mode budget
exhaustion, or a semantic decision requiring operator input. Do not repair a
candidate or alter treatment between modes.

Infrastructure failure may be diagnosed provider-free. It does not authorize
an extra model attempt under the same bundle.

## Acceptance

1. A fresh runtime release and bundle bind the exact implementation commit and
   pass provider-free replay with zero model calls.
2. Every launched invocation has complete terminal usage and `usage_stage`.
3. Direct mode records its selected L2 implementation tool route and either
   produces a candidate or retains a concrete terminal failure.
4. Full mode has no `repo_map` provider invocation when deterministic grounding
   has no semantic gaps, and an implementation worker is launched unless the
   admitted taskpack-author turn exhausts the global mode budget.
5. The full-mode sealed result contains stage totals, reserve status, and any
   bounded authoring overshoot consistent with invocation authority.
6. Candidate patches receive the same official SWE-EVO evaluation used by v7;
   the non-discriminating repository-wide public command is reported separately.
7. The final report compares v7 and post-repair quality, total and uncached
   tokens, provider and controller time, tool activity, stage allocation, and
   changed production paths without statistical generalization.

## Promotion Rule

This single calibration cannot authorize a scored pilot by itself. A promotion
decision requires an explicit post-run review and a separate decision artifact.

## Outcome

The serial run completed all three modes with complete usage accounting. The
dynamic L2 tool route, deterministic repository grounding, and full-mode stage
reservation all operated as designed. Full mode launched its implementation
worker after authoring used 233300 tokens, and no model repo-map invocation was
launched.

The calibration is not promoted. Aggregate usage reached 2788487 tokens because
an admitted Codex turn cannot be interrupted at the frozen token boundary.
Direct mode generated a retained worker patch but terminal reconciliation
discarded the mailbox semantic result before validation. Full mode generated an
incomplete retained patch after its taskpack omitted required `dvc/stage/*`
paths from the frozen write scope. The authoritative result, report, and
non-promotion decision are retained in
`acceptance/phase3-dvc-post-repair-calibration-v1/`.

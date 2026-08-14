# Phase 3 Single-Instance Calibration

## Status

The replacement calibration completed one valid, sealed execution of
`single_codex`, `agentteam_direct`, and `agentteam_full` on the same SWE-EVO
instance. All three modes had complete provider usage, resource, patch, and
official-evaluator evidence.

The result does not support expanding the pilot yet. All modes had the same
official quality result, while both AgentTeam modes exceeded the preregistered
maximum token-cost ratio of `2.0` relative to `single_codex`.

## Frozen Identity

- Instance: `psf__requests_v2.12.2_v2.12.3`
- Source commit: `ca15d4808734c86801c8f3d80c9152c35a163dc3`
- Source tree: `ed5a1585a8be3af46f525730afb5db31b3825812`
- Runtime commit: `f2165fe587c5afe1e0dafa530f7841a860cb177e`
- Bundle SHA-256:
  `ee9b336411f15a2b1cfe4a525542895f3b6e3d7f730f07477da90dee1a65edb4`
- Model and reasoning profile: `gpt-5.6-sol`, `high`
- Execution root:
  `/tmp/agentteam-phase3-calibration-run-requests-3738-v6`
- Completion time: `2026-08-14T05:21:59Z`

The state field `third_repetition_planned=true` is inherited pilot
bookkeeping. The calibration runner deliberately froze one repetition per
mode, scheduled no third repetition, and ended in `completed` state.

## Valid Comparison

Provider-reported total tokens equal input plus output. Reasoning tokens are a
subset of output and are not added again. Total wall time includes provider,
controller/common-acceptance, and official-evaluator time.

| Mode | Input | Cached | Uncached | Output | Reasoning | Total | Model wall | Control wall | Evaluator wall | Total wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_codex` | 314,086 | 258,304 | 55,782 | 3,993 | 1,238 | 318,079 | 104 s | 7.662 s | 21.562 s | 133.224 s |
| `agentteam_direct` | 1,085,665 | 999,424 | 86,241 | 7,428 | 2,046 | 1,093,093 | 218 s | 170.669 s | 21.984 s | 410.653 s |
| `agentteam_full` | 698,501 | 613,632 | 84,869 | 12,589 | 3,959 | 711,090 | 288 s | 473.580 s | 21.704 s | 783.284 s |
| valid comparison total | 2,098,252 | 1,871,360 | 226,892 | 24,010 | 7,243 | 2,122,262 | 610 s | 651.911 s | 65.251 s | 1,327.161 s |

`agentteam_full` comprises a taskpack-author invocation of 110,929 tokens and
an implementation-worker invocation of 600,161 tokens. It used 34.95% fewer
tokens than `agentteam_direct`, but took 90.74% longer because controller and
orchestration time dominated the difference.

Relative to `single_codex`:

| Mode | Token ratio | Wall-time ratio | Preregistered token gate |
| --- | ---: | ---: | --- |
| `agentteam_direct` | 3.4365x | 3.0824x | fail |
| `agentteam_full` | 2.2356x | 5.8795x | fail |

## Quality

All three modes produced the same production-code condition in
`requests/models.py` and received the same official result:

- FAIL_TO_PASS: `0/4`
- PASS_TO_PASS: `104/109`
- partial score: `0.290826`
- outcome: unresolved

Therefore this instance provides no evidence that orchestration improved
quality. One instance and one repetition are also insufficient for a general
quality or efficiency claim, even if a mode had won this comparison.

## Patch-Compatibility Audit

An older direct-mode attempt reported FAIL_TO_PASS `2/4` and PASS_TO_PASS
`106/109`. That candidate modified the same hidden-test insertion region, and
the official test patch fails to apply at `tests/test_requests.py:2177` after
the candidate patch. The old score is therefore excluded as contaminated
benchmark evidence.

A deterministic clean-check was performed by applying each candidate to the
frozen source commit and then running `git apply --check` for the evaluator-only
test patch. The three retained candidates all pass; the excluded old direct
candidate fails. This confirms that the retained quality tie is not caused by
the same patch collision.

The accepted protocol keeps the complete candidate patch. Before official
scoring, the evaluator applies that patch to the frozen source commit and
requires the evaluator-only test patch to pass `git apply --check`. An invalid
candidate becomes `candidate_patch_invalid`; a composition conflict becomes
`evaluator_patch_conflict`. Both outcomes retain provider cost but receive no
quality score and are not classified as infrastructure failures.

New Phase 3 protocols bind this policy as
`evaluator.candidate_patch_policy`. The compatibility receipt records source,
candidate, and test-patch digests plus bounded conflict location diagnostics;
it never includes hidden-test content. Historical artifacts are not rewritten.
The retained three-mode result remains valid because its candidates passed the
same deterministic compatibility audit before this policy was implemented.
The implementation passes all 1,196 provider-free tests with 7 skips, and a
real-artifact replay classifies the excluded v5 direct patch as
`evaluator_patch_conflict` while accepting all three retained v7 patches.

## Infrastructure Closure

The replacement runtime closes the infrastructure failures found in earlier
attempts:

- the full-mode author and implementation worker are both bound to the frozen
  resource envelope;
- the experiment-owned 1,800-second provider timeout overrides a taskpack's
  lower generated timeout;
- all provider invocations have terminal usage and `resource.json` evidence;
- no worker restarted and no empty invocation directory was retained;
- all three mode results and evaluator results are sealed.

The repair sequence is represented by runtime commits `5c27597`, `922fa53`,
`51d0ca5`, and `f2165fe`. Before execution, the complete provider-free suite
passed 1,187 tests with 7 skips. Focused resource/timeout tests, 438 taskpack
and scheduler tests with 2 skips, and 222 experiment tests with 5 skips also
passed.

## Calibration Development Cost

Only the 2,122,262-token replacement run belongs in the mode comparison.
Earlier calls remain useful for infrastructure accounting, but not for model
quality comparison.

| Iteration | Known tokens | Classification |
| --- | ---: | --- |
| v1 | 324,023 | infrastructure waste |
| v2 | 2,742,755 | partial run plus infrastructure waste |
| v3 | 267,016 | diagnostic scored execution |
| v4 | 162,555 | diagnostic scored execution |
| v5 | 1,852,284 | incomplete comparison; excludes one timed-out repo-map call whose usage is unavailable |
| v7 retained run | 2,122,262 | valid three-mode comparison |
| known total | 7,470,895 | plus the unavailable v5 repo-map usage |

The large gap between comparison cost and total calibration cost is itself an
engineering result: recovery, resource binding, timeout propagation, and
usage attribution must be proven provider-free before spending on scored
runs.

## Input-Cost Profile And Containment

The retained transcripts show that provider input cost was amplified by model
context replay. Failed test commands returned between 243,125 and 331,745
characters to the model, and those results remained in later requests. The
direct worker also made six web searches despite having a frozen repository
and sufficient public task authority. This evidence does not support SQLite or
decision-trace writes as the primary cost source.

Commit `d2f22075c8858faddc4223438c35b2a9c9b886bc` adds a scored-protocol model
context policy: every single-mode call, direct worker, full-mode author, and
full-mode worker receives Codex's `tool_output_token_limit=4000` and
`web_search="disabled"` settings. Command registration rejects absent,
duplicated, or mismatched settings before provider launch. Provider access,
controller verification, complete candidate patches, and official evaluation
remain unchanged.

The implementation passes 1,198 provider-free tests with 7 skips. Release
`phase3-calibration-d2f2207` and bundle
`a54007b0620f8e0db8930937c2f951426cc6359ee394213588c1e297c4c54a79`
were materialized, and a zero-execution initialization validated the bundle
and completed resource cleanup without creating provider evidence. These are
mechanism results only; no post-change token or quality result exists yet.

## Disposition

- Single-instance calibration mechanism: `completed`
- Usage coverage: `3/3`, `100%`
- Resource-evidence coverage: `3/3`, `100%`
- Quality conclusion: no mode advantage on this instance
- Cost conclusion: both AgentTeam modes fail the `2.0x` token gate
- Pilot expansion: `not_authorized`

A replacement bundle is now frozen against a runtime release that contains
both the accepted candidate-patch policy and the input-cost containment
policy. It has only completed provider-free initialization. Before a scored
call, record a new execution decision that either selects a new instance or
classifies a bounded Requests repetition as a cost-containment experiment with
an explicit token ceiling. Do not infer general performance from this single
calibration.

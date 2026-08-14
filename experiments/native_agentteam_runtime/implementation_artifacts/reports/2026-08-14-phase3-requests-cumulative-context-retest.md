# Phase 3 Requests cumulative-context retest

## Status

The bounded historical-control retest is complete. All three modes ran once,
provider usage coverage is complete, aggregate usage stayed below the frozen
3,000,000-token ceiling, and resource cleanup completed. The run supports the
cost-containment mechanism but does not support Phase 3 expansion.

## Bound execution

- Instance: `psf__requests_v2.12.2_v2.12.3`
- Runtime release: `phase3-calibration-82cf953`
- Runtime commit: `82cf95332c9d612749aca2ec8d7dcb54424f38aa`
- Authorized bundle SHA-256:
  `66dd5f04ac05a25907575efa2f3bebbaa26286590dc15a070128e478959ffc19`
- Run root:
  `/tmp/agentteam-phase3-requests-cumulative-context-retest-v1-run-v2`
- Pilot state SHA-256:
  `8e0cbb132667a816be5c10a19bfeec696a7f2ba051c1e1aabed9939594100ac0`
- Mode order: `single_codex`, `agentteam_direct`, `agentteam_full`
- Per-mode ceiling: `1,000,000` tokens
- Aggregate ceiling: `3,000,000` tokens
- Model: `gpt-5.6-sol`, high reasoning
- Context policy: output `4,000`, compaction `32,768`, soft tool warning
  `12`, last admitted tool `16`, web search disabled

The first launch attempt used runtime commit `b83c980` and failed before any
provider call because file-path execution of `model_invocation.py` could not
resolve a new relative import. Commit `82cf953` repaired that entry path and
added a subprocess regression test. The discarded run and bundle recorded
zero provider calls; the complete provider-free suite then passed with `1,210`
tests and `7` skips before this execution bundle was authorized.

## Results

| Mode | Input | Cached input | Output | Reasoning | Total | Uncached work | Wall time | Completed tools | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `single_codex` | 198,216 | 164,608 | 3,026 | 463 | 201,242 | 36,634 | 139.771 s | 8 | scored, unresolved |
| `agentteam_direct` | 252,599 | 200,192 | 6,858 | 2,701 | 259,457 | 59,265 | 410.473 s | 16 | `candidate_patch_invalid` |
| `agentteam_full` | 381,995 | 285,952 | 10,537 | 1,963 | 392,532 | 106,580 | 793.581 s | author 4, worker 15 | `evaluator_patch_conflict` |
| **Aggregate** | **832,810** | **650,752** | **20,421** | **5,127** | **853,231** | **202,479** | **1,343.825 s** | - | completed with two invalid quality outcomes |

`Uncached work` is `input - cached_input + output`. Reasoning tokens are
reported separately because the provider declares them within output usage.

Against the retained valid historical run:

| Mode | Historical total | Retest total | Change | Retest/single ratio |
| --- | ---: | ---: | ---: | ---: |
| `single_codex` | 318,079 | 201,242 | -36.732% | 1.0000x |
| `agentteam_direct` | 1,093,093 | 259,457 | -76.264% | 1.2893x |
| `agentteam_full` | 711,090 | 392,532 | -44.799% | 1.9505x |
| **Aggregate** | **2,122,262** | **853,231** | **-59.796%** | - |

The earlier two-field treatment consumed 3,042,089 tokens before full mode
could launch. This retest is 71.952% lower and completed all three modes. It is
still one historical-control repetition, so these percentages are engineering
evidence rather than a general causal estimate.

## Quality findings

### Single

The single candidate applied and received the same official result as the
retained baseline: FAIL_TO_PASS `0/4`, PASS_TO_PASS `104/109`, partial score
`0.290826`, unresolved. Lower cost did not improve measured quality.

### Direct

The direct worker used all 16 admitted tools: 15 commands and one file change.
It had implemented a multi-file candidate but reached the hard boundary before
post-patch tests. The worker correctly returned `failed`, reported incomplete
verification, and advised against merge. The scheduler therefore did not
integrate its worktree patch; the resulting empty candidate was rejected as
`candidate_patch_invalid`.

This is a strategy failure caused by the frozen boundary, not an infrastructure
failure. A common hard limit inferred from previously successful transcripts
does not guarantee room for final verification. Raising one number from this
sample is not enough; implementation roles need an explicit verification
reserve or a phase-aware boundary.

### Full

Full mode completed both provider invocations within their individual limits.
The author used 4 tools and the implementation worker used 15. Common
acceptance passed and the candidate changed `requests/models.py` plus
`tests/test_requests.py`. The production change restored handling for schemes
whose names begin with `http`, but the newly added test modified the same
section as the evaluator-only SWE-EVO test patch. Clean composition failed at
`tests/test_requests.py:2177`, so the frozen protocol correctly rejected the
candidate as `evaluator_patch_conflict` without a score.

The candidate was not modified after execution. Removing its test hunk and
reporting a score would be a post-hoc protocol change. The next benchmark
contract must decide before execution whether candidate test changes are
forbidden, separately retained but excluded from the scored patch, or composed
under another deterministic evaluator policy.

## Decision

The cumulative-context mechanism is accepted as effective containment evidence
for this Requests control: every invocation had complete usage, no provider
turn exceeded 16 completed tools, and aggregate cost fell materially. The
shared `12/16` tool policy is not accepted as the final Phase 3 policy because
direct mode lost its verification phase. End-to-end quality is also not
accepted because only single mode produced an official score and it remained
unresolved.

Phase 3 expansion remains unauthorized. Before another scored instance:

1. add and provider-free verify a role-aware or phase-aware tool budget that
   reserves enough calls for focused tests, diff inspection, and final report;
2. freeze one uniform benchmark candidate-test policy before provider launch;
3. retain active-context compaction, bounded output, disabled web search,
   complete usage accounting, serial execution, and existing resource limits;
4. run a provider-free replay and launch-registration audit before requesting
   another scored authorization.

# Phase 3B SWE-EVO minimal calibration report

## Status

The non-scored, gold-blind calibration completed once in all three execution
modes. All valid runs used the same immutable protocol, target commit, model,
reasoning profile, and provider-concurrency limit. This report does not claim an
official SWE-EVO score because the official evaluator image could not be pulled.

## Frozen identity

- SWE-EVO source commit:
  `9b83d5af943ba7a17567336f5b18239f73960219`
- Instance: `psf__requests_v2.27.0_v2.27.1`
- Target commit: `0192aac24123735b3eaf9b08df46429bb770c283`
- Target tree: `94369dd609f8ae984d2397ca8bde82369e348a60`
- AgentTeam runtime commit: `4894d9f`
- Protocol SHA-256:
  `01bd1bbca555a0c61f509a3d9077928bfd539033c27ed19ff45e8071170cedb3`
- Direct taskpack SHA-256:
  `59eaab2d00d61f4c26365c563d1e106c8f8c3f712af4095cc2a6d06e4fa672a8`
- Model and profile: `gpt-5.6-sol`, `high`
- Mode order: `single_codex`, `agentteam_direct`, `agentteam_full`
- Repetitions: one per mode; no semantic retry or repair invocation

## Provider usage

Provider-reported token totals use the runtime's Codex JSONL accounting. Total
tokens equal input plus output tokens; reasoning tokens are a subset of output
tokens and must not be added again.

| Mode/stage | Input | Cached input | Output | Reasoning | Total | Provider wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_codex` | 139,582 | 124,160 | 2,386 | 530 | 141,968 | 94 s |
| `agentteam_direct` worker | 821,116 | 727,808 | 9,410 | 3,282 | 830,526 | 308 s |
| `agentteam_full` author | 100,552 | 85,760 | 3,412 | 418 | 103,964 | 101 s |
| `agentteam_full` worker | 535,642 | 484,864 | 10,120 | 4,814 | 545,762 | 251 s |
| `agentteam_full` combined | 636,194 | 570,624 | 13,532 | 5,232 | 649,726 | 352 s |

Aggregate usage was 1,622,220 tokens, within the frozen 2,000,000-token
protocol budget. Relative to `single_codex`, direct mode consumed 5.85 times
the tokens and full mode consumed 4.58 times. Full mode consumed 21.8% fewer
tokens than direct mode in this one-sample calibration, despite taskpack
authoring accounting for 103,964 tokens (16.0% of its total). One sample is not
enough to infer a stable quality or cost ranking.

## Candidate results

All three runs completed, passed the common acceptance command, published one
candidate patch, and passed pre-run and post-run canary scans.

| Mode | Run ID suffix | Patch SHA-256 | Bytes | Changed files |
| --- | --- | --- | ---: | --- |
| `single_codex` | `b8594d9f...c629811b` | `aa692ec2e0bcb1e47021ae621aa172069fa0876169a1ffb8b35cc26a20802549` | 1,248 | `requests/utils.py`, `tests/test_utils.py` |
| `agentteam_direct` | `9e5c076d...9ba7166` | `592b61741da779c142a241974e9f6bd2f6090867e1a9c218a286bcadc2815c81` | 1,242 | `requests/utils.py`, `tests/test_utils.py` |
| `agentteam_full` | `add42cb8...90b62d` | `08dd2022bba38bd8b08cdd028c800ebc423b29ca24024ab954ce1558665ba9a2` | 1,310 | `requests/utils.py`, `tests/test_utils.py` |

Each candidate restores the parsed proxy authentication component before URL
reconstruction and adds a focused regression test. Direct and full mode place
the restoration after the existing netloc/path compatibility swap, matching
the benchmark reference placement. Single mode places it immediately before
that swap. The tested schemed and protocol-relative forms behaved identically
across the three candidates.

## Evaluation evidence

- At the exact base commit, both public FAIL_TO_PASS values lose authentication.
- All three candidate patches preserve both public values:
  `http://user:pass@example.com/path?query` and
  `http://user@example.com/path?query`.
- The common isolated acceptance command passed for all three modes.
- A host-local run of the complete `tests/test_utils.py` was attempted for the
  base and all candidates. Collection failed identically before tests ran
  because the host supplies `urllib3 2.0.7`, while requests 2.27.0 requires
  `urllib3 <1.27`; `SNIMissingWarning` is absent from the host version.
- The official image
  `ghcr.io/epoch-research/swe-bench.eval.x86_64.psf__requests-6028` was not
  available locally. Two pull attempts ended in registry transfer errors
  (`unexpected EOF` and TLS handshake timeout). Therefore official FAIL_TO_PASS,
  PASS_TO_PASS, and resolved status remain unavailable.

The public behavior checks are useful calibration evidence, but they are not a
substitute for an official container score.

## Framework defects found and fixed

1. Blocked evaluator evidence could contain a null post-run leak scan and crash
   validation. The validator now accepts the documented blocked form and has a
   regression test.
2. Full-mode taskpack authoring inherited Codex's read-only default despite a
   writable outer sandbox. The supported author command now supplies
   `workspace-write` unless a sandbox mode was explicitly configured.
3. Experiment cleanup removed candidate workspaces without retaining patches.
   Every terminal run now publishes an immutable candidate patch and metadata,
   including committed, uncommitted, and untracked changes relative to the
   frozen base, before cleanup. Patch publication uses a temporary Git index and
   participates in the canary scan.

These fixes are committed as `7ef6859` and `4894d9f`. Focused regression tests
pass. A broader 1,074-test run performed before the final patch-retention test
change exposed one unrelated baseline ownership-count mismatch in the M0 suite;
the same mismatch reproduces on the unmodified parent branch. The focused tests
covering all three fixes pass after the final change.

## Decision

This calibration demonstrates that the harness can execute and account for all
three modes under one protocol and retain comparable candidate evidence. It
also shows large orchestration overhead on this small task. Do not use this
single instance to authorize a quality claim or a scored pilot. First restore
official image availability, obtain an official score for this instance, and
then run the preregistered multi-instance pilot.


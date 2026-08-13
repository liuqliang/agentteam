# Phase 3B live smoke v12-v19

- Decision: `DEC-P3B-live-pilot-execution`
- Evidence level: `L3`
- Status: execution plumbing proven; scored comparison blocked
- Cost-profiler implementation base: `1c73bcb`

## Result

The live pilot now reaches a real Codex worker, preserves provider-reported
usage, seals a candidate patch, runs the common evaluator, launches the frozen
SWE-bench evaluator in the approved resource hierarchy, and cleans all owned
systemd units. The first end-to-end worker execution that had functioning
tools was epoch v19 for `psf__requests_v2.4.0_v2.4.1` in `single_codex` mode.

That worker read the repository, ran about 20 repository commands, and emitted
a 3,523-byte patch covering the four visible Requests 2.4.1 release goals. Its
provider-reported usage was:

| Metric | Value |
| --- | ---: |
| Input tokens | 980,995 |
| Cached input tokens | 903,424 |
| Uncached input tokens | 77,571 |
| Output tokens | 12,897 |
| Reasoning tokens | 6,127 |
| Total tokens | 993,892 |
| Provider wall time | 296 seconds |

The official evaluator applied the patch and started 138 tests, but timed out
at 1,800 seconds after reaching 39 percent. It therefore produced no valid
official score. This epoch is infrastructure-failed evidence, not a zero-score
model result and not a sample in the three-mode quality comparison.

## Epoch audit

| Epoch | Outcome | Provider usage | Experimental status |
| --- | --- | ---: | --- |
| v12 | source authority ordered after supervisor setup | 0 | invalid infrastructure epoch |
| v13 | acceptance executable was not normalized | 60,539 | invalid infrastructure epoch |
| v14 | evaluator service lacked runtime module path | 60,573 | invalid infrastructure epoch |
| v15 | evaluator service identity collided across pilots | 76,236 | invalid infrastructure epoch |
| v16 | score completed, terminal usage projection failed | 76,044 | recoverable plumbing evidence only |
| v17 | Codex code-mode host was absent from sandbox | 75,957 | invalid infrastructure epoch |
| v19 | real patch produced; official evaluator timed out | 993,892 | evaluator-failed smoke evidence |

The known provider total across v13-v17 and v19 is 1,343,241 tokens. Failed
epochs remain outside scored comparison but must remain in engineering-cost
accounting.

`phase3_cost_profile.v1` now reconstructs these costs from immutable invocation
terminals without changing the frozen result-bundle schema. It separates
cached and uncached input, stage, role, mode, outcome, model wall time,
controller/common-acceptance overhead, and official-evaluator wall time. It
also includes terminal provider usage from unsealed failed runs and labels that
cost as infrastructure waste instead of silently dropping it.

For v19 the machine-reconstructable model cost is therefore 993,892 total
tokens but only 77,571 uncached input tokens. The profile must retain both
numbers: total tokens are the provider's usage unit and budget authority,
whereas uncached input is the useful signal for repeated-context overhead.
The old v19 controller failed before writing an official-evaluator failure
receipt, so its 1,800-second timeout cannot be reconstructed from retained
machine authority. The profiler reports that evidence gap instead of importing
the prose value into a supposedly mechanical result.

Run the projection with:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.phase3_cost_profile \
  --pilot-root /tmp/agentteam-phase3-live-pilot-v19-43a7f54
```

## Corrections

The live execution path was corrected to:

- bind repository authority before resource supervision;
- normalize taskpack and common acceptance executables;
- pass the immutable runtime module path into evaluator services;
- isolate evaluator service identities by pilot and entry;
- project invocation usage coverage into terminal results;
- mount `codex-code-mode-host` beside the frozen Codex executable;
- classify provider tool-host failures as infrastructure failures;
- skip official scoring after a provider infrastructure failure;
- ignore evaluator-created untracked files when checking tracked source drift;
- seal evaluator failures and recover stopped active checkpoints without a
  duplicate provider invocation.

The affected suite passed 84 tests with 5 environment skips. The complete
runtime suite passed 1,162 tests with 7 skips.

## Blocking findings

The full 18-to-27 execution pilot must not start yet.

1. The Requests official evaluator is unstable for comparison. The same frozen
   empty-patch evaluation previously ranged from about 54 to 669 seconds; the
   real v19 patch reached the frozen 1,800-second timeout. Quality may still be
   measurable after an evaluator repair, but total wall time is not currently
   a controlled mode-comparison metric.
2. Worker-visible verification is not environment-equivalent to the official
   evaluator. The common and taskpack acceptance commands use host Python 3.12,
   where Requests 2.4.0 cannot import its vendored urllib3, while the official
   container uses Python 3.9. Direct and full modes could therefore reject a
   correct patch before official scoring.
3. Historical failed epochs did not project provider usage into pilot state.
   Commit `449be49` fixes future recovery and sealing, but historical states
   remain immutable and are accounted for by this report.
4. The fixed Requests image was probed without applying evaluator-only test
   patches. It contains the correct base commit, Python 3.9 environment, and
   collects 138 public tests, but those tests include unstable network-facing
   behavior. The next epoch must select a deterministic instance and freeze a
   controller-owned public verification runner for that instance. Exposing the
   Podman socket or evaluator-only Arrow fields to workers is forbidden.

## Evidence

Primary local evidence remains outside Git because it contains bounded raw
provider output and evaluator logs:

- v17: `/tmp/agentteam-phase3-live-pilot-v17-a8f36f4`
- v19: `/tmp/agentteam-phase3-live-pilot-v19-43a7f54`
- v19 candidate patch:
  `mode-runs/psf__requests_v2.4.0_v2.4.1/runs/experiment-run-a12c14583d287a4b4caa5abe06b932424577c448b052b6108fdad316a0256685/artifacts/candidate.patch`
- frozen evaluator environment: `/tmp/agentteam-phase3-evaluator-venv-v1`
- frozen dataset SHA-256:
  `74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520`
- frozen SWE-bench commit:
  `9b83d5af943ba7a17567336f5b18239f73960219`

The next execution decision must repair and re-preflight evaluator stability
and equal-input verification before authorizing another scored epoch.

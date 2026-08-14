# Phase 3B live smoke v12-v19

- Decision: `DEC-P3B-live-pilot-execution`
- Evidence level: `L3`
- Status: execution plumbing and fair public verification proven; new scored
  epoch not frozen
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

## Public verification repair

Provider-free follow-up on 2026-08-14 implemented
`phase3_public_verification_environment.v1`. New live bundles now require one
digest-bound environment authority per instance. The controller extracts only
`/opt/miniconda3/envs/testbed` from the digest-pinned official image and binds
the complete tree, Python executable, Python version, instance ID, image digest,
and fixed namespace path. The environment is mounted read-only; the Podman
socket, Arrow dataset, evaluator harness, gold patch, and hidden test patch are
not provider views.

The unused `psf__requests_v2.12.2_v2.12.3` instance was selected for a
provider-free calibration probe. Its exact image environment contains Python
3.9.20 and a 191,394,742-byte dependency tree with 9,019 files. A public
behavior assertion for parameter handling on `http+unix` URLs produced:

| State | Repetitions | Result | Per-run wall time |
| --- | ---: | --- | ---: |
| Frozen base commit | 3 | RED | 0.04 s |
| Independently derived one-line fix | 3 | GREEN | 0.04-0.10 s |

The same RED and GREEN results were reproduced inside the actual bubblewrap
provider namespace. Environment extraction plus authority creation took 2.40
seconds; a complete sandbox authority, canary probe, source revalidation, and
assertion launch took 5.62 seconds. This is deterministic controller overhead,
not model-token cost.

The runtime taskpack retains a portable `python3` command. Worker PATH,
integration verification, and common visible evaluation resolve that command
to the same bound Python 3.9 environment. The protocol binds the environment
authority digest through `dependency_cache_policy`. Ordinary library views keep
their prior 64 MiB/10,000-entry integrity bounds; only an explicit
`bounded_dependency_tree.v1` view receives the 512 MiB/20,000-entry bound.

No provider call or scored execution was made during this repair. The
diagnostic Requests change was removed from the temporary source worktree after
the RED/GREEN proof. The focused Phase 3 runner and environment suite passed 32
tests. The complete runtime suite passed 1,173 tests with 7 environment skips.

## Blocking findings

The full 18-to-27 execution pilot must not start yet.

1. The Requests official evaluator is unstable for comparison. The same frozen
   empty-patch evaluation previously ranged from about 54 to 669 seconds; the
   real v19 patch reached the frozen 1,800-second timeout. Quality may still be
   measurable after an evaluator repair, but total wall time is not currently
   a controlled mode-comparison metric.
2. Epochs v12-v19 remain bound to host Python 3.12 and are not valid comparison
   samples. The implementation now enforces image-equivalent public Python for
   every newly built live bundle, but a new selection, protocol, and epoch must
   be frozen before that repair has scored evidence.
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

The next execution decision must freeze the unused deterministic instance, its
public assertion, and its new environment authority, then run exactly one
repetition per mode before considering a wider scored pilot.

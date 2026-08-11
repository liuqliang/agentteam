# Phase 3B SWE-EVO minimal calibration

## Decision

Run one non-scored, gold-blind calibration instance once in each Phase 2
execution mode before authorizing the scored Phase 3 pilot. This calibration
measures whether the live harness can produce comparable token, wall-time, and
patch evidence. It does not contribute to Phase 3 benchmark claims or release
promotion.

## Frozen input

- Benchmark source: `SWE-EVO/SWE-EVO` at
  `9b83d5af943ba7a17567336f5b18239f73960219`.
- Instance: `psf__requests_v2.27.0_v2.27.1`.
- Target repository commit:
  `0192aac24123735b3eaf9b08df46429bb770c283`.
- Target repository tree:
  `94369dd609f8ae984d2397ca8bde82369e348a60`.
- Public goal: fix the proxy URL parsing defect that drops the `auth`
  component.
- Runtime-visible inputs exclude the benchmark patch, test patch, hidden test
  identities, and evaluator-only artifacts.

## Execution contract

- Modes: `single_codex`, `agentteam_direct`, and `agentteam_full`.
- Repetitions: one per mode.
- Order: deterministic seeded order recorded in the immutable protocol.
- Provider concurrency: one invocation at a time.
- Retries: no semantic retry; infrastructure failures remain terminal evidence.
- Repository isolation: one exact-commit clean snapshot per mode.
- Direct mode receives a preregistered deterministic taskpack. Full mode must
  author a new taskpack and cannot read the direct taskpack.
- The same model, reasoning profile, sandbox policy, goal, and acceptance
  command apply to all modes.
- Protocol budget: at most 2,000,000 aggregate tokens and 10,800 aggregate
  wall-clock seconds, enforced at provider boundaries.

## Acceptance and scoring

The common harness acceptance command asserts that
`prepend_scheme_if_needed()` preserves user/password authentication in an HTTP
URL. The assertion is confirmed to fail at the bound base commit.

After all modes terminate, each candidate patch should also be evaluated with
the official SWE-EVO instance image when the container image and rootless
runtime are available. Local acceptance and official benchmark scoring are
reported separately. A missing official score must be recorded as an
infrastructure limitation, never as a pass.

## Required report

For every mode record terminal status, provider invocation count, input,
cached-input and output tokens when reported, total tokens, wall time, changed
files, acceptance result, and official evaluator status. Also record taskpack
authoring cost separately for full mode and any framework defects encountered.

Operator review is required before this plan, generated experiment artifacts,
or any target patch is integrated or used to authorize the scored pilot.

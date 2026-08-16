# Phase 3 DVC medium-instance calibration

Status: completed; calibration did not authorize a scored pilot

## Decision

Run the already preregistered medium-complexity instance
`iterative__dvc_2.19.0_2.20.0` exactly once in each Phase 3 mode. This is the
first new-instance check after the Requests mechanism repairs. It measures
whether the current runtime can complete a larger repository task with bounded
cost; one instance is not a general performance claim.

## Frozen Identity

- source commit: `78dd045d29f274960bcaf48fd2d055366abaf2c1`
- source tree: `ddbe1d3231306250970a0b6ff9384838643d31d3`
- runtime commit: `0ef80844ee10948e2b21a59fc675a9b5621f9cf0`
- model: `gpt-5.6-sol`, reasoning profile `high`
- mode order: `single_codex`, `agentteam_direct`, `agentteam_full`
- repetitions: one per mode; continuation forbidden
- provider concurrency: one invocation at a time
- per-mode ceiling: `600000` provider-reported total tokens and `1800` seconds
- aggregate ceiling: `1800000` provider-reported total tokens and `5400`
  seconds
- public environment image:
  `sha256:cccdad0b5064f23be9d6e1047238a9fc3f3d3a528015427eed8d17d8fd1ece73`
- public environment authority:
  `e59020c51edebc237b8b53b9012452197504c70c3e781403e5d5c9b23bfca5f9`

The first extracted environment authority `e755332f...` was invalidated before
provider launch when an operator-side `pytest --version` probe wrote bytecode
into its mutable host path. The validator detected the changed tree. That
environment and its partial bundle are discarded with zero provider calls;
the execution binds only the clean, policy-bound `v3` authority above.

The first bundle built from the clean environment exposed a provider-free
runner defect: the evaluator received all three candidate-authority instances
while the source map correctly covered only the selected DVC instance. Runtime
commit `c470610d...` projects evaluator candidates through the frozen
selection. The earlier `e5f934f...` bundle is discarded with zero provider
calls.

The next provider registration failed before launch because sandbox mount
identity still interpreted the public environment with the smaller generic
dependency-tree bound. Runtime `9d8bbf5...` introduces the explicit
`bounded_public_dependency_tree.v1` mount policy and revalidates that policy at
launch. The failed run retained zero tokens and no provider usage artifact.

The first provider-complete `single_codex` execution consumed 352125 total tokens and
received a complete official score (`F2P 6/14`, `P2P 64/66`, unresolved,
partial score `0.593939`). Its additional common-acceptance run failed, but a
provider-free replay established that the repository-wide `pytest -rA`
command is not a discriminating acceptance oracle for this image: after CA
trust was repaired it still reported 254 failures and 608 errors from baseline
environment assumptions and emitted 6224372 bytes. The official SWE-EVO
evaluator remains the score authority.

The attempted continuation then exposed a distinct budget-scope defect before
the direct worker received a task. One protocol-global controller shared the
600000-token and 1800-second allowance across all three modes and counted the
operator review pause as execution time. Runtime `0ef8084...` separates each
mode run's provider lane and budget clock from the protocol-wide immutable mode
sequence. Because this changes the execution treatment, the earlier single
result is retained only as infrastructure-recovery cost and diagnostic quality
evidence. The scored v7 calibration restarts all three modes from zero under the
new runtime; the failed direct continuation made zero provider calls.

The token ceiling is checked at controller boundaries. An admitted provider
turn can report usage after it has crossed a ceiling, but no later turn or mode
may start after the overrun is observed. All reported retry usage counts.

## Isolation

The worker receives the release-note goal, frozen source repository, and
digest-bound public dependency environment. Benchmark patches, hidden tests,
official test identifiers, and evaluator state remain evaluator-only. Each
mode starts from the same source commit and cannot read sibling-mode output.

Candidate tests under conventional public test paths are retained as
supplemental evidence but excluded from the scored production patch. The
official evaluator first verifies that the production patch composes cleanly
with the hidden test patch.

## Stop Conditions

Stop before another provider launch if any of the following occurs:

- aggregate or per-mode budget is exhausted;
- provider usage is missing or cannot be reconciled;
- runtime, source, public environment, or protocol identity drifts;
- resource ownership or cleanup fails;
- gold visibility or cross-mode isolation is violated;
- official evaluation becomes unavailable for infrastructure reasons;
- corrective semantic operator input would be required inside a mode.

A valid but unresolved patch is retained as a quality result. It is not an
infrastructure failure and does not authorize an extra attempt.

## Acceptance

1. Provider-free replay validates every authority and reports zero model calls
   before live authorization.
2. Exactly one terminal execution is attempted for each mode unless a frozen
   stop condition prevents a later mode.
3. Every launched provider invocation has complete input, cached input,
   output, reasoning, total-token, and wall-time evidence.
4. Every accepted production patch has compatibility and official evaluator
   evidence; rejected patches retain their cost and rejection class.
5. The final report compares quality, total and uncached token cost, provider
   time, controller time, evaluator time, tool count, and changed production
   paths without claiming statistical significance.

## Outcome

The fresh v7 run completed all three frozen modes with complete provider usage
coverage and no identity or isolation failure. The result does not support
promotion:

- `single_codex` produced a valid two-file patch and received official partial
  score `0.593939`, but did not resolve the instance;
- `agentteam_direct` consumed more tokens and time than `single_codex`, then
  exhausted the fixed 16-call tool budget after patch-context failures and
  published no candidate patch;
- `agentteam_full` spent `326656` tokens on taskpack authoring and `262118`
  tokens on a repository-map worker. The repository-map worker also exhausted
  the fixed tool budget before writing its required handoff, so the dependent
  implementation task never ran;
- the repository-wide common acceptance command remained non-discriminating
  and exceeded the retained-output limit in every mode.

The immutable run summary is retained in
`acceptance/phase3-dvc-medium-calibration-v1/result.json`; interpretation and
follow-up requirements are in `report.md` and decision revision 7. No result
from this one-instance calibration is a general quality claim.

# Phase 3 Benchmark Readiness

Status: **provider-free implementation and host integration checks passed**.
This result does not authorize a scored benchmark run or close the `P3-READY`
post-backlog gate.

## What was verified

The provider-free fixture follows the complete pre-execution authority path:
fixed local SWE-EVO metadata, deterministic complexity-stratified selection,
immutable preregistration, equal three-mode visible-input and budget bindings,
mode-root isolation, and D5 decision binding/replay for
`DEC-P3-readiness-execution`. The test publishes a canonical receipt twice in
a temporary `acceptance/phase3-readiness.json` location and proves the second
publication is idempotent.

Mutation checks reject changed selection order and a changed per-mode budget.
Every mode can resolve its own runtime namespace and is denied access to a
sibling namespace. Gold remains evaluator-only and no prior outcome is added
to runtime-visible input.

## Usage and isolation reconciliation

The receipt schema requires all of the following values together:

- `live_provider_calls: 0`
- `scored_mode_executions: 0`
- `invocations_created: 0`
- `provider_status: not_invoked`
- `terminal_usage_records: 0`
- `terminal_usage_status: not_applicable`
- `token_totals: null`

This records provider absence rather than inventing a terminal usage record
whose token values happen to be zero. The focused path does not construct
`ModelInvocationCall`, does not enter provider admission, and does not execute
`single_codex`, `agentteam_direct`, or `agentteam_full` as scored samples.

## Receipt contract

`phase3_readiness_receipt.v1` binds the local fixture digest, ordered selected
instances and selection digest, preregistration authorization digest, decision
contract and inherited decision, per-mode input/budget/root reconciliation,
isolation denials, usage reconciliation, and digest-bound verification
results. `receipt_sha256` covers all receipt content except itself; immutable
publication additionally returns the digest of the complete serialized
receipt.

The deterministic post-backlog controller is the owner of the retained
`acceptance/phase3-readiness.json` evidence artifact. The test publishes only
to a temporary fixture root, so this implementation does not pre-author an
acceptance decision or commit mutable run evidence.

## Verification

- Focused readiness suite: `3` tests passed.
- P3-01/P3-02 dependency suite: `24` tests passed.
- Selected P2/D5/usage regressions: `17` tests passed.
- Schema validation: Draft 2020-12 schema accepted.
- Artifact lint: passed (`145` JSON files, `1` JSONL file, `0` errors).
- Worker-sandbox runtime suite: `1017` tests ran, `5` skipped, with `1` failure
  and `2` errors caused by the sandbox denying local socket binding.
- Host integration runtime suite: `1017` tests passed with `4` skipped. This
  rerun used the same P3-03 code state in an environment that permits the local
  Unix and loopback HTTP fixtures.

Commands:

```text
cd experiments/native_agentteam_runtime/m0_runtime
python3 -m unittest discover -s tests -p 'test_phase3_benchmark_readiness.py' -v
python3 -m agentteam_runtime.artifact_lint --root ..
python3 -m unittest discover -s tests -p 'test*.py'
```

## Operator decision

The implementation is eligible for operator-reviewed integration because the
host suite passed. The configured `P3-READY` gate still lacks a registered
deterministic controller that creates and retains
`acceptance/phase3-readiness.json`; the current test publishes only to a
temporary fixture root. Implement and verify that controller before closing
Phase 3A. A later Phase 3B taskpack must separately reference the passed
retained receipt and freeze its actual instance list, budgets, mode order, and
live-run authority. Do not interpret this readiness result as a benchmark
score, provider-usage claim, or superiority claim.

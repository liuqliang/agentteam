# Phase 3 Benchmark Readiness

Status: **implementation checks passed; integration evidence incomplete**.
This result does not authorize a scored benchmark run or integration.

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
- Complete runtime suite: `1017` tests ran, `5` skipped, `1` failure and
  `2` errors. The three non-passing tests require socket binding, which this
  execution sandbox denies with `PermissionError: [Errno 1] Operation not
  permitted`: one Unix supervisor readiness test and two local HTTP webhook
  tests. An independent probe reproduced `unix_bind_denied errno=1`; Internet
  socket creation is denied as well.

Commands:

```text
cd experiments/native_agentteam_runtime/m0_runtime
python3 -m unittest discover -s tests -p 'test_phase3_benchmark_readiness.py' -v
python3 -m agentteam_runtime.artifact_lint --root ..
python3 -m unittest discover -s tests -p 'test*.py'
```

## Operator decision

Do not integrate this patch until the complete suite is rerun in a host that
permits its local socket fixtures and passes. Merge only through verified
integration followed by operator review. A later
Phase 3B taskpack must separately reference the passed retained receipt and
freeze its actual instance list, budgets, mode order, and live-run authority.
Do not interpret this readiness result as a benchmark score, provider-usage
claim, or superiority claim.

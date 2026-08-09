# Phase 3 Benchmark Readiness

Status: **Phase 3A closed; `P3-READY` passed operator review**.
This result does not authorize a scored benchmark run.

## Retained gate execution

The controller-only `phase3a-readiness-closure-v4` run completed against
release `phase3a-closure-ebce7b1` and integration head
`ebce7b10a0ab7b2f9dc3e3936df3f7c2e1e77a02`. Gate epoch `1` retained the
canonical receipt at `acceptance/phase3-readiness.json`; its complete evidence
digest is
`d87486c41ef79707d52408215b05d22b52d736d16380001590d031c4fb483af7`,
and its receipt-content digest is
`5fa7ff18044f2fcf5c01ee7d33b2592091d6af91bd09c1de641eeffe3a0bfc9d`.

The registered relation validator passed before interactive approval. The
approval is bound to gate epoch `1`, the evidence digest above, and the exact
integration head. The run then transitioned from
`awaiting_operator_review` to `completed`. It started no worker pool and
retained no model invocation start record; the receipt reports zero provider
calls, zero scored executions, zero invocation and terminal-usage records,
and `token_totals: null`.

An earlier closure attempt published an incorrectly identified execution
decision into its append-only experimental ledger before failing. That old
work root remains unchanged as failure evidence. The accepted v4 run used a
fresh authority namespace and replayed the original Phase 3 direction and
readiness-execution decisions without changing their identity or revision.

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

The production `phase3_readiness` module builds, validates, and immutably
publishes `phase3_readiness_receipt.v1`. The receipt binds the local fixture
digest, ordered selected
instances and selection digest, preregistration authorization digest, decision
contract and inherited decision, per-mode input/budget/root reconciliation,
isolation denials, usage reconciliation, and digest-bound verification
results. `receipt_sha256` covers all receipt content except itself; immutable
publication additionally returns the digest of the complete serialized
receipt.

`P3-READY` is registered as an action-free, provider-free deterministic gate.
Its production relation validator repeats the schema, canonical receipt,
verification, equal-input, equal-budget, and mode-root checks before a gate
can pass. A concrete evidence run remains the owner of the retained
`acceptance/phase3-readiness.json`; registering the controller does not
pre-author an acceptance decision or commit mutable run evidence.

## Verification

- Focused readiness and gate-registry suite: `8` tests passed.
- P3-01/P3-02 dependency suite: `24` tests passed.
- Selected P2/D5/usage regressions: `17` tests passed.
- Schema validation: Draft 2020-12 schema accepted.
- Artifact lint: passed (`145` JSON files, `1` JSONL file, `0` errors).
- Worker-sandbox runtime suite: `1017` tests ran, `5` skipped, with `1` failure
  and `2` errors caused by the sandbox denying local socket binding.
- The accepted v4 sealed command discovered `1026` tests and passed at the
  bound integration head. Its result digest is
  `11d69be35f1eb6033bf7e753aa63e216befff62b096d6a10bc6119f2b4081ea1`.

Commands:

```text
cd experiments/native_agentteam_runtime/m0_runtime
python3 -m unittest discover -s tests -p 'test_phase3_benchmark_readiness.py' -v
python3 -m agentteam_runtime.artifact_lint --root ..
python3 -m unittest discover -s tests -p 'test*.py'
```

## Operator decision

Phase 3A is complete. Future taskpacks that use `P3-READY` must bind
`phase3_readiness_controller_v1` and `phase3_readiness_relation_v1`; historical
frozen taskpacks remain unchanged. A Phase 3B taskpack must separately
reference the passed retained receipt and freeze its actual instance list,
budgets, mode order, model profile, and live-run authority. Do not interpret
this readiness result as a benchmark score, provider-usage claim, or
superiority claim.

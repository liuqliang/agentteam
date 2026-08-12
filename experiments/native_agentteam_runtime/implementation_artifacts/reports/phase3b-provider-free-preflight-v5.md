# Phase 3B provider-free preflight v5

- Task: `P3B-R03`
- Decision: `DEC-P3B-v5-resource-execution`
- Integration code state: `d07e66e607a0105826d8536378c1f4984f961ec6`
- Evidence level: `L2`
- Evidence status: `complete`
- Preflight disposition: provider-free preparation verified; operator review remains required
- Live status: `P3-LIVE` is unauthorized

## Bound preparation inputs

The verification continued from the operator-accepted Phase 3B projection and the integrated R02 resource implementation. The frozen preparation bindings remain:

| Binding | SHA-256 |
| --- | --- |
| Complexity projection | `f103e61548af36b41ad5b32f6e05941827ab52ace24ec3ce4c1663ca01cb88e1` |
| Routing manifest | `ce7e368c34d2be326342134e6ad9c8bf6126b75b50f065bc2a309819c1def295` |
| Selection | `9103915631132adb25542da5df655802ee3b2fe974a3d5808fd62630619bbc77` |
| Approved resource envelope | `71218a0289cedb692889e734f9f0757614d00a10baf9c773d3bf35c0a5fa08f2` |
| Provider-free preflight receipt | `1e50fc43b43520aae937a82871993577004865624b8a4f32c84e1ca70bbfafa4` |

## Focused verification results

Command, run from the repository root:

```text
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_phase3_pilot_preparation experiments.native_agentteam_runtime.m0_runtime.tests.test_phase3_resource_envelope experiments.native_agentteam_runtime.m0_runtime.tests.test_model_invocation experiments.native_agentteam_runtime.m0_runtime.tests.test_experiment_harness
```

Result: `Ran 225 tests in 81.646s`; `OK (skipped=6)`; exit code `0`.

This lane covers the gold-blind projection and replay guards, exact versioned resource envelopes, pre-execution property readback, descendant cleanup, counter evidence, resource-exhaustion classification, model-worker admission, evaluator bounding, and historical protocol compatibility. The six skipped cases did not produce failures or errors.

The first root-level invocation omitted the required runtime `PYTHONPATH`; it returned one import-loader error for `test_phase3_pilot_preparation` after running the other modules. The command above corrects only the import environment and is the canonical focused result; no source or test change was needed.

## Provider-free integration result

Command, run from the repository root:

```text
scripts/test-native-runtime.sh integration
```

Result: `Ran 707 tests in 79.882s`; `OK (skipped=2)`; exit code `0`.

This matches the v5 blueprint's 707-test provider-free integration baseline. No live-launch command, provider admission, merge, push, or release activation was performed.

## Zero scored-call evidence

A deterministic receipt was built from the provider-free aggregate fixture and the approved resource binding. Its reconciled fields are:

```json
{"admission_status":"denied","live_provider_calls":0,"permit_issued":false,"preflight_receipt_sha256":"1e50fc43b43520aae937a82871993577004865624b8a4f32c84e1ca70bbfafa4","resource_envelope_sha256":"71218a0289cedb692889e734f9f0757614d00a10baf9c773d3bf35c0a5fa08f2","scored_mode_executions":0}
```

The focused suite additionally asserts that receipt mutation, stale or mismatched authority, rejected authority, and expanded authority all fail closed. Therefore the evidence records zero scored provider calls and leaves `P3-LIVE` denied.

## Conclusion and boundary

All R03 acceptance checks are complete at L2: focused verification passes, the 707-test provider-free integration lane passes, zero scored calls are reconciled, and the live gate remains closed. This report supports a later taskpack-freeze or operator-review decision only. It does not authorize `P3-LIVE`, a scored model execution, merge, push, or release activation.

# Phase 3B Provider-Free Preflight

Status: **implementation preflight passed; `P3-LIVE` remains denied**.

This report covers deterministic implementation fixtures only. It does not
freeze the unresolved experiment decisions, publish a
`phase3_live_authorization.v1` artifact, issue a live-launch permit, invoke a
scored provider, or report benchmark outcomes.

## Materialized authority path

`phase3_pilot_preparation` now validates the complete ordered chain before it
builds an aggregate contract:

1. the frozen selection authority and its canonical digest;
2. one distinct runtime-visible input and frozen direct taskpack per selected
   instance;
3. one immutable preregistration per selected instance, including equal
   three-mode input and budget bindings;
4. the accepted `P3-READY` evidence and fixed SWE-EVO dataset binding;
5. the aggregate token and wall-time ceilings calculated by
   `build_phase3_pilot_contract` from every maximum scheduled repetition.

The provider-free receipt is validated by
`phase3_pilot_preflight.schema.json`. Its schema requires zero live provider
calls, zero scored executions, zero invocation records, no token totals, no
authorization artifact, and `permit_issued: false`. The receipt and its
verification section each carry canonical SHA-256 bindings.

## Deterministic fixture result

The focused fixture selected three instances. Each instance binds three modes,
at most three repetitions, `1,000` tokens per mode/repetition, and `120`
seconds per mode/repetition. The mechanically reconciled aggregate ceilings
were therefore:

- `maximum_total_tokens: 27000`;
- `maximum_wall_time_seconds: 3240`;
- `max_inflight_model_invocations: 1`.

Changing a preregistration-bound visible task changed both the instance
materialization digest and the aggregate contract digest. Re-signed receipt
mutations that claimed a provider call were rejected by the preflight schema.
Materialization digest drift was also rejected before receipt construction.

## Live-authorization denial evidence

The provider-free tests attempted admission with each of the following unsafe
inputs: missing authorization, operator rejection, stale epoch, mismatched
selection digest, and an expanded token ceiling. Every case was denied by the
existing Phase 3 live-admission validator before a permit could be returned.

The generated preflight receipt itself always records:

```text
gate_id: P3-LIVE
contract_status: not_authorized
authorization_artifact_supplied: false
permit_issued: false
admission_status: denied
denial_reason: mandatory_operator_review_and_epoch_authorization_required
```

## Verification

- `python3 -m unittest discover -s tests -p 'test_phase3_pilot_preparation.py' -v`:
  `26` tests passed.
- `python3 -m unittest discover -s tests -p 'test_phase3_pilot*.py' -v`:
  `33` tests passed.
- `python3 -m agentteam_runtime.artifact_lint --root ..`:
  `167` JSON files and `1` JSONL file checked, `0` errors.
- `scripts/test-native-runtime.sh integration`:
  `701` tests passed, `2` skipped.

No live provider call or scored mode execution occurred during these checks.

## Operator gate

The implementation is suitable for merge review. A later, separately
authorized task must supply the actual approved experiment decisions, review
the materialized instance authorities and aggregate ceiling, open a fresh
`P3-LIVE` epoch, and publish an exactly matching operator authorization before
any scored execution. This report must not be used as that authorization.

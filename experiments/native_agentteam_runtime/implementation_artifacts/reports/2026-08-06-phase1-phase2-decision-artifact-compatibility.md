# Phase 1/Phase 2 Decision-Artifact Compatibility Report

Status: passed

## Authority

- Taskpack plan SHA-256:
  `cc03c0d24af7d0eed76334260b564eef8eda111e2ec09a4bd6cf8377adfd1860`
- Blueprint SHA-256:
  `69666f419ee82eebabdda9ea0759e85dc7ca02719d188cf0644d5dde91b9fc0e`
- Frozen taskpack digest:
  `cf2d9c67c4c4a4bcd9319361acd4252716c2887656913efe73f8ef46461f4892`
- Implementation commit: `c488859`
- Taskpack contract repair commits: `dc26d48`, `8512704`
- Provider calls: `0`

## Historical Integrity

The following accepted authorities are byte-unchanged relative to D5
completion commit `30dc381`:

- Phase 1 plan and blueprint;
- Phase 1 model-invocation usage report;
- Phase 2 plan and blueprint.

The audit did not edit a historical taskpack, review, report, calibration
artifact, run record, token total, or sealed result bundle. Existing P1/P2
taskpacks remain on the legacy execution path and may receive only additive
legacy decision indexes.

## Findings And Repairs

### P1 compatibility

A passed `model_invocation_live_smoke.v1` artifact now becomes an explicit
decision-linked `evidence` artifact during D5 terminal retention. Its declared
raw spool must be a regular in-run file no larger than the existing 1 MiB P1
limit. The spool is retained byte-for-byte. Missing, escaping, symlinked,
oversized, or non-passed acceptance authority fails closed.

The compatibility test independently decodes the same terminal provider event
before and after retention and requires identical event digest and token
totals. A separate ordinary provider log is still reduced to at most 64 KiB,
so the P1 exception does not disable general D5 compaction.

### P2 compatibility

Experiment comparison now validates every result bundle and requires one
result schema, protocol digest, target source commit, and complete runtime
release identity. Mixed pre-D5 and D5 releases are rejected before text or JSON
comparison output. Existing sealed bundle schema and byte metrics are not
changed.

## Taskpack Execution

Framework taskpack preparation completed as follows:

- blueprint schema validation: passed;
- approval digest validation: passed;
- dry-run materialization: accepted;
- executable materialization: accepted;
- freeze: passed;
- frozen validation: accepted;
- task count: `3`;
- dependency edges: `2`.

The initial dry-run correctly rejected missing blueprint self-inputs for
`COMPAT-01` and `COMPAT-02`. The taskpack and approval digest were repaired,
then materialization and freeze passed. Because the approved contract fixes
`provider_calls` at zero, execution used the frozen taskpack's deterministic
verification command instead of dispatching model workers.

## Verification

- Focused compatibility suite: `28` tests passed.
- Repository-wide suite: `985` tests passed, `4` skipped.
- Artifact lint: passed for all tracked runtime JSON/JSONL artifacts before
  final report publication and rerun after publication.
- `git diff --check`: passed.
- Historical authority diff: clean relative to `30dc381`.

## Conclusion

The original P1 and P2 goals do not require a new live-model run. Historical
evidence remains valid and immutable. New decision-bound executions preserve
P1 acceptance evidence and refuse P2 comparisons across incompatible artifact
accounting authorities.

A future semantic taskpack that intends to use D3-D5 directly must still carry
an explicit `taskpack_decision_contract.v1`; legacy frozen P1/P2 taskpacks are
not silently upgraded.

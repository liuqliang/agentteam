# P0-A Experiment Readiness Guard

Status: implemented and host-verified.

## Purpose

This task closes the governance and fail-closed portion of P0 experiment
readiness before Phase 1 model-invocation usage work begins. In this document,
`P0` means the seven experiment-readiness capabilities required before a
scored pilot. It does not mean research Phase 0, and it is unrelated to the
PRE-00 through PRE-04 execution-preflight task IDs.

P0 is a milestone spanning this P0-A task, Phase 1 usage attribution, and the
later P0-B experiment harness. P0-A must not claim that P0 is complete and
must not implement a live benchmark runner.

## Authority

- Research claims and the seven readiness capabilities remain governed by
  `research/agentteam_research_positioning.md`.
- This plan is implementation authority for the readiness record, minimal
  experiment-manifest contract, CLI inspection, and pilot guard.
- Phase 1 usage semantics remain governed by
  `plans/2026-07-23-phase1-model-invocation-usage.md`.

## Ordered Tasks

```text
P0A-01 terminology and roadmap alignment
  -> P0A-02 machine-readable readiness contract
  -> P0A-03 minimal immutable experiment-manifest schema
  -> P0A-04 fail-closed operator CLI
  -> P0A-05 release and regression verification
```

### P0A-01 Terminology And Roadmap Alignment

**Risk:** L0

Clarify all current-route documentation so that:

- `P0 experiment readiness` names the seven-capability milestone;
- `Phase 0` names research-claim freezing only;
- `PRE-00` through `PRE-04` name Phase 1 execution preflight only;
- Phase 1 is explicitly part of P0 closure rather than work that starts after
  P0 is complete;
- P0-B follows Phase 1 and implements the experiment harness, reset/isolation,
  budgets, operator ledger, and result bundle.

Correct operator help that currently implies real budget-limit enforcement.
Until P0-B exists, `pursue` may claim bounded rounds, timeouts, gates, and
operator stops, but not token/time experiment-budget enforcement.

### P0A-02 Machine-Readable Readiness Contract

**Risk:** L1

Add `p0_experiment_readiness.schema.json` and one runtime-packaged
`p0_experiment_readiness.v1.json` record. The record contains exactly these
capability IDs:

1. `invocation_level_real_usage`
2. `three_mode_experiment_harness`
3. `immutable_experiment_manifest`
4. `clean_reset_and_blind_gold_isolation`
5. `actual_budget_enforcement`
6. `operator_action_ledger`
7. `machine_readable_result_bundle`

Each capability has `missing`, `partial`, or `passed` status, a concise reason,
and bounded evidence references. The initial record must truthfully preserve
the current incomplete state. Runtime validation rejects missing, duplicate,
unknown, or extra capabilities and rejects a record whose declared overall
status disagrees with the capability states.

The packaged record is operational authority for the current runtime release.
It is not a research-claim artifact and cannot promote a research claim.

### P0A-03 Minimal Experiment Manifest Schema

**Risk:** L1

Add `experiment_manifest.schema.json` for future harness input. It fixes:

- experiment and instance IDs;
- repository source, immutable Git commit, and Git object format;
- goal, constraints, and acceptance command;
- one of `single_codex`, `agentteam_direct`, or `agentteam_full`;
- backend, model, sandbox policy, deterministic seed, and blind-gold policy;
- token and wall-time budgets;
- expected usage-contract version;
- the readiness-record digest reviewed before pilot execution.

This task validates manifests only. It does not execute them, reset a
repository, expose a gold patch, or consume a model budget.

### P0A-04 Fail-Closed Operator CLI

**Risk:** L1

Add:

```text
agentteam experiment readiness [--json]
agentteam experiment validate-manifest --manifest <path> [--json]
agentteam experiment check-pilot --manifest <path> [--json]
```

`readiness` validates and reports the runtime-packaged record.
`validate-manifest` validates structure without authorizing execution.
`check-pilot` validates both artifacts and succeeds only when all seven
capabilities are `passed` and the manifest binds the exact canonical readiness
record digest. It otherwise returns a structured blocker list and a nonzero
exit code. No command in P0-A launches a provider or mutates a target
repository.

The reusable pilot-authorization function is the required guard for the future
P0-B runner. That runner must call it before reset, provider launch, or budget
consumption.

### P0A-05 Release And Regression Verification

**Risk:** L1

Ensure local and Git-backed releases package the readiness record and both new
schemas. Add focused tests for:

- the real initial record and schemas;
- exact capability inventory and overall-status reconciliation;
- missing, duplicate, unknown, and extra capability rejection;
- manifest Git OID/object-format consistency;
- manifest validation without pilot authorization;
- pilot rejection while any capability is incomplete;
- pilot acceptance with a temporary all-passed record and matching digest;
- readiness-record and manifest digest tampering;
- concise text and complete JSON CLI output;
- installed-release data availability;
- corrected `pursue` help wording;
- zero provider calls and no target mutation across all three commands.

Run the complete native-runtime test suite, `git diff --check`, and JSON syntax
validation. Install and activate the reviewed release before P1-00.

## Acceptance

P0-A is complete only when:

- the three phase/preflight terms are unambiguous in current authority docs;
- the runtime reports all seven readiness capabilities truthfully;
- the current release reports `pilot_authorized: false`;
- a structurally valid manifest still cannot bypass incomplete readiness;
- a synthetic all-passed test proves the future success path without changing
  the packaged current record;
- no P0-A command launches a provider or changes a target repository;
- releases contain every runtime data/schema dependency;
- focused and full tests pass.

## Stop Conditions

Stop and request operator review if implementation would:

- mark any currently incomplete capability `passed`;
- redefine Phase 1 invocation identity or token-counting semantics;
- add a benchmark runner before P0-B;
- weaken blind-gold isolation, immutable Git binding, or equal-budget rules;
- change an approved research claim.

## Next Step

After P0-A acceptance, execute P1-00 and the Phase 1 usage-attribution
taskpack. Then define and implement P0-B for the remaining experiment harness
capabilities. A scored benchmark pilot remains prohibited until the packaged
readiness record reports all seven capabilities `passed`.

## Implementation Result

- the packaged record reports seven capabilities as `0 passed`, `4 partial`,
  and `3 missing`;
- `agentteam experiment readiness` exposes concise and JSON views;
- manifest validation is independent from pilot authorization;
- public pilot authorization trusts only the runtime-packaged readiness record;
- the current pilot check fails closed with zero provider calls and zero target
  mutations;
- local, Git-backed, and remote-Git release paths retain the schemas and
  readiness data;
- 8 focused P0-A tests and the 620-test native-runtime host suite pass.

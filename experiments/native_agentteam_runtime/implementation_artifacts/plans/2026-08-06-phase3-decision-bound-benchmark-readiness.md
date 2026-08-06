# Phase 3 Decision-Bound Benchmark Readiness

Status: post-D5 implementation contract awaiting deterministic materialization
and freeze. This plan supersedes only the unfinished benchmark-preparation
portion of `2026-07-11-cost-attribution-and-long-run-validation.md`. It does not
rewrite the completed Phase 1 or Phase 2 authorities.

## Direction Decision

Prepare the first standard benchmark pilot through explicit decision authority
before spending live model budget. The initial benchmark remains the
complexity-stratified SWE-EVO subset selected by the research authority.

This taskpack implements the adapter, deterministic selection and
preregistration boundary, and provider-free acceptance checks. It does not run
the scored pilot. A later live-run taskpack requires a frozen instance list,
mode order, budgets, model profile, release identity, and separate operator
authorization.

## Post-D5 Route

The unfinished research route is now:

```text
Phase 3A benchmark adapter and preregistration readiness
  -> Phase 3B separately authorized standard benchmark pilot
  -> Phase 4 mechanism ablations selected from pilot evidence
  -> Phase 5 normal resume and fault-injection recovery study
  -> Phase 6 semantic-feedback long-running task
  -> continue, narrow, or archive decision
```

Each phase receives a new root or child direction decision when its goal or
claim changes. Mechanical tasks inherit an execution decision. Runtime
acceptance decisions are created from evidence and are not pre-authored in a
taskpack.

## Authority Boundary

- The completed P1 usage contract and P2 experiment protocol remain immutable.
- The research positioning document remains authority for claims, benchmark
  choice, fairness, metrics, repetition, and continue/narrow/stop criteria.
- D0-D5 remain authority for decision, Git state, evidence, and report storage.
- This plan may add Phase 3 adapter code, schemas, tests, and a readiness report.
- No provider invocation, benchmark score, source merge, push, or release
  activation is authorized here.

## Task Graph

```text
P3-MAP bounded repository handoff
  -> P3-01 SWE-EVO adapter and deterministic instance selection
      -> P3-02 preregistration and equal-input authority
          -> P3-03 provider-free end-to-end readiness acceptance
```

### P3-MAP Repository Handoff

Locate the existing P2 protocol, workspace, evaluator, mode, budget, result,
usage, and decision-runtime boundaries. Produce a compact handoff; do not
re-summarize historical plans into a new prose trace.

### P3-01 Benchmark Adapter

Implement a narrow SWE-EVO adapter that validates local benchmark metadata,
normalizes instance identity, checks source commit and evaluator references,
and selects a complexity-stratified subset from a declared seed and filtering
rule. Selection must be deterministic and must not inspect gold patches or
mode outcomes.

### P3-02 Preregistration Authority

Define and validate one immutable Phase 3 preregistration artifact binding:

- benchmark and dataset revision;
- ordered instance IDs and selection digest;
- three execution modes and counterbalanced order;
- target/source commits and AgentTeam release identity;
- model, reasoning profile, sandbox, permissions, tools, and cache policy;
- total token, wall-time, operator-input, and repetition budgets;
- mechanical evaluator, partial-score rule, metrics, and decision thresholds.

The artifact must reject post-result mutation and cannot be generated from a
prior mode's run artifacts.

### P3-03 Provider-Free Readiness

Exercise adapter, selection, preregistration, clean snapshots, mode isolation,
budget wiring, result sealing, usage reconciliation, and decision binding with
fixtures only. Publish a machine-readable readiness receipt and concise report.
The receipt must state `live_provider_calls: 0`.

## Acceptance

- the approved blueprint contains `taskpack_decision_contract.v1`;
- every backlog task binds exactly one active execution decision;
- materialization, freeze, run publication, and replay preserve the contract;
- selection is byte-stable for equal dataset revision, filters, and seed;
- a changed instance list or execution condition changes the preregistration
  digest and invalidates the old authorization;
- all three modes receive equal visible inputs and total budgets;
- gold/evaluator-only material remains outside runtime-visible inputs;
- failed, interrupted, and budget-stopped outcomes remain reportable;
- focused tests, artifact lint, and the full repository suite pass;
- no live benchmark result or superiority claim is produced.

## Stop Conditions

Stop and request a new direction decision if implementation would change the
research benchmark, fairness rules, primary metric, repetition policy, or
accepted P1/P2 counting contracts. Stop and request operator authorization
before downloading restricted data, invoking a provider for a scored run, or
freezing a budget based on unavailable price or capacity information.

## Follow-On Taskpacks

Phase 3B is not automatically generated by completion of this taskpack. Its
blueprint must reference the passed readiness receipt and freeze the actual
instance list and live budget. Phase 4 through Phase 6 are intentionally not
pre-expanded into worker tasks: their execution decisions depend on preceding
evidence, while their direction and acceptance rules remain fixed by the
research authority.


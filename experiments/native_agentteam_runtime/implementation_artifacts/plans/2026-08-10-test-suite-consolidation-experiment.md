# Test Suite Consolidation Experiment

Status: completed.

## Decision

AgentTeam will reduce test maintenance cost without reducing behavioral or
authority coverage. Parameterization, shared fixtures, file ownership, and
execution lanes may change. A semantic test scenario may not be removed merely
to reduce method count or wall time.

This experiment is intentionally separate from production runtime behavior.
It must not modify taskpack, scheduler, integration, decision, benchmark, or
provider semantics to make tests easier to satisfy.

## Frozen Baseline

Baseline date: 2026-08-10.

| Measure | Baseline |
|---|---:|
| Test methods | 1,054 |
| Test source lines | 65,859 |
| `test_taskpack.py` methods | 406 |
| `test_m0_runtime.py` methods | 286 |
| `test_experiment_harness.py` methods | 187 |
| Last full-suite wall time | about 188 seconds |
| Environment-only failures | 3 local-socket failures |
| Explicit skips | 5 |

The three largest files contain 879 methods and about 85 percent of test source
lines. Static AST comparison found no byte-equivalent duplicate test bodies;
the primary redundancy is repeated setup and repeated assertions across unit,
scheduler, CLI, and experiment boundaries.

## Invariants

The consolidation must preserve:

1. every invalid schema or taskpack mutation currently exercised;
2. at least one pure contract test and one runtime-boundary test for each
   cross-module authority contract;
3. tamper, recovery, idempotency, permission, Git worktree, integration,
   benchmark, and provider-lifecycle coverage;
4. the distinction between provider-free, fake-provider, host-dependent, and
   live-provider tests;
5. deterministic failure labels that identify the semantic scenario, even
   when several scenarios share one parameterized test method;
6. a green provider-free suite in a restricted workspace sandbox.

## Scenario Accounting

Method count is not the coverage metric. Each table-driven case must have a
stable `case_id`. The experiment records:

- `test_method_count`;
- `semantic_case_count`;
- `host_case_count`;
- `provider_live_case_count`;
- full-suite and fast-lane wall time;
- failures grouped by product regression, environment restriction, and
  unavailable external provider.

A consolidation is rejected if semantic case count falls without an explicit
case-by-case review.

## Execution Lanes

### Fast

Pure functions, schema validation, deterministic state transitions, report
formatting, model routing, and retry decisions. No sockets, systemd, subprocess
sleep, real Git worktrees, or provider invocation.

Target: under 30 seconds in the restricted workspace.

### Integration

Temporary Git repositories, worktrees, scheduler/worker mailboxes, CLI
subprocesses, projection DB rebuilds, release selection, and integration
verification. Fake providers remain the default.

### Host

Tests that require localhost sockets, Unix sockets, systemd, inotify, cgroups,
or host process fencing. These tests remain mandatory in their lane but do not
make the restricted provider-free lane red.

### Live

Explicitly authorized real Codex/provider calls. These are never implied by a
normal local unit-test command.

## Implementation Phases

### Phase A: Pilot

1. parameterize retry-decision and model-routing matrices with stable case IDs;
2. parameterize taskpack validation mutations while retaining every mutation;
3. introduce narrowly scoped shared fixture helpers for JSON, Git repositories,
   taskpacks, scheduler messages, and fake Codex commands;
4. isolate the three known socket-dependent tests behind the host lane;
5. compare semantic scenario inventory before and after the rewrite.

### Phase B: Ownership Split

Split `test_taskpack.py` into taskpack validation, taskpack author, CLI,
release/update, projection DB, notification, and operator-report modules. Split
`test_m0_runtime.py` into scheduler, mailbox worker, runtime adapters,
worktree/integration, and observability modules. Split experiment harness
classes into files without changing their bodies first.

### Phase C: Cross-Layer Deduplication

For each contract, retain the exhaustive field matrix at the lowest deterministic
layer. Higher layers assert only boundary propagation and one representative
failure. Removing a higher-layer assertion requires a documented lower-layer
owner and an integration assertion proving the boundary.

## Stop Conditions

Stop and restore the last accepted test state if:

- a semantic case cannot be mapped to a retained case ID;
- a failure becomes less diagnosable after parameterization;
- host/live requirements leak into the fast lane;
- production code must change solely to accommodate test organization;
- full-suite regressions cannot be attributed to the mechanical rewrite.

## Acceptance

The pilot is accepted when:

1. all baseline semantic cases in the touched areas remain represented;
2. the provider-free fast and integration tests are green;
3. host-only tests are explicitly discoverable and runnable;
4. `git diff --check` and schema validation pass;
5. the final report lists method, semantic-case, line-count, and wall-time
   deltas rather than claiming success from a smaller method count alone.

## Pilot Result

Result date: 2026-08-10.

| Measure | Baseline | Pilot | Delta |
|---|---:|---:|---:|
| Test methods | 1,054 | 1,036 | -18 |
| Test source lines | 65,859 | 65,568 | -291 |
| `test_taskpack.py` methods | 406 | 393 | -13 |
| `test_model_routing.py` methods | 20 | 15 | -5 |
| Touched semantic cases | 30 | 30 | 0 |
| Fast-lane tests | not separated | 97 | new lane |
| Fast-lane wall time | not separated | 1.37 seconds | new baseline |
| Full-suite wall time | about 188 seconds | 236.73 seconds | +48.73 seconds |
| Default skips | 5 | 8 | +3 host cases |

The wall-time result does not establish a speed improvement. The pilot reduced
maintenance repetition and introduced a short feedback lane; full-suite timing
remains dominated by Git, scheduler, subprocess, and experiment-harness tests
and must be compared across repeated runs before claiming a performance gain.

The 30 touched semantic cases are three role/risk routes, six retry decisions,
ten unsafe write scopes, five invalid top-level taskpack fields, and six invalid
backlog field types. Every case has a stable `case_id` and all remain covered.

The full provider-free suite passed in a standard Git worktree:

```text
Ran 1036 tests in 236.395s
OK (skipped=8)
```

The three additional skips are the previously environment-failing localhost
and Unix-socket cases. They are discoverable through
`scripts/test-native-runtime.sh host` and are not deleted or weakened.

The pilot established executable ownership lanes before the Phase B physical
split so that file movement could be verified independently from
parameterization.

## Phase B Compatibility Discovery

Historical Phase 2 contracts and the gate capability registry use fully
qualified IDs under `tests.test_experiment_harness`. Those IDs are authority
references, so a direct module rename would invalidate frozen evidence even if
the assertions remained identical.

Phase B therefore starts with a compatibility-preserving discovery layer:

| Discovery owner | Case classes | Test cases |
|---|---:|---:|
| experiment budget and operator actions | 4 | 80 |
| experiment results and schemas | 3 | 32 |
| experiment runtime and sandbox | 4 | 57 |
| experiment calibration and gates | 2 | 18 |
| Total | 13 | 187 |

The original module remains the canonical location of historical IDs and still
runs all 187 cases when invoked as a module. Domain suites are deliberately not
named `test_*.py`, so broad discovery does not execute them a second time. An
ownership invariant test rejects unassigned and multiply assigned harness
classes. Explicit historical IDs remain executable. This is an ownership split,
not a class-body migration: the capability registry binds those test IDs to the
canonical source file, so moving the bodies would require a production authority
migration solely for test organization and violates this experiment's stop
conditions.

Retry decisions have been physically moved from `test_model_routing.py` to
`test_retry_decision.py`. This smaller boundary had no historical gate-ID
dependency and validates the intended end-state without a compatibility shim.

The compatibility discovery change passed the provider-free full suite in a
standard Git worktree before the ownership invariant was added:

```text
Ran 1036 tests in 210.804s
OK (skipped=8)
```

## Phase B Monolith Split

`M0RuntimeTests` now uses 12 physical domain mixins covering adapters, core
runtime, grounding, integration, mailbox, notifications, observability,
planning, reports, retry, scheduler, and workers. Its compatibility module
contains no direct test methods and preserves all 286 inherited test IDs.

`TaskpackTests` now uses 14 physical domain mixins covering authoring,
blueprints, CLI, contracts, controllers, core behavior, governance,
notifications, projections, pursue, readiness, releases, reporting, and
validation. Its compatibility module contains no direct test methods and
preserves all 393 inherited test IDs.

The support modules own the original imports and shared fixtures. Structural
tests reject a method that has no mixin owner, has multiple owners, or moves
back into either compatibility class. The largest former files changed from
23,722 and 15,585 lines to small compatibility entrypoints; physical case files
are bounded by domain, with no exact duplicate test-method bodies.

Test identity comparisons normalize the invocation prefix because `unittest`
legitimately reports the same class as `tests.test_*`, `test_*`, or
`experiments.native_agentteam_runtime...test_*` depending on how the module was
loaded. The stable identity is the suffix beginning at the canonical
`test_<module>.<class>.<method>` path.

## Final Acceptance Result

Result date: 2026-08-10.

| Measure | Initial baseline | Final | Delta |
|---|---:|---:|---:|
| Test methods | 1,054 | 1,039 | -15 |
| Test source lines | 65,859 | 66,640 | +781 |
| Preserved normalized pre-split IDs | 1,037 | 1,037 | 0 missing |
| Structural ownership tests | 0 | 2 | +2 |
| Exact duplicate test bodies | 0 groups | 0 groups | unchanged |
| Fast lane | not separated | 99 tests / 1.35s | new lane |
| Integration lane | not separated | 701 tests / 88.46s | new lane |
| Experiment lane | not separated | 210 tests / 87.01s | new lane |
| Full suite | about 188s | 1,039 tests / 181.86s | -6.14s observed |
| Full-suite skips | 5 | 8 | +3 explicit host cases |

The source-line increase is accepted: imports and compatibility entrypoints are
now explicit in each physical ownership module. The goal was reduced ownership
and fixture complexity, not compressed source bytes. The single observed
full-suite timing does not justify a performance claim, but it rules out a
material regression in this environment.

The three additional skips are retained host-lane tests, not removed coverage.
The restricted workspace did not run the host lane because it cannot provide
the required localhost and Unix sockets. `scripts/test-native-runtime.sh host`
remains the explicit host command.

No cross-layer test was removed after the AST audit found no exact duplicate
bodies. Higher-layer scenarios differ in scheduler, CLI, Git, recovery, or
authority boundaries and remain useful. Future consolidation must identify a
specific lower-layer owner and boundary propagation assertion before deleting
one of them.

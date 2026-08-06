# Phase 1/Phase 2 Decision-Artifact Compatibility Audit

Status: operator-authorized compatibility taskpack. Historical Phase 1 and
Phase 2 plans, blueprints, reviews, reports, calibration artifacts, frozen
taskpacks, and run evidence are read-only inputs. This taskpack may add new
code, tests, and audit records; it must not rewrite those authorities.

## Goal

Prove that the completed Phase 1 model-usage and Phase 2 experiment-harness
goals remain valid after D5 decision-centric artifact reduction. Repair only
cross-boundary defects that affect a new decision-bound execution. Do not rerun
live calibration when deterministic compatibility evidence is sufficient.

## Historical Authority Boundary

- Phase 1 invocation starts, terminal usage records, accepted live-smoke
  evidence, final report, and approval retain their original schemas and
  digests.
- Phase 2 protocols, manifests, release identities, sealed result bundles,
  calibration evidence, and promotion records retain their original schemas
  and digests.
- Existing taskpacks continue through the legacy runtime path because they do
  not contain `taskpack_decision_contract.v1`.
- `agentteam artifacts migrate-legacy` may append decision indexes; it may not
  edit a historical authority file or invent missing rationale.

## Audit Findings

### C1: Phase 1 accepted raw spool

The passed `model_invocation_live_smoke.v1` artifact names a bounded raw spool
and binds the terminal provider-event digest used for independent token
decoding. Ordinary D5 log retention may keep only a 64 KiB complete JSONL tail,
but a spool referenced by passed acceptance evidence must remain complete and
bounded by the existing 1 MiB Phase 1 limit.

Required repair:

- detect only the fixed passed Phase 1 acceptance artifact;
- reject unsafe, missing, symlinked, or oversized spool paths;
- link the immutable acceptance JSON to the root decision as `evidence`;
- retain its referenced spool without truncation;
- continue truncating unrelated provider logs;
- prove the independent decoder returns identical event digest and totals
  before and after terminal retention.

### C2: Phase 2 comparison authority

`artifact_bytes_written` and `raw_spool_bytes_written` are meaningful only
inside one experiment authority. Mixing D5 and pre-D5 runtime releases under
one comparison can make artifact cost look comparable when its production and
retention policy differs.

Required repair:

- validate every result bundle before comparison;
- require identical result schema, protocol digest, target source commit, and
  complete runtime release identity;
- apply the same check to text and JSON CLI output;
- reject mixed authority instead of silently rendering it;
- leave sealed result bundles and their schemas unchanged.

### C3: No full rerun required

The P1 accepted spool lives below its acceptance run and P2 sealed authorities
live below experiment-owned roots. D5 does not rewrite historical runs. A
deterministic compatibility taskpack can therefore establish the required
cross-boundary properties without another provider call or live calibration.

## Task Graph

```text
COMPAT-01 P1 evidence-aware raw-spool retention
COMPAT-02 P2 comparison authority fence
  -> COMPAT-03 focused and repository-wide compatibility verification
```

## Acceptance

- no tracked historical P1/P2 authority document or report changes;
- a passed P1 acceptance artifact becomes decision-linked evidence;
- its spool remains byte-identical and independently decodable;
- an unrelated oversized provider log is compacted;
- P2 comparison accepts one protocol/source/release family;
- P2 comparison rejects a mixed runtime release before rendering JSON or text;
- D0-D5 focused tests, P1 end-to-end tests, P2 result-bundle tests, projection
  tests, artifact lint, and the full repository suite pass;
- no live provider invocation is made by this compatibility taskpack.

## Stop Conditions

Stop and request a new architecture decision if compatibility requires
mutating a sealed P1/P2 authority, changing an accepted token total, changing a
sealed experiment result schema in place, or comparing results produced by
different runtime release identities.

## Verification

Run from `experiments/native_agentteam_runtime/m0_runtime`:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.test_decision_artifact_lifecycle \
  tests.test_phase1_usage_end_to_end \
  tests.test_experiment_harness.ExperimentResultBundleTests \
  tests.test_projection_db_operator_paths
```

Then run the complete repository suite and artifact lint. The compatibility
taskpack has `provider_calls: 0`; historical live evidence is read, not
regenerated.

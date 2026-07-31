# Phase 2 Calibration Identity Recovery

Status: identity and late-binding implementation complete; replacement
candidate authority and promotion taskpack pending

## Problem

The first P2-07A contract used one `source_commit` for both the repository
under evaluation and the AgentTeam runtime release. Deterministic fixtures
run in a separate repository, so that model cannot truthfully bind their
report to the reviewed AgentTeam candidate commit required by P2-08.

## Decision

Use two independent identities:

- `target_source_commit` binds the evaluated repository and equal-input
  protocol.
- `runtime_source_commit` plus the full `runtime_release_identity` bind the
  AgentTeam code that executed the experiment.

The report remains derived from sealed run bundles. P2-08 validates the
runtime identity against its reviewed candidate commit and leaves the target
identity independent.

## Implementation

1. Upgrade deterministic calibration reports to
   `phase2_deterministic_calibration.v2`.
2. Recompute both identities from the sealed run family.
3. Add `agentteam experiment calibrate-deterministic`, driven by a bounded
   request JSON that names existing sealed runs and fixture roots.
4. Make the command publish canonical create-if-absent evidence without
   invoking a provider.
5. Update the P2-08 validator to bind `runtime_source_commit`, not the target
   repository commit.
6. Bind every controlled terminal-outcome run to the same target and runtime
   identities as the primary and repeat runs.
7. Require the request and all referenced paths to stay inside explicit
   authority roots, bind the request digest into the report, and validate the
   installed runtime release manifest during P2-08.
8. Package a one-shot deterministic driver that must execute from the
   candidate release and creates the persistent sealed run family, request,
   and report without provider calls.
9. Require a Git-installed candidate and recompute every tracked release file
   mode and blob OID against the declared source commit before P2-08 accepts
   the calibration.
10. Keep candidate runtime identity independent from the later governance
    commit by allowing controller-only blueprints to resolve approved
    authority paths at materialization. The resulting taskpack and
    materialization manifest retain digest-bound in-taskpack snapshots, and
    freeze rejects any intervening source drift.

## Acceptance

- Target and runtime commits may differ and both remain validated.
- Every primary, repeat, and controlled run shares the same protocol and
  runtime release identity.
- A forged runtime commit, release identity, or identity digest is rejected.
- Runtime identity drift in a controlled terminal-outcome run is rejected.
- The request entrypoint cannot create runs or invoke a provider.
- The packaged driver can create persistent deterministic runs only in a new
  empty authority root and only for its own installed release.
- P2-08 rejects a changed, missing, or extra tracked release file and rejects
  a calibration request whose retained bytes differ from the report binding.
- Existing Phase 2 readiness, live-calibration, and finalization tests pass.
- The complete runtime test suite passes before a new candidate release is
  installed.
- A tracked blueprint can late-bind authority below `project_root` or the
  profile's `project_work_root`, while the frozen taskpack contains no
  unresolved declaration.
- Authority drift between materialization and freeze is rejected.

## Boundaries

This recovery does not execute P2-08, authorize P2-09, call a live provider,
merge to `main`, push, or activate a post-promotion release.

The control-plane Linux account is trusted while materialization, freeze, and
P2-08 execute. The calibration closure snapshot plus pre/post tree-digest
checks cover ordinary drift, accidental mutation, missing or additional files,
unsafe paths and symlinks, stale digests, and source changes that remain
observable at either check. They do not claim protection from a malicious
same-account process that swaps authority content during recomputation and
restores the original byte-identical tree before the post-check. That actor
requires a privileged immutable authority store or mount boundary and is out of
scope for this contract.

P2-08 intentionally recomputes against the persistent authority roots rather
than a relocated copy. Existing sealed-run attestations bind absolute paths,
device IDs, and inode identity, so relocation invalidates their historical
evidence. The frozen taskpack still carries a complete closure snapshot and
regular-file inventory digest for each root and rejects source/snapshot overlap
before copying. The inventory binds path, byte size, and content SHA-256,
rejects symlinks and special files, and excludes empty directories and
permission bits so freeze and read-only direct execution preserve the same
semantic identity.

## Verification

- Focused calibration, Phase 2 gate, readiness CLI, controller-only taskpack,
  and launcher tests passed.
- The complete native runtime suite passed 934 tests with 3 skips.
- Final independent review reported no major or blocking defects.

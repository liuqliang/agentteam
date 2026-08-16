# Phase 3 provider-free runtime refreeze

Status: implemented and provider-free verified

## Goal

Freeze the accepted controller-verification and candidate-test-separation
implementation into a new inactive runtime release and mechanically rebuild
all runtime-dependent Requests preparation authorities. Do not launch a model,
score a mode, publish live authorization, activate a release, or claim cost or
quality improvement.

## Inputs

- Source commit: `9f0be43c7238e6de2e1a91093995aa29bc5f1289`.
- Parent decision: `DEC-P3-controller-verification-handoff-v1` revision 2.
- Retained semantic/data/environment authority: Requests preparation bundle
  `0431260bc6618402c72b965455e197c951d90528793c78d451c785c41d26d416`.
- Existing `12/16` tool boundary and resource envelope remain unchanged.

Requests is used only as a retained authority carrier for deterministic
rebuilding. This task does not repeat its provider execution.

## Required rebuild

1. Install an isolated release from the exact clean commit without activation.
2. Rebind the runtime commit in shared visible input.
3. Recompute the direct taskpack, preregistration, pilot contract, calibration
   contract, experiment protocol, and frozen runtime taskpack.
4. Preserve source, selection, public environment, evaluator environment,
   resource envelope, and provider-free public verification authority.
5. Bind a new runtime-policy preflight containing the candidate policy,
   controller verification conditions, mode visibility, release file digests,
   and commit-level test evidence.
6. Publish one digest-bound preparation bundle with
   `live_authorization=not_authorized`.

## Acceptance

- The release manifest binds the exact source commit and no active release.
- Release and checkout copies of all changed runtime/schema files have equal
  SHA-256 digests and import successfully from the release.
- Formal validators accept all 15 bundle artifacts and the frozen taskpack.
- Single, direct, and full mode inputs expose the same candidate policy.
- The Codex CLI accepts the exact bounded context and hook configuration in a
  provider-free `features list` probe.
- Release-level controller-handoff and candidate-split smokes pass.
- No live epoch, live authorization, provider transcript, usage artifact, or
  scored execution exists in the bundle.
- Repository tests, artifact lint, and decision-chain validation pass.

## Boundary

This refreeze proves preparation integrity only. A new SWE-EVO instance or
Requests repetition requires a separate execution decision with an explicit
token ceiling and expected information gain.

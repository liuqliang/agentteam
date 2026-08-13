# Phase 3B provider-free resource preflight v7

- Decision: `DEC-P3B-v7-provider-free-resource-preflight`
- Status: approved for provider-free implementation and verification
- Risk: `L2`
- Live boundary: no provider invocation and no `P3-LIVE` authorization

## Problem

The existing Phase 3B aggregate receipt proves static authority consistency and
zero provider calls. It does not execute the v2 resource hierarchy on the
current host. Unit tests and one operator preflight validate components, but no
single immutable artifact reconciles all three mode slices, required leaf and
control scopes, project/mode aggregate counters, and final cleanup.

## Decision

Add a provider-free resource preflight runner and versioned receipt. One
`Phase3PilotResourceOwner` owns the project and three mode slices for the
complete probe. Each mode runs deterministic local commands only:

- `single_codex`: workload leaf and evaluator leaf;
- `agentteam_direct`: control-plane, workload, and evaluator leaves;
- `agentteam_full`: control-plane, workload, and evaluator leaves.

Every probe must use the production owner-reference, transient-service,
monitor, readback, evidence, and cleanup paths. No Codex command, credential,
provider adapter, model invocation lifecycle, or experiment mode adapter may
be called.

Before cleanup, capture project and all mode aggregate evidence. Cleanup runs
in `finally` and must report every mode and the project drained or removed.
The resulting receipt binds the resource envelope digest, pilot ID, exact
probe inventory, leaf evidence summaries, aggregate evidence, cleanup result,
and explicit zero-provider counters.

## Acceptance

- Deterministic tests cover all eight probes and reject missing, duplicated,
  escaped, failed, or incomplete evidence.
- A real user-systemd preflight passes on this host without provider access.
- The Phase 3 provider-free receipt v2 binds the resource-preflight receipt;
  historical v1 receipts remain valid and unchanged.
- Focused tests and `scripts/test-native-runtime.sh integration` pass.
- No scored call, merge, push, or release activation occurs.

## Stop Conditions

Stop before live authorization if any transient service escapes its mode
slice, any resource property cannot be read back, any required evidence is
missing, cleanup is incomplete, or any provider/model invocation artifact is
created.

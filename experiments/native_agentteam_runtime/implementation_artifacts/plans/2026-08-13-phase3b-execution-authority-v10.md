# Phase 3B execution authority v10

- Decision: `DEC-P3B-multi-instance-pilot`
- Evidence level: `L2`
- Status: approved for provider-free implementation
- Live boundary: no provider invocation, evaluator execution, or live permit

## Decision

Add one strict execution-authority object which preserves the concrete values
behind every digest referenced by the benchmark preregistration. It must bind:

- the exact AgentTeam release commit, Codex CLI version, and named runtime
  environment;
- the approved model and reasoning profile plus the complete service
  configuration;
- worker tools, sandbox, permission, external-service, benchmark-visible-test,
  termination, and shared-cache policies;
- the approved common workload resource envelope;
- the frozen v9 instance candidate bundle.

The execution profile used by all three modes and all three instances must be
derived mechanically from this authority. Callers may not supply standalone
digest strings.

## Two-stage release binding

The authority implementation and tests are committed first. That commit is the
runtime release bound by the production authority in a later commit. This
avoids a self-referential Git commit digest while allowing the authority commit
to supervise execution from the exact earlier runtime release.

## Acceptance

- Every preregistration digest can be recomputed from a persisted concrete
  policy object.
- Model, reasoning, resource, candidate, and release drift fail closed.
- Unknown fields, malformed digests, symlink substitution, and immutable
  publication conflicts fail closed.
- Execution-profile derivation is deterministic and contains no caller-owned
  digest values.
- Existing Phase 3 and provider-free integration tests continue to pass.

## Boundary

This task does not yet publish the production authority, direct taskpacks,
preregistrations, aggregate pilot contract, live authorization, or scored run.

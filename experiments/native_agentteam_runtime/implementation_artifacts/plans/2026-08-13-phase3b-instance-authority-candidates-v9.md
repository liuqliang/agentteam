# Phase 3B instance authority candidates v9

- Decision: `DEC-P3B-multi-instance-pilot`
- Evidence level: `L2`
- Status: approved for provider-free candidate projection
- Live boundary: no provider invocation, image pull, evaluator run, or taskpack freeze

## Decision

Project the three selected official Arrow rows into a versioned candidate
bundle with a strict visibility split:

- worker-visible: repository source, base commit and tree, public problem
  statement, fixed constraints/non-goals, and the benchmark's general test
  command as argv;
- evaluator-only: environment setup commit and tree, image reference and remote
  manifest digest, benchmark family, log parser, and SHA-256 bindings for gold
  patch/test/test-ID fields without their content.

Repository commits and trees must be verified from the official Git remotes.
Evaluator image manifests must be queried remotely without pulling image layers.
The Arrow reader must select an exact allowlist before converting values to
Python and must never return patch, test patch, or test IDs to worker-visible
construction code.

## Acceptance

- The selection-freeze v8 bundle replays before candidate construction.
- Exactly the three selected instance IDs are projected in selection order.
- Official dataset hash, base commits, environment commits, Git trees, image
  references, image manifest digests, OS, and architecture are exact.
- Problem statements and public test commands bind the worker-visible input.
- Gold content is absent from every published file; only canonical digests are
  retained under evaluator-only authority.
- Mutation, missing coverage, duplicate rows, unknown fields, symlinked bundle
  roots, and publication conflicts fail closed.
- The completion receipt states that final preregistration remains blocked on
  the final runtime release and execution-policy digests.

## Boundary

This task does not pull or execute evaluator images, check out full source
trees, author or freeze direct taskpacks, publish preregistrations, authorize
`P3-LIVE`, invoke a provider, merge, push, or activate a release.

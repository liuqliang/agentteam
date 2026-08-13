# Phase 3B instance authority candidates v9

- Decision: `DEC-P3B-multi-instance-pilot`
- Evidence level: `L2`
- Status: candidate authorities frozen
- Live status: `P3-LIVE` remains unauthorized

## Result

The production projection read the three selected rows from the fixed SWE-EVO
Arrow artifact at source commit
`9b83d5af943ba7a17567336f5b18239f73960219`. The artifact SHA-256 remains
`74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520`.
The reader selected an exact column allowlist before converting the three rows
to Python values.

The immutable candidate bundle is under
`implementation_artifacts/acceptance/phase3b-instance-authority-candidates-v9/`.
It binds, in selection order:

1. `psf__requests_v2.4.0_v2.4.1`: candidate
   `8d3ac88fafef8dce19dd32a1d587c3f5a0a640d2b5e2a54c476e9bca76790d19`;
2. `dask__dask_2023.3.2_2023.4.0`: candidate
   `d8dcbb3dc2ef489aeff3cb83574b11f8da95b0a616b6dade54190f567a4588e2`;
3. `iterative__dvc_2.19.0_2.20.0`: candidate
   `7a926817a5c6e086731bbb6d2b106b3a45fa1908311fc8ff4f38a535647a57bd`.

The aggregate candidate digest is
`f60c537a478aa43f694134b1e22ccc31479d8534aad4683473ec532849bea178`.
The receipt digest is
`237c4d416253b3561a616a13d6cf18f244d8f856f8a94d09cfdf811589e14d8b`.

Each worker-visible authority contains only the repository commit/tree, public
problem statement, fixed constraints/non-goals, and general benchmark command.
Evaluator-only state contains environment and image bindings plus canonical
SHA-256 values for gold data. A content scan found no gold patches, test
patches, or sentinel values in either published file.

## Verification

Production replay against the real Arrow file reproduced the approved bundle.
A second immutable publication returned `created=false` for both files, and the
committed loader replayed the candidate-to-receipt binding.

Focused preparation tests:

```text
Ran 46 tests in 2.083s
OK
```

Related Phase 3 regression:

```text
Ran 84 tests in 2.529s
OK
```

Provider-free integration lane after repairing the stale monolith test-count
constant:

```text
Ran 1124 tests in 170.010s
OK (skipped=8)
```

The ownership guard now checks its structural invariants instead of rejecting
legitimate additions whenever the total test count changes. Artifact lint
checked 112 JSON files with zero errors. `git diff --check` and Python bytecode
compilation also pass.

## Boundary

These are reviewed candidate inputs, not final preregistrations or executable
taskpacks. Final preregistration remains blocked on the exact runtime release,
Codex CLI/environment identity, service configuration, tools, sandbox,
permission, external-service, visible-test, termination, and shared-cache
authorities. No provider was called, no evaluator image was pulled, no selected
task was executed, and no live permit was issued.

# Phase 3B selection freeze v8

- Decision: `DEC-P3B-multi-instance-pilot`
- Evidence level: `L3`
- Status: selection authority frozen
- Live status: `P3-LIVE` remains unauthorized

## Result

The production trusted projection read the fixed SWE-EVO Arrow artifact at
source commit `9b83d5af943ba7a17567336f5b18239f73960219`. Its artifact SHA-256 is
`74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520`.
It reproduced 48 source rows, the approved high/low/medium populations of
15/21/12, and eligible populations of 15/20/12 after excluding the previous
calibration instance.

The deterministic selector froze, in order:

1. `psf__requests_v2.4.0_v2.4.1` (`high`);
2. `dask__dask_2023.3.2_2023.4.0` (`low`);
3. `iterative__dvc_2.19.0_2.20.0` (`medium`).

The immutable bundle is under
`implementation_artifacts/acceptance/phase3b-selection-freeze-v8/` and binds:

- decision input: `c0bf2d520e24b94adf0650e3d3dca7563b4088b320d1a4dfccbb3d229fb2a094`;
- complexity projection: `f103e61548af36b41ad5b32f6e05941827ab52ace24ec3ce4c1663ca01cb88e1`;
- routing manifest: `ce7e368c34d2be326342134e6ad9c8bf6126b75b50f065bc2a309819c1def295`;
- selection: `9103915631132adb25542da5df655802ee3b2fe974a3d5808fd62630619bbc77`;
- selection authority: `4eef7bccd83662b27b2f359189ccc59943a494eb8af05de58f0bf8f29a64f09a`;
- freeze receipt: `c7007aea7c80de97f07d1eb250bc082f8bb39f0d9336abf3db85a7a8885b7d73`.

These are canonical JSON digests. Whole-file `sha256sum` values differ because
immutable publication appends one newline to the canonical payload.

## Verification

The production path was executed with `pyarrow==21.0.0` in an isolated
provider-free evaluator environment. A second publication reported
`created=false` for all five artifacts, and `load_phase3_selection_freeze_bundle()`
replayed the complete chain.

Focused preparation tests:

```text
Ran 38 tests in 1.819s
OK
```

The tests cover complete bundle replay, idempotent publication, proposal drift,
conflicting authorities, routing drift, symlink substitution, rehashed decision
drift, and the approved model/budget/selection contract.

Related Phase 3 authority regression:

```text
Ran 92 tests in 2.116s
OK
```

Provider-free integration lane:

```text
Ran 707 tests in 78.229s
OK (skipped=2)
```

Artifact lint checked `186` JSON files and `1` JSONL file with zero errors.
`git diff --check` and Python bytecode compilation also pass.

## Boundary

P3B-01A and P3B-01C now have persisted production authority. P3B-02 remains
open because its exact target commits, evaluator images, worker-visible goals,
acceptance commands, direct taskpacks, and preregistrations require per-instance
construction and review. No benchmark task, provider, model, or evaluator was
executed and no live permit was issued.

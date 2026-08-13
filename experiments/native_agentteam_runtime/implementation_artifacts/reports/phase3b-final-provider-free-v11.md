# Phase 3B final provider-free freeze v11

- Decision: `DEC-P3B-multi-instance-pilot`
- Evidence level: `L3`
- Status: P3B-02 and provider-free P3B-03 complete
- Live status: `P3-LIVE` remains unauthorized

## Result

The complete three-instance authority chain is now persisted and replayable.
No caller-owned digest placeholders remain in the preregistration execution
profile. Concrete service, tool, sandbox, permission, external-service,
visible-test, termination, shared-cache, and resource policies are stored in
`implementation_artifacts/acceptance/phase3b-execution-authority-v10/`.

The execution authority binds:

- AgentTeam runtime release
  `f666eaceeabb321db31ff2374a55368fcb62c6cd`;
- Codex CLI `codex-cli 0.147.0`;
- environment `ubuntu-24.04.4-linux-x86_64-cgroup-v2-v1`;
- model `gpt-5.6-sol`, reasoning profile `high`;
- candidate bundle
  `f60c537a478aa43f694134b1e22ccc31479d8534aad4683473ec532849bea178`;
- execution authority
  `d28f853c02655f92f52b2733476f78acc579c67131b8544eea612fda7aae0748`.

The final immutable bundle is under
`implementation_artifacts/acceptance/phase3b-final-provider-free-v11/`. Its
bindings are:

- instance materialization:
  `748a39478eedb79554041ffbe327102063553d0cf04fb65c56407f1bd11adf51`;
- pilot contract:
  `8d597abb61fab3e0b551d6e1a94c8f833f36714396cdf805aa49ab818881e46b`;
- resource preflight:
  `70b31ac337232d19227133c64eb85cb3c1e75f125e4a5deb7296c955cd3d5eb6`;
- aggregate provider-free preflight:
  `557c1f41a0b4890b3aba0c071e93dc6ddd7391b8c8efa8829ea74ce9aeff64dd`;
- final chain receipt:
  `95de138f21e361a017ed9b6c763a116314fb8bf8b20a6c577d072e23d80376e9`.

## Per-instance freeze

| Instance | Direct taskpack | Preregistration |
| --- | --- | --- |
| `psf__requests_v2.4.0_v2.4.1` | `382d7d8c36b42ed2444b48433857faeaf224e5341265a8ceba06c32083364291` | `f73f453692cc2da22cd1b789933bcbb7fdd2b5b2241a20dd45275a8db7609536` |
| `dask__dask_2023.3.2_2023.4.0` | `3c65bb6b7d7c4b0ca32590a7bf3eefec633457cbe68aeb3d2dd0a673f63618d4` | `b370037485929da65b3321d51c998d45d32932bfd0429776ef3ab969c12d0685` |
| `iterative__dvc_2.19.0_2.20.0` | `09139cc794f8ffac3f55d66a133cc8a3c785e4e9e281cc4bd9e26669c44d6200` | `40253954107555881202ec54f82b6797da4f35b73c985f159541a748878e77a8` |

Every instance binds the same runtime, model, execution policies, per-mode
budget, and counterbalanced mode order. Each mode receives the same public
task input for that instance. The final bundle contains no gold patch or test
content.

## Budget and gate

The aggregate worst-case ceiling is 40,500,000 tokens and 48,600 provider wall
seconds with one inflight model invocation. This covers three instances, three
modes, and up to three repetitions. It is a hard authorization ceiling, not an
expected spend.

The aggregate receipt records zero provider calls and zero scored mode
executions. Its live status is `not_authorized`; a separate epoch-bound
operator decision is still required before any scored call.

## Verification

The approved final builder mechanically reproduced byte-identical committed
materialization, pilot, and preflight objects from the persisted authorities.
The loader replayed the complete candidate -> execution -> selection ->
materialization -> pilot -> resource -> preflight -> final receipt chain.

Focused preparation tests:

```text
Ran 56 tests in 2.419s
OK
```

Related Phase 3 regression:

```text
Ran 94 tests in 2.902s
OK
```

Provider-free integration lane:

```text
Ran 1134 tests in 170.088s
OK (skipped=8)
```

A gold-content scan over the final bundle passed. A second publication
reported `created=false` for all four final artifacts. Artifact lint checked
118 JSON files with zero errors. Python bytecode compilation and
`git diff --check` also pass.

## Boundary

This freeze completes provider-free preparation only. It does not pull or run
the official evaluator images, invoke Codex for a scored task, issue a
`phase3_live_authorization.v1`, merge source, push, or activate a release.

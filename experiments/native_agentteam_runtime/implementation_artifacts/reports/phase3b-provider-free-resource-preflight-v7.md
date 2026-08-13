# Phase 3B provider-free resource preflight v7

- Decision: `DEC-P3B-v7-provider-free-resource-preflight`
- Evidence level: `L2`
- Status: passed
- Live status: `P3-LIVE` remains unauthorized

## Result

The production provider-free resource runner created one pilot project owner,
three mode slices, and the fixed eight-probe inventory:

| Mode | Probes |
| --- | --- |
| `single_codex` | workload, evaluator |
| `agentteam_direct` | control plane, workload, evaluator |
| `agentteam_full` | control plane, workload, evaluator |

All probes executed `/usr/bin/true` through the production transient-service,
owner-reference, readback, monitor, acknowledgement, and cleanup paths. No
Codex command, provider adapter, model invocation, or scored mode execution was
created.

The immutable acceptance artifact is
`implementation_artifacts/acceptance/phase3b-provider-free-resource-preflight-v7/receipt.json`.
Its bindings are:

- resource preflight receipt digest:
  `70b31ac337232d19227133c64eb85cb3c1e75f125e4a5deb7296c955cd3d5eb6`;
- complete canonical artifact digest:
  `24d2124808d26586905bfd25893d44775c7ff25605d176dd57ea476378e7f18f`;
- probe count: `8`;
- mode aggregate count: `3`;
- cleanup complete: `true`;
- provider calls: `0`;
- model invocations: `0`;
- scored mode executions: `0`.

The receipt contains raw cgroup-v2 CPU, memory, PID, throttling, OOM, identity,
and cleanup evidence for leaf, mode, and project scopes.

## Aggregate receipt compatibility

`build_phase3_provider_free_preflight_receipt()` remains byte-compatible with
historical v1 callers. When supplied a validated dynamic resource receipt it
now emits `phase3_pilot_preflight.v2`, binding the resource envelope and
resource-preflight receipt digests plus a `resource_hierarchy` verification
check. Tests reject receipt drift and preserve all v1 tests.

The repository does not yet contain the complete frozen P3B-02 selection,
materialization, and per-instance preregistration JSON artifacts. Therefore no
formal aggregate v2 acceptance receipt was fabricated from test fixtures. It
must be generated after P3B-02 persists those authorities.

## Verification

Focused Phase 3 preparation and resource tests:

```text
Ran 51 tests in 1.588s
OK
```

The focused suite covers the fixed eight-probe inventory, zero-provider
reconciliation, cleanup on probe failure, dynamic receipt mutation, aggregate
v2 binding, v2 drift rejection, nested evidence validation, malformed cleanup
rejection, and historical v1 compatibility.

Related resource/model/experiment regression:

```text
Ran 243 tests in 82.082s
OK (skipped=6)
```

Provider-free integration lane:

```text
Ran 707 tests in 78.448s
OK (skipped=2)
```

`git diff --check` also passes.

## Boundary

This closes the dynamic resource portion of provider-free P3B-03. It does not
freeze P3B-02 instance authorities, execute any selected benchmark task,
authorize P3-LIVE, merge source, push, or activate a release. The next work is
to persist and review the three selected instance authorities and direct-mode
taskpacks, then generate the formal aggregate v2 preflight receipt from those
authorities and this dynamic resource receipt.

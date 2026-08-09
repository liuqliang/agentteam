# Phase 3 Generic Gate Registry

## Decision

Extend the existing deterministic gate registry beyond the Phase 2 controller
chain without changing the frozen Phase 2 topology or historical taskpacks.
Register `P3-READY` as a provider-free relation gate whose acceptance is based
on production validation of the retained readiness receipt.

## Scope

1. Move Phase 3 readiness receipt construction, digest binding, publication,
   and semantic validation from test helpers into runtime code.
2. Add `P3-READY` to the gate registry with no controller action input and no
   provider authorization.
3. Preserve the exact `P2-08 -> P2-09 -> P2-10` controller-only graph.
4. Build relation context for non-Phase-2 registered gates from trusted gate
   epoch state, while retaining Phase 2 sidecar compatibility.
5. Reject a readiness receipt when mode bindings diverge, canonical digests do
   not match, or provider invocation artifacts exist in the evidence run.

## Non-goals

- Do not rewrite or mutate frozen Phase 1, Phase 2, or Phase 3A taskpacks.
- Do not authorize or execute a scored benchmark run.
- Do not weaken operator review or merge gates.
- Do not introduce a second gate execution framework.

## Acceptance

- Focused Phase 3 readiness and gate registry tests pass.
- Existing Phase 2 controller tests remain unchanged and pass.
- The full native runtime test suite passes.
- JSON/JSONL artifact lint passes.

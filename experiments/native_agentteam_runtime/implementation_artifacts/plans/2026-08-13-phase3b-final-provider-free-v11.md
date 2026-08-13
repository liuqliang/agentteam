# Phase 3B final provider-free freeze v11

- Decision: `DEC-P3B-multi-instance-pilot`
- Evidence level: `L3`
- Status: approved for provider-free finalization
- Live boundary: `P3-LIVE` remains closed

## Inputs

- selection authority:
  `4eef7bccd83662b27b2f359189ccc59943a494eb8af05de58f0bf8f29a64f09a`;
- candidate bundle:
  `f60c537a478aa43f694134b1e22ccc31479d8534aad4683473ec532849bea178`;
- execution authority:
  `d28f853c02655f92f52b2733476f78acc579c67131b8544eea612fda7aae0748`;
- runtime release: `f666eaceeabb321db31ff2374a55368fcb62c6cd`;
- resource preflight receipt:
  `70b31ac337232d19227133c64eb85cb3c1e75f125e4a5deb7296c955cd3d5eb6`.

## Decision

Mechanically derive the shared execution profile from the execution authority,
then build exactly one visible input, frozen direct taskpack, and immutable
preregistration per selected instance. Build one aggregate pilot contract and
one resource-bound provider-free v2 preflight receipt. Publish a final receipt
which binds the complete chain and explicitly records zero provider calls,
zero scored executions, and no live authorization.

The approved builder owns the budget, counterbalanced mode order, retry policy,
abort conditions, and research-authority hash. Callers cannot substitute those
values during final construction.

## Acceptance

- Three instance maps cover the frozen selection exactly and in order.
- Worker-visible tasks match v9; runtime/model/execution profiles match v10.
- All modes bind equal per-instance inputs and budgets.
- Direct taskpacks are frozen before gold and one-to-one with instances.
- Worst-case aggregate ceiling is 40,500,000 tokens and 48,600 provider wall
  seconds with one inflight invocation.
- Dynamic resource receipt, materialization, pilot, and v2 preflight all replay.
- Gold content, sibling-mode state, and live authority are absent.
- Rehashed drift, publication conflict, symlink substitution, and release
  unavailability fail closed.
- Focused, Phase 3, and complete provider-free integration suites pass.

## Boundary

This task may publish provider-free authorities and reports only. It must not
pull or execute evaluator images, invoke a model for a scored task, publish a
live authorization, admit `P3-LIVE`, merge, push, or activate a release.

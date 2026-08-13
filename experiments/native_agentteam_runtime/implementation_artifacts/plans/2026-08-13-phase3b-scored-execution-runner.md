# Phase 3B Scored Execution Runner

Status: implementation contract

## Decision

Implement `P3B-05` as a resumable pilot controller over the existing immutable
experiment-run allocation and three mode adapters. The controller is the only
component allowed to consume a valid `P3-LIVE` permit. It serializes launches,
accounts aggregate budgets, evaluates retained candidate patches through the
official SWE-EVO evaluator boundary, and publishes the paired pilot report.

The provider-free v11 bundle remains historical evidence. Because the runner
changes the executable runtime release, live execution requires a new release
binding, regenerated derived authorities, and a fresh epoch-bound
authorization. No selected instance, model, reasoning profile, mode, threshold,
or budget may change during that refresh.

## Execution Boundary

For each instance and repetition:

1. derive an `experiment_protocol.v1` from the frozen per-instance authority;
2. allocate one immutable run for each scheduled mode;
3. execute the existing `SingleCodexModeAdapter`,
   `AgentTeamDirectModeAdapter`, or `AgentTeamFullModeAdapter`;
4. use the common finalizer for visible acceptance, usage reconciliation, patch
   publication, and result sealing;
5. after the provider process is terminal, pass only the retained candidate
   patch to the trusted official evaluator;
6. seal the official score beside the mode result and update the pilot
   checkpoint atomically.

Gold patches, gold tests, F2P/P2P identities, and evaluator state must never be
written to a protocol, taskpack, provider sandbox, mode root, prompt, or sibling
mode artifact. The common visible acceptance result is diagnostic evidence; the
official evaluator result is the final score authority.

## Schedule

- Run two initial repetitions for all three selected instances and all three
  modes, using the preregistered counterbalanced rows: 18 mode executions.
- After both initial repetitions of an instance/mode pair are terminal, schedule
  a third repetition only when outcomes disagree or token/wall variance is
  greater than 30 percent.
- Execute third repetitions in the third preregistered row order.
- Keep `max_inflight_model_invocations=1` for the entire pilot.
- A terminal benchmark failure is data, not a controller exception. An
  infrastructure failure is retained and classified.

## Recovery And Authority

The pilot root contains one immutable manifest and one replace-on-write
checkpoint. Each mode run and official score remains independently sealed.
Restarting the controller:

- validates the live permit, release, protocol, taskpack, selection, and
  evaluator digests again;
- reuses terminal mode results and terminal official scores;
- treats an allocated but non-terminal run according to the existing run
  recovery contract;
- never repeats a terminal scored provider call;
- stops before the next provider call on authority drift, incomplete usage,
  budget exhaustion, gold leakage, or an unclassified retry.

Only provider transport and rate-limit failures receive the one preregistered
retry. Reported usage and elapsed time from failed attempts still count.

## Direct Taskpack Bridge

The current Phase 3 materialization stores a benchmark-level direct taskpack,
not the runtime's frozen taskpack directory. Before live authorization, build a
deterministic runtime taskpack from each benchmark-level authority, freeze it
with the existing taskpack implementation, and bind the resulting digest in the
per-instance protocol and refreshed preregistration. This bridge is
provider-free and must be byte-stable.

## Official Evaluator Bridge

Use the fixed SWE-EVO Arrow artifact and the exact image reference plus manifest
digest already bound per instance. The evaluator runs outside provider-visible
roots and receives:

- instance ID and evaluator-only row loaded from the fixed Arrow artifact;
- retained candidate patch;
- exact image reference/digest;
- bounded timeout and resource envelope.

It publishes bounded stdout/stderr digests, F2P/P2P aggregate counts, resolved
status, wall time, image identity, evaluator source identity, and a canonical
receipt. Raw gold content is not copied into retained pilot artifacts.

## Abort Conditions

Abort before the next launch when any of the following occurs:

- authorization, release, protocol, taskpack, evaluator, image, selection, or
  resource authority drift;
- aggregate token or wall ceiling reached;
- provider usage coverage below 100 percent;
- official evaluator cannot prove the expected image digest;
- gold or sibling-mode data becomes provider-visible;
- the resource owner cannot enforce or clean up the required cgroup hierarchy;
- an interrupted run cannot be reconciled without risking a duplicate scored
  call.

## Verification

Before a scored call:

1. unit-test deterministic protocol/taskpack materialization and schedule order;
2. run a provider-free 18-execution simulation through fake mode and evaluator
   boundaries;
3. inject interruptions before launch, after mode sealing, and after official
   scoring, then prove idempotent resume;
4. test budget stop, authority drift, retry classification, third-run triggers,
   and incomplete usage aborts;
5. run the focused Phase 3 suite and full runtime suite;
6. freeze the new runtime release and regenerate all derived authorities;
7. issue a new `P3-LIVE` epoch and run one real smoke execution;
8. continue the admitted schedule only if smoke usage, isolation, scoring,
   cleanup, and recovery evidence are complete.

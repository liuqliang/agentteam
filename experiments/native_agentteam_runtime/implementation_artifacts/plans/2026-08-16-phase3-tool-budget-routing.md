# Phase 3 Tool Budget Routing

Status: completed with broker follow-up

## Decision

Replace the universal Phase 3 Codex tool-call limit with a deterministic
role-and-risk route. Preserve the frozen model and reasoning profile so the
change affects orchestration capacity rather than model capability.

The selected route is invocation authority: the command-line hook limits, the
registered model invocation policy, and the terminal evidence must agree.
Benchmark execution must carry a frozen resource risk target separately from
the task evidence level. A benchmark `medium` stratum maps to resource `L2`,
but does not turn a one-task direct taskpack into an `L2` evidence workflow
that requires a repo-map predecessor.

## Scope

1. Define and validate a versioned tool-budget routing artifact.
2. Select limits from role and frozen resource risk before every Phase 3 model
   invocation.
3. Rewrite trusted Codex commands to exactly match the selected invocation
   policy.
4. Preserve host-captured started, completed, failed, and unmatched tool-call
   lower bounds independently from hook state.
5. Record why exact tamper-resistant admission counts require a separate
   controller-owned broker rather than another provider-writable trace file.
6. Add focused contract, sandbox, scheduler, and mode-adapter tests.

## Initial Routes

| Role | L0 | L1 | L2 | L3 |
| --- | ---: | ---: | ---: | ---: |
| implementation | 12/16 | 20/28 | 28/40 | 40/56 |
| repo_map | 12/16 | 20/28 | 28/40 | 40/56 |
| taskpack_author | 16/24 | 20/28 | 24/32 | 32/44 |
| reviewer/evaluator | 12/16 | 16/24 | 20/28 | 28/40 |

Each cell is `soft/hard`. Unknown roles fail closed; missing risk uses the
frozen task risk and never an inferred repository-wide complexity score.

## Non-goals

- Changing the Phase 3 model or reasoning profile.
- Revising completed DVC calibration artifacts.
- Adding stage token reservations for full mode. That follows this change.
- Increasing limits dynamically in response to model requests.
- Claiming the current same-UID Codex hook state as tamper-resistant evidence.

## Acceptance

- Identical role/risk inputs produce byte-equivalent routing artifacts.
- Codex command policy and registered invocation policy cannot diverge.
- A medium direct benchmark worker receives the `L2` resource route while its
  task evidence contract remains unchanged.
- The controller can distinguish started, completed, failed, unmatched, and
  malformed host-captured JSONL events without parsing model prose.
- Existing legacy context policies remain valid.
- Focused tests and the affected runtime test modules pass.

## Outcome

- Added `tool_budget_route.v1` with fail-closed role aliases and fixed
  `soft/hard` limits.
- Bound benchmark `low/medium/high` strata to resource `L1/L2/L3` in the
  experiment protocol without changing task evidence levels.
- Bound selected routes to mode authority, launch registration, worker role,
  model invocation lifecycle, and the exact Codex hook command.
- Preserved replay compatibility for base and legacy context policies.
- Expanded JSONL measurement into an explicitly labelled host-captured lower
  bound.

## Broker Follow-up

The Codex hook and worker tools execute as the same OS identity inside the
provider boundary. Any writable counter visible to the hook is therefore also
potentially writable by worker shell commands. A second writable sandbox mount
would weaken the existing sole-writable-repository contract without producing
tamper-resistant evidence.

Exact admission accounting must be implemented by a controller-owned tool
broker or equivalent privilege boundary that observes and authorizes tool
requests before execution. Until then, hook counts enforce the budget and
host-captured JSONL supplies an independent lower bound; neither is labelled
as a strong authoritative admission ledger.

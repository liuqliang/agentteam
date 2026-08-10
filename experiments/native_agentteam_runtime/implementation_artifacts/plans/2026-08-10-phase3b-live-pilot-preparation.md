# Phase 3B Live Pilot Preparation

Status: implementation contract in progress; provider-free preparation only.
This document does not authorize a scored run.

## Goal

Prepare the first complexity-stratified SWE-EVO pilot so that its real model
cost can be approved once, bounded mechanically, and attributed to a fixed
experimental protocol. Phase 3B inherits the completed Phase 3A `P3-READY`
result and the research authority; it does not reopen Phase 1 usage counting,
Phase 2 mode fairness, or D0-D5 artifact authority.

## Readiness Authority

The preparation binds the accepted Phase 3A result:

- gate: `P3-READY`;
- controller: `phase3_readiness_controller_v1`;
- relation: `phase3_readiness_relation_v1`;
- retained evidence SHA-256:
  `d87486c41ef79707d52408215b05d22b52d736d16380001590d031c4fb483af7`;
- receipt-content SHA-256:
  `5fa7ff18044f2fcf5c01ee7d33b2592091d6af91bd09c1de641eeffe3a0bfc9d`;
- accepted integration head:
  `ebce7b10a0ab7b2f9dc3e3936df3f7c2e1e77a02`.

Phase 3A recorded zero scored executions and zero provider calls. That absence
is a prerequisite, not a live-run permit.

## Upstream Inventory

The official upstream inspected during preparation is:

- repository: `https://github.com/SWE-EVO/SWE-EVO.git`;
- source commit: `9b83d5af943ba7a17567336f5b18239f73960219`;
- dataset artifact:
  `hf_out/hf_dataset/test/data-00000-of-00001.arrow`;
- artifact SHA-256:
  `74e7c63160ada4ceba71d5d89a9bb7c9794f4574b384458d546eb65cdb730520`;
- split and size: `test`, 48 rows.

The dataset artifact contains evaluator-only fields. A later selection step
must first emit an allowlisted routing manifest and must never expose patch,
test patch, prior result, or score fields to a selecting model or runtime.

## Corrected Multi-Instance Contract

The Phase 3A preregistration v1 binds one `shared_visible_inputs` object. That
is sufficient for its single provider-free fixture, but a real pilot contains
different repository commits, goals, and acceptance commands. Reusing one v1
artifact for several instances would falsely claim equal-input authority.

Phase 3B therefore uses one immutable v1 preregistration per selected instance.
Each preregistration binds that instance's three equal mode inputs, per-mode
budget, direct taskpack, model profile, isolation policy, and counterbalanced
order. `phase3_pilot_contract.v1` then binds the ordered collection and
computes the maximum aggregate token and wall-time ceiling. This is additive:
historical Phase 3A artifacts remain valid and unchanged.

The pilot contract always contains `provider_calls_authorized: false` and
`live_authorization.status: not_authorized`. A separate
`phase3_live_authorization.v1` must match the current gate epoch, contract,
readiness evidence, selection, release, model, reasoning profile, modes, and
aggregate ceilings exactly. Authorization may not expand a frozen budget.

## Task Graph

```text
P3B-00 fixed upstream inventory and gold-blind routing manifest
  -> P3B-01 complexity rule, quotas, seed, and actual selection freeze
      -> P3B-02 per-instance task/input/taskpack preregistration
          -> P3B-03 aggregate pilot contract and provider-free preflight
              -> P3B-04 operator review and epoch-bound live authorization
                  -> P3B-05 scored execution, evaluation, and pilot report
```

`P3B-00` through `P3B-03` are provider-free. `P3B-04` is a mandatory pause.
Only `P3B-05` may invoke the model, and only through the admitted permit.

## Frozen Runtime Rules

- modes are `single_codex`, `agentteam_direct`, and `agentteam_full`;
- every instance/mode starts from its own clean work root and fresh model
  session;
- each instance uses the same model, reasoning profile, service config, tools,
  network, sandbox, permissions, host class, tests, cache policy, token budget,
  wall-time budget, and allowed operator-input budget across modes;
- initial repetitions are two; a third is permitted only by the existing
  disagreement or greater-than-30-percent variance rule;
- maximum inflight model invocations is one for the first pilot;
- only provider transport and rate-limit failures may be retried, and all
  reported usage and elapsed time still count against the instance budget;
- stop immediately on incomplete usage coverage, gold visibility, cross-mode
  artifact visibility, authority digest drift, or budget exhaustion;
- benchmark evaluator output is the only final-score authority.

## Decisions Still Required Before Freeze

These values must not be guessed or mechanically filled from unavailable
evidence:

1. the gold-blind complexity proxy and stratum boundaries;
2. the per-stratum quota and therefore pilot sample size;
3. the exact Codex model and reasoning profile;
4. per-instance token and wall-time ceilings based on measured calibration;
5. non-inferiority margin and acceptable token/time ratios;
6. the one preselected secondary benefit metric;
7. the actual direct-mode taskpack for every selected instance.

The official paper identifies linked pull-request count as a useful complexity
proxy, but the public dataset does not expose a complete dedicated PR-count
column. Phase 3B must either define a deterministic visible-input extraction
rule or use a separately reviewed, gold-blind metadata manifest. It must not
stratify from model outcomes or gold-patch size.

## Acceptance

- upstream source commit, dataset artifact hash, split, and row count match;
- allowlisted metadata conversion emits no evaluator-only field;
- selection replay is byte-stable and binds the actual ordered IDs;
- every selected ID has exactly one preregistration and one frozen direct
  taskpack;
- different instances may have different task inputs, while all three modes
  for one instance bind identical visible inputs and budgets;
- execution profile or mode-order drift across instances is rejected;
- aggregate ceilings equal the sum of all maximum scheduled repetitions;
- changing any instance binding changes the pilot contract digest;
- missing, rejected, stale, mismatched, or budget-expanding live authorization
  is denied before provider admission;
- provider-free tests and preflight report zero live calls;
- provider usage coverage must be 100 percent before a scored result can be
  accepted.

## Stop Conditions

Stop before taskpack freeze if the selected complexity rule depends on gold or
prior outcomes, the requested sample exceeds the approved aggregate budget, a
mode cannot receive an equivalent input, or evaluator material cannot be kept
outside runtime-visible roots. Stop during execution on any abort condition in
the sealed contract. Do not substitute a smaller sample, different model, new
budget, or changed acceptance command after an outcome is visible.

# DVC Medium Calibration Report

## Result

The one-instance calibration completed all three frozen modes with complete
provider accounting. It does not authorize a scored pilot and does not show a
quality advantage for AgentTeam.

| Mode | Result | Total tokens | Uncached input | Provider time | Controller time | Quality |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `single_codex` | valid patch | 339825 | 73939 | 282 s | 305.9 s | partial `0.593939`, unresolved |
| `agentteam_direct` | no patch | 412925 | 80024 | 512 s | 535.7 s | `candidate_patch_invalid` |
| `agentteam_full` | no patch | 588774 | 106595 | 520 s | 597.5 s | `candidate_patch_invalid` |

Aggregate usage was 1341524 tokens with 100 percent invocation coverage. The
fresh run stayed below the frozen 1800000-token aggregate ceiling. The earlier
352125-token single-mode run is infrastructure-recovery cost and is not mixed
into this comparison.

## What Happened

`single_codex` changed `.github/workflows/packages.yaml` and `dvc/repo/ls.py`.
The official evaluator applied the patch and reported F2P `6/14`, P2P `64/66`,
and partial score `0.593939`; the instance remained unresolved.

`agentteam_direct` correctly located the three requested change areas. Two
patch attempts used stale or inaccurate source context and failed atomically.
The worker then reached the frozen 16-call tool limit before it could reread the
exact context, apply a narrower patch, or run verification. It consumed 21.5
percent more tokens and 39.6 percent more controller time than `single_codex`
while producing no candidate.

`agentteam_full` produced a semantically reasonable two-task backlog: an L2
repository-map task followed by a dependent implementation task. Taskpack
authoring alone consumed 326656 tokens. The repository-map worker consumed a
further 262118 tokens, passed 15 focused tests, and identified the relevant
paths, but also reached the fixed tool limit before writing its sole required
handoff. The implementation worker therefore never started. The two
pre-implementation roles consumed 98.1 percent of the mode ceiling.

The common repository-wide `pytest -rA` command failed and exceeded the 4 MiB
retained-output cap in every mode. Provider-free replay had already shown this
command fails broadly on the unchanged benchmark environment, so it is retained
as environment evidence and is not interpreted as candidate quality. The
official SWE-EVO evaluator remains authoritative.

## Findings

1. A fixed 16-call hard limit is too small for this medium task. It converted
   recoverable patch-context and fixture mistakes into terminal failures.
2. Tool admission count is not retained as a durable artifact after the sandbox
   exits. Codex event counts and the worker's terminal statement are available,
   but the exact hook counter is not. Future experiments need an authoritative
   persisted admission count.
3. Full-mode budget allocation is not stage aware. Authoring and mapping can
   consume nearly the entire mode allowance before implementation begins.
4. Full mode duplicates repository-grounding cost: deterministic repo-map
   construction precedes a model-driven repo-map task, while the taskpack author
   also performs repository exploration.
5. Direct mode classified the compound release task as L1, while full mode
   classified its mapping and implementation tasks as L2. Direct-mode risk and
   tool-budget routing need to account for compound goals.
6. The taskpack decomposition itself was directionally correct. The failure was
   execution-resource allocation and artifact publication, not loss of the
   release-note semantics.

## Required Follow-Up

Before another live calibration:

1. Replace the universal 16-call cap with deterministic role/risk budgets,
   retaining a soft warning and a larger hard ceiling for L2 mapping and
   implementation work.
2. Persist the authoritative tool-admission count and exhaustion reason outside
   the sandbox.
3. Add stage reservations so full mode cannot admit authoring or mapping work
   that leaves insufficient token allowance for implementation.
4. Reuse deterministic repository grounding and require the model-driven
   repo-map role only when the task has an explicit unresolved grounding need.
5. Make workers write required handoff artifacts early, then enrich them, so a
   late tool-budget stop retains usable partial progress.
6. Replace or narrow the non-discriminating common acceptance command in a new
   protocol revision; do not mutate this completed run.

These are runtime-treatment changes. Any post-repair comparison must use a new
bundle and rerun every compared mode under one release. This report makes no
statistical claim from one benchmark instance.

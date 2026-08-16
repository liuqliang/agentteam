# Phase 3 Deterministic Grounding And Stage Reserve

Status: in progress

## Calibration Finding

The DVC full-mode taskpack author and model repo-map worker consumed 98.1% of
the 600,000-token mode budget before implementation began. A post-terminal
stage counter alone would only stop the run earlier; it would not make the
implementation task executable.

Codex CLI exposes no hard per-turn token ceiling. Stage limits therefore have
bounded-overshoot semantics: they control subsequent provider admission and
report overshoot after terminal usage, but cannot interrupt a running provider
turn at an exact token boundary.

## Decision

1. Generate the first grounding handoff deterministically from tracked files,
   symbols, task text, and public verification commands.
2. Keep the repo-map task as a completed prerequisite in the frozen taskpack,
   and seed its immutable handoff into the runtime artifact store before the
   scheduler starts.
3. Use a model repo-map worker only when deterministic grounding reports an
   explicit unresolved semantic need. Phase 3 benchmark execution defaults to
   deterministic grounding.
4. Add full-mode stage admission policy after grounding removes the mandatory
   model repo-map turn.

## Artifact Contract

`repo_map_handoff.v1` contains:

- repository commit and tree identity;
- task id and objective digest;
- selected source/config/test paths and symbol summaries;
- candidate public verification paths;
- repository structure and bounded warnings;
- grounding authority `deterministic_repo_context.v1`;
- `semantic_gaps`, empty only when no unresolved need was detected.

The handoff is controller-generated. It is runtime evidence, not a production
candidate change, and must not enter the scored patch.

## Stage Policy

For `agentteam_full`, reserve at least 40% of total mode tokens for
implementation and review. Taskpack authoring may consume at most 60% before
subsequent authoring admissions are denied. With deterministic grounding, no
separate repo-map model reservation is needed in the first calibration replay.

The controller records actual stage usage and stage overshoot. It does not
claim exact interruption at the stage ceiling.

## Acceptance

- A full-mode L2 task begins with an implementation worker and no model
  repo-map invocation when deterministic grounding is complete.
- The implementation worker receives a digest-checked handoff materialized in
  its attempt worktree.
- The handoff never appears in the scored production patch.
- Repeated construction at the same repository tree and task authority is
  byte-identical.
- Missing or mismatched seeded artifacts fail before provider launch.
- Existing non-experiment and follow-up handoff reuse behavior remains valid.


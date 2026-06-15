# M67 Dogfood Calibration Report

Status: completed with author timeout evidence.

## Goal

Run a bounded AgentTeam-as-target calibration after the M67 quality tasks:

- hardened follow-up `next_goal` specificity;
- Chinese next-step rationale in report and Feishu summaries;
- `agentteam queue next` selected-item provenance and readiness;
- roadmap-derived taskpack-author route-template guidance.

The calibration was intentionally limited to taskpack authoring. It did not run
workers, integrate patches, merge, push, activate releases, or edit semantic
authority artifacts.

## Command

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
  python3 -m agentteam_runtime.agentteam taskpack draft \
  --project-root /home/liuql/projects/agentteam/.worktrees/native-runtime-m0 \
  --goal "Follow-up goal: Continue the roadmap-derived implementation route. Previous taskpack context: source_taskpack_id=m67-agentteam-dogfood source_report_path=/tmp/agentteam-m67-dogfood-work/runs/m67-agentteam-dogfood/reports/final_report.md selected next_goal=Run bounded dogfood calibration after M67 quality tasks. Instructions: draft only; do not run workers, merge, push, or activate releases; include evidence_paths, non_goals, success_metrics_or_no_metric_delta, verification_summary, recommended_next_implementation_tasks, and review gates." \
  --draft-root /tmp/agentteam-m67-calibration-drafts \
  --taskpack-id m67-calibration-draft \
  --author-runtime codex \
  --codex-timeout-seconds 300
```

## Result

- Result status: `timed_out`.
- Exit code: `-9`.
- Elapsed time: `300.158` seconds.
- Draft taskpack files written: `0`.
- Target repository status after calibration: clean.

Evidence paths:

- `/tmp/agentteam-m67-calibration-drafts/.m67-calibration-draft-author/author_state.json`
- `/tmp/agentteam-m67-calibration-drafts/.m67-calibration-draft-author/author_result.json`
- `/tmp/agentteam-m67-calibration-drafts/.m67-calibration-draft-author/author_prompt.md`

## Useful Signal

The generated author prompt did contain the new route-template guidance:

- `Roadmap-derived follow-up task template:`
- `source merge, push, and release activation remain operator review gates`

The timeout was not caused by missing prompt guidance. The child Codex session
spent the authoring budget reading local skill instructions and source files,
then timed out before writing the required five taskpack files.

## Calibration Finding

The post-M67 quality fields are present in the author prompt, but live Codex
taskpack authoring remains too open-ended for a five-file authoring job under a
300-second budget. For this route to be reliable, authoring needs a direct
artifact-production mode that minimizes external workflow drift and makes the
expected output shape more mechanically unavoidable.

## Recommended Next Implementation Tasks

1. Add an author-direct mode for Codex taskpack authoring that asks for only the
   five required files and tells the model to avoid planning workflows, skills,
   repository changes, and exploratory source reading unless required to fill a
   field. Implemented after this calibration.
2. Add an author timeout diagnostic that reports whether zero taskpack files
   were written, the largest stderr/stdout contributors, and a compact next
   action. Implemented after this calibration.
3. Re-run the same bounded calibration with the direct author mode before
   starting broader dogfood loops.

Do not proceed to M68 model adapters, DB-primary storage, or direct semantic
authority edits based on this calibration.

## Direct Mode Re-Run

After adding the direct artifact-production protocol and timeout diagnostics,
the same bounded authoring calibration was re-run with a new draft root:

- Draft root: `/tmp/agentteam-m67-calibration-drafts-v2`
- Taskpack id: `m67-calibration-draft-v2`
- Result status: completed.
- Elapsed time: `283.139` seconds.
- Required files written: `5/5`.
- Explicit validation: `agentteam taskpack validate` returned `accepted`.
- Target repository status after calibration: clean.

The generated draft included the required route-template fields:

- `evidence_paths`
- `non_goals`
- `success_metrics_or_no_metric_delta`
- `verification_summary`
- `recommended_next_implementation_tasks`
- `agentteam_target_review_gate`

Remaining calibration signal: the author run succeeded but took 283 seconds,
which is close to the 300-second timeout. The next reliability work should
focus on reducing Codex author latency and making the five-file output shape
even more mechanical before broader live dogfood loops.

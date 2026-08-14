# Phase 3 input-cost causal screen

Status: completed; stopped at the aggregate token gate

## Authority

Decision `DEC-P3-input-cost-causal-screen-v1` authorizes one bounded
historical-control screen on `psf__requests_v2.12.2_v2.12.3`. The retained
sealed run is the baseline. This is an engineering screen, not a randomized
causal experiment.

## Fixed Comparison

- Source commit: `ca15d4808734c86801c8f3d80c9152c35a163dc3`
- Baseline bundle:
  `ee9b336411f15a2b1cfe4a525542895f3b6e3d7f730f07477da90dee1a65edb4`
- Treatment runtime commit:
  `d2f22075c8858faddc4223438c35b2a9c9b886bc`
- Model: `gpt-5.6-sol`, reasoning profile `high`
- Mode order: `single_codex`, `agentteam_direct`, `agentteam_full`
- Treatment: `tool_output_token_limit=4000`, `web_search="disabled"`
- Per-mode token ceiling: `1,000,000`
- Planned three-mode token ceiling: `3,000,000`
- Repetitions: one per mode

The 1,000,000-token ceiling is evaluated at controller stop boundaries.
Provider usage is terminal evidence, so an invocation already in flight can
cross the ceiling before the controller observes it. Modes run one at a time,
and the next mode is not launched until the preceding usage, evaluator, and
resource evidence pass review.

## Stop Conditions

Stop before the next provider launch when any of these conditions holds:

- model identity or context-policy binding differs from the frozen contract;
- provider usage is missing or cannot be reconciled;
- resource cleanup is incomplete;
- candidate-patch compatibility or evaluator infrastructure is invalid;
- observed cumulative cost makes the planned 3,000,000-token ceiling unsafe;
- a corrective operator intervention would be required.

A mode that produces a valid low-quality patch is still retained. Quality
failure is an experiment result, not infrastructure failure.

## Analysis

Compare each mode with its retained baseline using provider total, input,
cached input, uncached input, output, quality, and wall time. Report absolute
changes and ratios. The `2.0x` AgentTeam-to-single token gate remains the
operational threshold, but one repetition cannot establish general benchmark
performance.

Proceed to a new SWE-EVO instance only if the treatment execution is valid,
usage coverage is complete, and the result provides enough evidence to judge
the containment policy. Do not expand automatically.

## Outcome

The final clean treatment run completed `single_codex` and
`agentteam_direct`. It did not launch `agentteam_full` because cumulative
provider usage reached `3,042,089` tokens and crossed the preregistered
`3,000,000`-token aggregate boundary.

- `single_codex` used `266,392` total tokens, `16.25%` below the retained
  historical baseline, with unchanged official quality.
- `agentteam_direct` used `2,775,697` total tokens, `153.93%` above its
  historical baseline and `10.4196x` the treatment single-agent cost.
- The direct candidate conflicted with the evaluator-only test patch and was
  retained as an invalid execution without a quality score.
- `agentteam_full` and a new SWE-EVO instance were not launched.

Transcript profiling found 38 completed direct-mode commands and 538,643
characters of command output. One full test command returned 332,589
characters. The frozen `tool_output_token_limit=4000` setting therefore did
not bound command output returned by Codex CLI, and repeated context replay
remained the dominant cost mechanism.

The screen rejects the current containment policy as sufficient for
orchestrated modes. Phase 3 expansion remains unauthorized. See
[`2026-08-14-phase3-input-cost-causal-screen.md`](../reports/2026-08-14-phase3-input-cost-causal-screen.md).

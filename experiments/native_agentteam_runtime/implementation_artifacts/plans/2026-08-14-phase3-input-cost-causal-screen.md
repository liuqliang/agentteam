# Phase 3 input-cost causal screen

Status: authorized execution

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

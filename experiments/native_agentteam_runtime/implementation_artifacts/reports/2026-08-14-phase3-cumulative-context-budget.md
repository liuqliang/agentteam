# Phase 3 cumulative context budget

## Status

The provider-free implementation, runtime release, and replacement
single-instance preparation bundle are complete. No new model call or scored
mode execution was made.

## Accepted mechanism

All Phase 3 Codex provider positions now share one protocol-bound policy:

- per-tool output limit: `4,000` tokens;
- web search: disabled;
- active-context compaction threshold: `32,768` tokens;
- tool-call soft warning: reservation `12`;
- last admitted tool: reservation `16`;
- later tool requests: rejected by `codex_pre_tool_budget.v1` before execution.

The PreToolUse counter reserves slots under a file lock, so concurrent tool
requests cannot overshoot the hard boundary. Reaching the boundary does not
kill Codex, which preserves its ability to emit a final response and terminal
usage.

## Frozen artifacts

- Runtime commit: `b83c9805574ca45b1f703fafb6b78da051402820`
- Runtime release: `phase3-calibration-b83c980`
- Release root: `/tmp/agentteam-phase3-calibration-release-v12`
- Preparation bundle root:
  `/tmp/agentteam-phase3-calibration-bundle-requests-3738-v14`
- Bundle SHA-256:
  `0431260bc6618402c72b965455e197c951d90528793c78d451c785c41d26d416`
- Pilot contract SHA-256:
  `aebdbb518f8387c8c8cabc6c5c2bed171bd9f921229e5150acd66f153868d6b3`
- Calibration contract SHA-256:
  `13bdce35f9ca804ea79bd690966834e3ce33f0c9a84ab4b3dcfa8d24d17d8f0c`
- Protocol SHA-256:
  `478eec46c027d35b0939d4a9e503cb4c76543a16bc938302da0234ed39b4fdb3`
- Validation receipt SHA-256:
  `6f8eda29c6b6c9c2a26ecec81720a46c4c662f53b436dbd67658d3cf41a39cb0`

The bundle rebuilds every runtime-dependent authority instead of replacing a
single release field. It includes the visible input, direct taskpack,
preregistration, pilot, bounded calibration contract, protocol, and frozen
runtime taskpack. The Requests source, task semantics, public verification
environment, resource envelope, and evaluator authority remain unchanged.

## Verification

- Generated-hook tests: `6` passed.
- Focused Phase 3 suite: `239` passed, `5` skipped.
- Complete provider-free suite: `1,209` passed, `7` skipped.
- Independent bundle replay validated all `14` canonical JSON artifact
  bindings and the bundle digest.
- The frozen runtime taskpack digest replayed successfully.
- Installed Codex `0.147.0` accepted the exact compaction and PreToolUse
  configuration in a provider-free `features list` probe.
- No `stdout.jsonl`, usage record, model-invocation evidence, live epoch, or
  live authorization exists in the replacement bundle.

## Boundary

This result proves mechanism integrity, not cost reduction or benchmark
quality. The preparation bundle records zero provider calls, zero scored mode
executions, and `live_authorization=not_authorized`. A later execution decision
must choose between a bounded Requests causal retest and a new SWE-EVO instance,
set the paid token ceiling, and issue a fresh epoch-bound authorization.

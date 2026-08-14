# Phase 3 cumulative context budget

Status: provider-free implementation complete

## Problem correction

The retained Codex JSONL records complete command events. A 332,589-character
`aggregated_output` in that file does not prove that all of those characters
were sent back to the model. Codex applies `tool_output_token_limit` while
recording function output into model history and may retain a fuller event for
operator visibility.

The valid conclusion from the Requests screen is narrower: a per-tool limit
does not cap cumulative provider input across many sampling rounds. The
treatment direct worker completed 38 commands and reported 2,754,757 input
tokens. Cached input was 2,625,536 tokens, consistent with repeated history
reuse across a long turn.

## Historical calibration

Completed command counts in the retained valid baseline were:

| Provider position | Completed tool calls |
| --- | ---: |
| `single_codex` | 10 |
| `agentteam_direct` worker | 12 |
| `agentteam_full` author | 5 |
| `agentteam_full` worker | 13 |

The failed containment direct worker completed 41 tool calls: 38 commands and
3 file changes. A common soft limit of 12 and hard limit of 16 therefore leaves
headroom above every retained valid execution while bounding the observed
diagnostic loop.

## Bound policy

New Phase 3 protocols bind all of these fields together:

- `tool_output_token_limit=4000`;
- `web_search_policy=disabled`;
- `model_auto_compact_token_limit=32768`;
- `tool_call_soft_limit=12`;
- `tool_call_hard_limit=16`;
- `tool_budget_policy=codex_pre_tool_budget.v1`.

The policy is applied equally to every scored provider position. Historical
protocols containing either no context controls or the earlier two-field
policy remain readable.

## Enforcement

AgentTeam generates a deterministic inline Codex `PreToolUse` command hook.
The hook uses a locked, turn-scoped reservation counter under the provider
sandbox's temporary directory:

1. the first 16 calls reserve their execution slots atomically before running,
   so parallel tool requests cannot overshoot the hard boundary;
2. reservation 12 injects bounded context instructing the worker to stop
   exploration, preserve its patch, run only essential checks, and finalize;
3. reservation 16 is admitted with a final-tool warning, while reservation 17
   and every later request is rejected before the tool executes;
4. the model remains able to produce its final response, so Codex can emit
   terminal usage normally, unlike an external process kill;
5. the hook command and limits are part of the registered launch command and
   are checked before provider admission.

The counter is an execution control, not semantic authority. Candidate code,
usage, evaluator evidence, and the decision record remain authoritative.

## Provider-free acceptance

- Protocol schema accepts the complete six-field policy and rejects partial
  combinations or invalid limit ordering.
- Sandbox and launch registration preserve all fields.
- Every Codex command contains exactly one auto-compact setting, one exact hook
  configuration, and one hook-trust bypass flag.
- Direct invocation of the generated hook proves soft warning, hard stop,
  per-turn isolation, and locked concurrent counting without a model call.
- Existing protocols and the complete provider-free test suite remain valid.
- The Requests report is corrected so full transcript output is not described
  as model-visible output.

No new SWE-EVO execution is authorized by this implementation. A later
decision must freeze a replacement bundle and choose whether to rerun Requests
or select a new instance.

## Verification result

- Generated PreToolUse hook tests: `6` passed, including locked concurrent
  counting and deterministic transcript replay.
- Focused protocol, mode, runner, and experiment suite: `239` passed,
  `5` skipped.
- Complete provider-free suite: `1,209` passed, `7` skipped.
- Installed Codex `0.147.0` accepted both generated hook TOML configurations
  and the 32,768-token compaction setting through a zero-provider config probe.
- Retained transcript replay produced valid call counts with zero malformed
  events: baseline positions `10`, `12`, `5`, and `13`; treatment positions
  `11` and `41`.

No model-provider call was made during implementation or verification.

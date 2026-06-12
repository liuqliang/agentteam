# M45 Chinese Operator Brief Design

## Purpose

M45 adds a Chinese-language operator brief for people who review completed
AgentTeam runs in Chinese. The brief is a human-facing rendering of existing
completion data. It does not create new runtime authority.

The two surfaces are:

- `agentteam report` and other completion-report artifacts;
- Feishu `run_completed` notifications, including manual resend through
  `agentteam notify run-completed`.

## Design Boundary

The brief is additive. Existing result JSON, report content, evidence summaries,
event payloads, and integration gates remain the source of truth. Consumers must
continue to make decisions from structured fields such as `result_status`,
`changed_files`, `operator_summary`, `evidence_summary`, integration state, and
report metadata.

The brief is deterministic. The formatter uses fixed Chinese labels, stable
section ordering, enum-to-text mappings, and bounded copies of existing source
fields. It must not call an LLM, invoke machine translation, summarize raw
worker transcripts, or paraphrase patches and logs.

Codex worker prompts should also request Chinese `operator_summary` prose for
operator-facing fields. The formatter remains deterministic: it wraps and
bounds the returned summary, but it does not translate English text after the
fact. File paths, commands, code symbols, and metric names should stay literal.

## Input Contract

The formatter may read only already-produced completion summary and report data:

- run id and run status;
- task count and blocked count;
- `completion_summary.what_changed`;
- `completion_summary.changed_files`;
- `completion_summary.verification`;
- `completion_summary.integration`;
- `completion_summary.integration_recommendation`;
- `completion_summary.next_steps`;
- `completion_summary.evidence_gaps`.

If an optional source field is absent, the brief must either omit that detail or
emit a fixed Chinese fallback such as `未提供`. It must not invent a replacement.

## Output Contract

The completion report exposes an additive localized view alongside the existing
report data:

```json
{
  "completion_summary": {
    "chinese_operator_brief": [
      "本次运行已完成，共 1 个任务，0 个阻塞。",
      "主要变更：...",
      "验证情况：...",
      "集成状态：已通过",
      "下一步：..."
    ]
  }
}
```

The list uses this stable line order when the source data is available:

1. `结果`: mapped from `result_status`.
2. `变更`: derived from `what_changed` and bounded `changed_files`.
3. `验证`: derived from verification summary lines.
4. `集成状态`: derived from integration state.
5. `合并建议`: derived from the integration recommendation.
6. `下一步`: derived from next steps.
7. `证据缺口`: derived from evidence gaps.

Free-text source spans are copied, bounded, and sanitized. The formatter may add
Chinese connective words and labels, but it must not translate arbitrary
operator prose. This keeps the brief deterministic while still giving Chinese
operators a consistent natural-language envelope.

## Feishu `run_completed` Contract

Feishu remains outbound-only. M45 only changes the `run_completed` message body:
after the existing report summary is built, the notification formatter includes
the same Chinese brief when available.

The notification must preserve existing Feishu constraints:

- no webhook URLs, signing secrets, access tokens, raw prompts, raw transcripts,
  patches, or long logs;
- bounded message size with deterministic truncation;
- notification failure records telemetry but does not block scheduling,
  validation, integration, report generation, or run completion;
- Feishu messages never approve merges, resume runs, or mutate runtime state.

Other notification event types, including manual gates and run-started events,
are outside M45 unless a later milestone explicitly extends the contract.

## Non-Goals

- no LLM-generated summary;
- no machine translation of arbitrary worker prose;
- no replacement of English or structured report fields;
- no automatic merge, rebase, approval, manual-gate answer, or Feishu callback;
- no change to task status, evidence policy, integration policy, or artifact
  retention behavior;
- no broad localization framework beyond the `zh-CN` completion brief.

## Verification Expectations

Future implementation should include deterministic tests that:

- build a completion report with `completion_summary.chinese_operator_brief`
  while preserving existing report fields;
- render the same brief into a dry-run Feishu `run_completed` message;
- verify identical input produces identical text;
- verify missing optional fields use fixed fallbacks or omissions;
- verify no live model call, real Feishu network call, secret leakage, or
  automatic merge path is introduced.

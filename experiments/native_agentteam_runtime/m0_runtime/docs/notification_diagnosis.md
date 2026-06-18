# AgentTeam Feishu notification diagnosis

Use the project CLI diagnosis path when an operator needs to check Feishu custom bot delivery without writing runtime state or printing webhook secrets.

```bash
agentteam notify diagnose --dry-run --json
agentteam notify diagnose --message "AgentTeam delivery diagnosis"
```

The command reads the current project profile, checks both the rich text payload and the concise text fallback payload, and reports env var names rather than secret values.

The lower-level m0 runtime CLI has the same delivery diagnosis path for runtime debugging:

```bash
python3 -m agentteam_runtime.cli \
  --output-dir /tmp/agentteam-notify-diagnosis \
  --diagnose-feishu-webhook \
  --notification-project agentteam \
  --feishu-webhook-env AGENTTEAM_FEISHU_WEBHOOK \
  --feishu-signing-secret-env AGENTTEAM_FEISHU_SIGNING_SECRET
```

The command prints JSON only. It reports the env var names, whether each value is set, signing status, and `delivery_variants` for `rich_text` and `concise_text`. It does not print the webhook URL, hook token, or signing secret.

Use `--diagnose-feishu-dry-run` to validate payload construction without sending to Feishu:

```bash
python3 -m agentteam_runtime.cli \
  --output-dir /tmp/agentteam-notify-diagnosis \
  --diagnose-feishu-webhook \
  --diagnose-feishu-dry-run \
  --notification-project agentteam \
  --feishu-webhook-env AGENTTEAM_FEISHU_WEBHOOK \
  --feishu-signing-secret-env AGENTTEAM_FEISHU_SIGNING_SECRET
```

Runtime notification telemetry now includes bounded delivery metadata when a send fails, retries, or uses fallback:

- `delivery_attempt_count`: number of concrete send attempts recorded.
- `max_delivery_attempts`: configured upper bound per payload variant.
- `delivery_attempts`: sanitized attempt records with variant, attempt number, result, status/body codes, and bounded error summaries.
- `fallback_used` and `fallback_reason`: present when the rich text payload is rejected and the notifier tries the concise text fallback.
- `delivery_variant`: final variant that determined the notification outcome.

If Feishu rejects the rich text variant with `body_code=11232`, AgentTeam sends the concise text fallback. Operators should check `delivery_attempts` to confirm the rejected rich attempt and the fallback result.

Token usage telemetry in reports, status summaries, and notifications includes machine-readable source metadata when usage is reported:

- `usage_source=codex_jsonl`: token usage was parsed from Codex JSONL stdout events.
- `usage_source=runtime_result`: token usage was provided explicitly by the worker runtime result.
- `usage_source=mixed` with `usage_sources`: aggregate usage contains more than one source.

When runtime attempts do not report token usage, the unavailable reason is `missing_runtime_token_usage`. Diagnostic notification payloads use `usage_status=not_applicable` with `reason=diagnostic_notification` so operators can distinguish diagnostics from missing worker telemetry.

Focused token observability verification:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_token_usage_unavailable_output_explains_missing_runtime_reports \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_token_usage_records_source_for_runtime_result_and_codex_jsonl \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_token_usage_preserves_not_applicable_diagnostic_semantics \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_feishu_webhook_diagnosis_tests_variants_without_secret_leak \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_feishu_notification_sink_from_env_sends_manual_gate \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_run_completed_event_contains_operator_report_from_runtime_summary \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_codex_runtime_adapter_collects_token_usage_from_json_events
```

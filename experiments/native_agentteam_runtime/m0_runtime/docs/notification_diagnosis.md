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

# Phase 1 Model Invocation Usage Milestone Report

Milestone status: `finalization_pending`

This report is rendered from canonical deterministic evidence and controller-validated P1-LIVE evidence. Its merge recommendation remains conditional until the external P1-06E finalization artifact and immutable operator approval both validate.

## Evidence bindings

- implementation_run_id: `phase1-model-invocation-usage`
- gate_epoch: `8`
- acceptance_series_id: `phase1-model-invocation-usage-acceptance`
- selected_acceptance_run_id: `phase1-model-invocation-usage-acceptance-attempt-007`
- git_object_format: `sha1`
- validated_code_sha: `c6bd4868cc83a23d3182d281273d6efb0bfa83b4`
- acceptance_attempts_sha256: `1f586acb3c9b25e9e2911d404e5110d4ee57fdc809caffbd189e76c4f0a25ca2`
- deterministic_verification_sha256: `f962380d120fffc97dad8035d2c5e0ac996a780e76c4ce43205cb58c24bb61fc`
- gate_epoch_sha256: `20c3020ce081a6bd65c9dc5e6ac5e5ffc306a3c02ffbd74b77c05d96941206ee`
- p1_live_receipt_sha256: `ae220bdd9bd2dcede81ecafe21dd096f8c868467a457261ca4ea40baa75d13d1`
- projection_invocation_sha256: `980231234343ba4ff84040ea8584a9b7e6df887db5da883e7e63cd04c22a9537`
- selected_live_artifact_sha256: `1a28fcb212c2bc0b182702374bf3006a64b653e665978137360b805ae8061b6d`

## Implemented invocation paths

- `acceptance_live_smoke`

## Unsupported or unavailable paths

None recorded by canonical evidence.

## Coverage result

- lifecycle_terminal_coverage: `{"covered":1,"percent":100.0,"status":"complete","total":1}`
- token_usage_coverage: `{"covered":1,"percent":100.0,"status":"complete","total":1}`
- reported_token_totals: `{"cached_input_tokens":0,"contributing_invocation_count":1,"input_tokens":15635,"output_tokens":5,"reasoning_tokens":0,"total_tokens":15640}`
- partial_known_token_lower_bounds: `{"cached_input_tokens":null,"contributing_invocation_count":0,"input_tokens":null,"output_tokens":null,"reasoning_tokens":null,"total_tokens":null}`
- open_invocations: `0`
- exact_deterministic_fixture_token_totals: `{"cached_input_tokens":45,"input_tokens":270,"output_tokens":75,"reasoning_tokens":30,"total_tokens":345}`

## Acceptance series attempts

| Run | Attempt | Selected | Controller status | Usage |
| --- | --- | --- | --- | --- |
| phase1-model-invocation-usage-acceptance-attempt-007 | attempt-007 | yes | passed | {"invocation_count":1,"open_invocations":0,"partial_known_token_lower_bounds":{"cached_input_tokens":null,"contributing_invocation_count":0,"input_tokens":null,"output_tokens":null,"reasoning_tokens":null,"total_tokens":null},"reported_token_totals":{"cached_input_tokens":0,"contributing_invocation_count":1,"input_tokens":15635,"output_tokens":5,"reasoning_tokens":0,"total_tokens":15640},"usage_status_counts":{"not_applicable":0,"partial":0,"reported":1,"unavailable":0}} |

## Projection replay and deterministic verification

- projection_source: `authoritative_files_with_replay_validation`
- projection_check_status: `passed`
- deterministic_completion_status: `passed`
- task_result_count: `10`
- integration_verification_passed_count: `10`
- verification_command_sha256: `ae97888f39af4ffab03fde88ec011e49f9e40de929e842d893d1483b1f1dc47a`
- verification_result_sha256: `9250527342890bd96c1f8abbae80b9f809dba3d074b9ab8b2c629b5e1a4c5480`

## Changed files

- `docs/agentteam-command-reference.md`
- `experiments/native_agentteam_runtime/README.md`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/diagnostic_chat.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_cli_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_multifile_pipeline_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_pipeline_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_repo_context_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_scheduler_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/model_invocation.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/phase1_usage_acceptance.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/phase1_usage_report.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/usage_live_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/tests/test_invocation_projection.py`
- `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`
- `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- `experiments/native_agentteam_runtime/m0_runtime/tests/test_phase1_usage_end_to_end.py`
- `experiments/native_agentteam_runtime/m0_runtime/tests/test_projection_db_operator_paths.py`
- `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- `experiments/native_agentteam_runtime/schemas/phase1_usage_finalization.schema.json`

## Remaining risks

- `P1-06E operator approval remains required before source integration`
- `the controller does not merge, push, or activate a runtime release`

## Conditional merge recommendation

Do not merge yet. The source state is `finalization_pending`. Proceed only if the external finalization artifact validates the report-only commit lineage and P1-06E reaches `awaiting_operator_review`; source integration remains blocked until the matching immutable operator approval passes.

# M36a Live Codex Pipeline Smoke Implementation Notes

## Goal

Prove the smallest complete implementation pipeline before attempting a larger
multi-file pilot.

The smoke is intentionally not a product feature. It verifies that a Codex
worker can receive bounded role and repo context, edit an isolated worktree,
report changed files, pass scheduler validation, enter the integration queue,
pass batch verification, merge back to the source repository, and leave the
source repository verified.

## Implemented Behavior

`agentteam_runtime.live_codex_pipeline_smoke` is gated by
`AGENTTEAM_RUN_LIVE_CODEX=1`. Without the gate it returns a skipped JSON
summary and does not create the output directory.

When enabled, the smoke creates a temporary Git repository containing:

- `src/text_utils.py`, with an intentionally incomplete `normalize_slug`
  implementation;
- `tests/test_text_utils.py`, using Python stdlib `unittest`;
- one backlog task that may write only `src/text_utils.py`;
- one role agent with a bounded role context package and repo-map references.

The task asks the worker to read `role_context_path` and `repo_context_path`,
fix `normalize_slug`, avoid editing tests, and report exactly
`src/text_utils.py` in `changed_files`.

After `run_simulation` returns, the smoke verifies the full delivery path:

- scheduler validation is accepted;
- diff audit reports exactly `src/text_utils.py` as the actual changed file;
- the accepted patch is present in the integration queue;
- `verify_integration_batch(..., merge_verified_batch=True)` applies the patch
  in a batch worktree;
- `python -m unittest discover -s tests` passes in the batch worktree;
- the verified batch is fast-forward merged into the source repository;
- the same test command passes again in the source repository.

The summary reports the repo context path, role context path, changed files,
actual changed files, queue status, batch status, verification status, merge
status, and source verification result.

## Validation Boundary

This milestone exposed a validation gap: existing write-scope validation treated
every `write_scope` entry as a directory prefix. A task scoped to
`src/text_utils.py` was therefore rejected because the validator normalized it
as `src/text_utils.py/`.

The runtime now accepts exact-file scopes as well as directory scopes. A changed
file is valid when it exactly equals a scope entry or falls under a scope
directory prefix.

## Boundary

Normal tests must remain deterministic and must not require live Codex. The
test suite uses a fake Codex command that reads the real mailbox prompt,
consumes `repo_context_path` and `role_context_path`, edits the source file, and
writes the same `--output-last-message` contract that the Codex adapter expects.

The next pilot should not simply make this fixture larger. It should exercise a
small multi-file task with source, tests, and docs so the scheduler can prove
that decomposition, selected repo context, candidate tests, patch batching, and
verified merge work together on a more realistic change.

## Validation

Focused deterministic coverage:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_live_codex_smoke.LiveCodexSmokeTests.test_live_codex_pipeline_smoke_skips_without_env_gate \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_live_codex_smoke.LiveCodexSmokeTests.test_live_codex_pipeline_smoke_runs_with_fake_codex_command \
  -v
```

Exact-file scope regression coverage:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_attempt_outcome_accepts_exact_file_write_scope \
  -v
```

Local live validation:

- `AGENTTEAM_RUN_LIVE_CODEX=1` with the default `codex exec` adapter completed
  one full smoke run;
- summary status was `completed`;
- scheduler validation was `accepted`;
- integration batch status was `verified`;
- merge status was `merged`;
- source-repo verification exited with code `0`.

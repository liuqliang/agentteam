# M36b Multi-File Codex Pipeline Smoke Implementation Notes

## Goal

Validate a small multi-file implementation task on the same runtime pipeline
that M36a used for the single-file smoke.

M36b is still a fixture-level pilot, but it is intentionally closer to normal
implementation work: source code, documentation, and tests all interact, and
the accepted result must modify more than one file before integration.

## Implemented Behavior

`agentteam_runtime.live_codex_multifile_pipeline_smoke` is gated by
`AGENTTEAM_RUN_LIVE_CODEX=1`. Without the gate it returns a skipped JSON
summary and does not create the output directory.

When enabled, the smoke creates a temporary Git repository containing:

- `src/toc.py`, with an intentionally incomplete `build_toc(markdown_text)`;
- `docs/guide.md`, with empty table-of-contents markers;
- `tests/test_toc.py`, which verifies both the TOC builder and the rendered
  guide content.

The backlog task asks the worker to:

- read `role_context_path` and `repo_context_path`;
- implement `build_toc(markdown_text)`;
- update `docs/guide.md` between `<!-- TOC:start -->` and
  `<!-- TOC:end -->`;
- edit only `docs/guide.md` and `src/toc.py`;
- report exactly those two files in `changed_files`.

After `run_simulation` returns, the smoke verifies the same delivery gates as
M36a:

- scheduler validation is accepted;
- diff audit reports exactly `docs/guide.md` and `src/toc.py`;
- the accepted patch is queued for integration;
- batch verification runs `python -m unittest discover -s tests`;
- the verified batch is fast-forward merged into the source repository;
- the same test command passes again in the source repository;
- the merged guide contains the expected TOC entries.

## Added Coverage

Compared with M36a, this covers:

- multiple exact-file write scopes in one task;
- source and documentation edits in the same patch;
- tests that check consistency between code behavior and generated docs;
- repo context selection for a task that spans `src/`, `docs/`, and `tests/`;
- a more realistic L1-sized task while keeping the fixture deterministic.

## Boundary

This is not yet proof that the runtime can handle an arbitrary large project.
It proves that the current Codex-only control plane can carry a small
multi-file change through the complete local integration workflow.

Normal tests still use a fake Codex command. The fake command reads the real
mailbox prompt, consumes both context files, edits both scoped files, and emits
the same `--output-last-message` result contract expected from `codex exec`.

## Validation

Focused deterministic coverage:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_live_codex_smoke.LiveCodexSmokeTests.test_live_codex_multifile_pipeline_smoke_skips_without_env_gate \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_live_codex_smoke.LiveCodexSmokeTests.test_live_codex_multifile_pipeline_smoke_runs_with_fake_codex_command \
  -v
```

Local live validation:

- `AGENTTEAM_RUN_LIVE_CODEX=1` with the default `codex exec` adapter completed
  one full multi-file smoke run;
- summary status was `completed`;
- scheduler validation was `accepted`;
- actual changed files were exactly `docs/guide.md` and `src/toc.py`;
- integration batch status was `verified`;
- merge status was `merged`;
- source-repo verification exited with code `0`.

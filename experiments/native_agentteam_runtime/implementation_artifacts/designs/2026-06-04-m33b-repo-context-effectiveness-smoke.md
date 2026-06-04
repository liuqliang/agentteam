# M33b Repo Context Effectiveness Smoke Implementation Notes

## Goal

Provide a gated smoke path that checks whether a Codex worker can consume the
`repo_context_path` attached by M32/M33a. This complements the observability
view: M33a shows that context was attached; M33b checks that a worker can read
and use it.

## Entry Point

Run the smoke through:

```bash
AGENTTEAM_RUN_LIVE_CODEX=1 \
python3 -m agentteam_runtime.live_codex_repo_context_smoke \
  --output-dir <scratch-dir>
```

Without `AGENTTEAM_RUN_LIVE_CODEX=1`, the module exits successfully with a
`skipped` summary and does not create the output directory.

The smoke also accepts `--codex-command ...`, which lets deterministic tests
use a fake Codex command while preserving the same runtime adapter path.

## Fixture Shape

The smoke creates a temporary Git repository containing:

```text
README.md
pkg/context_target.py
```

`pkg/context_target.py` defines `repo_context_smoke_target`. The backlog task
asks the worker to read `repo_context_path`, identify the selected file
containing that function, and write:

```text
generated/live_codex_repo_context_smoke.json
```

The smoke succeeds only when:

- the scheduler accepts the result;
- the expected generated file exists;
- the runtime result reports the expected generated file in `changed_files`;
- the repo context selected file is `pkg/context_target.py`;
- the worker-reported `selected_file` is also `pkg/context_target.py`.

## Deterministic Coverage

Unit tests cover:

- env-gated skip behavior;
- fake Codex execution through `CodexRuntimeAdapter`;
- fake command parsing the model prompt, extracting `repo_context_path`, reading
  the repo context JSON, and reporting the selected file.

No normal test requires a live Codex call.

## Current Limits

This smoke proves that the repo context path is usable by a worker process. It
does not yet measure whether arbitrary live Codex runs consistently prefer
selected files, nor does it compute selected-file hit rate from diff audit.
Those are follow-up observability metrics.

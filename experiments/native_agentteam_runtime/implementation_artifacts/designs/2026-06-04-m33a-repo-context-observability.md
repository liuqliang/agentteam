# M33a Repo Context Observability Implementation Notes

## Goal

Make repository context attachment inspectable from the normal runtime
observability path. Operators should not need to open raw mailbox files to know
which attempt received which `repo_context.v1` package.

## Implemented Behavior

When a dispatch payload includes `repo_context_path`, the scheduler records it
on the `message_dispatched` event payload together with
`repo_context_schema_version`.

Event replay restores these fields onto the attempt state:

```json
{
  "attempt_id": "ATTEMPT-001",
  "repo_context_path": "run/repo_contexts/ATTEMPT-001-repo_map_agent.json",
  "repo_context_schema_version": "repo_context.v1"
}
```

The SQLite state index exposes `repo_context_path` on the `attempts` table.

## Observability View

`build_runtime_observability(output_dir, view="repo-contexts")` reads
`repo_contexts/*.json` and returns compact summaries:

- context path and schema version;
- attempt id when state-index data links the context path to an attempt;
- task id and agent role;
- selected file count;
- selected file paths, languages, categories, and selection reasons;
- omitted file count;
- repo-map manifest path;
- warning count.

The view does not embed source bodies or full symbol maps.

The CLI exposes the same view through:

```bash
python3 -m agentteam_runtime.cli \
  --output-dir <run-dir> \
  --show-runtime-observability \
  --observability-view repo-contexts
```

## Current Limits

M33a shows which context was attached. It does not yet prove that a live model
read or used the selected files. That belongs to a live Codex effectiveness
smoke or a later diff-audit hit-rate metric.

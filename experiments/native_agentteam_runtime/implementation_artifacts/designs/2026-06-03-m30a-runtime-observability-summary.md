# M30a Runtime Observability Summary Design

## Goal

Provide a read-only CLI summary for an existing runtime output directory without
requiring raw JSONL inspection.

## Design

M30a adds `build_runtime_observability(output_dir)` and exposes it through:

```text
python -m agentteam_runtime.cli --output-dir <run> --show-runtime-observability
```

The command requires only `--output-dir`. It reads:

- canonical `events.jsonl` through replay;
- the rebuildable SQLite state index for event count and latest event;
- `state/integration_queue.json` when present;
- worker registry files when present.

The output is a sorted JSON object containing task, attempt, lease, runtime
session, integration queue, and worker status counts, plus the latest event and
bounded recent failure summaries.

## Policy

This is a CLI-only monitor. It does not add a dashboard, polling UI, new storage
authority, or mutation path. Canonical events remain the source of truth.

## Non-Goals

M30a does not add per-resource drilldown commands, live tailing, or local web
dashboard behavior. Those remain later M30 slices.

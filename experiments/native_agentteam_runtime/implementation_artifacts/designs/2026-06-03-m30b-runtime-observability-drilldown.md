# M30b Runtime Observability Drilldown Design

## Goal

Let operators inspect specific runtime resources from the CLI without opening
raw JSONL or SQLite files.

## Design

M30b extends the M30a command with `--observability-view`:

```text
python -m agentteam_runtime.cli \
  --output-dir output/current \
  --show-runtime-observability \
  --observability-view events
```

Supported views are:

- `summary`;
- `backlog`;
- `leases`;
- `events`;
- `sessions`;
- `workers`;
- `integration-queue`.

Every view includes common metadata: output directory, events path, state DB
path, event count, and latest event. Drilldown views add only the relevant
resource list. The command remains read-only.

## Policy

`--observability-view` is valid only with `--show-runtime-observability`. The
default view remains `summary`, so M30a callers keep the same behavior.

## Non-Goals

M30b does not add live tailing, pagination, filtering, dashboard behavior, or a
new storage authority.

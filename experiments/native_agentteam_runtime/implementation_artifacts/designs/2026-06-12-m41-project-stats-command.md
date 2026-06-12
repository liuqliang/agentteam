# M41 Project Stats Command Design

## Purpose

M41 adds a compact read-only `agentteam stats` command. The command turns the
M40 projection database into an operator-facing project summary: runs,
taskpacks, events, tasks, evidence completeness, artifact footprint, retention
classes, and token usage.

The command is diagnostic. It does not mutate the project, rebuild the
projection, delete artifacts, or change runtime state.

## Data Source

`stats` should prefer a fresh `<work_root>/agentteam.db` projection. If the DB
is missing, stale, or unreadable, it should fall back to a direct file scan of
`frozen/` and `runs/` and mark the output with `projection_source: files`.

This keeps the command useful for new projects while preserving the DB as an
optional acceleration layer rather than a new authority.

## Output Shape

JSON output should include:

- project and work root;
- projection source and check status;
- top-level counts for runs, taskpacks, events, tasks, evidence summaries,
  artifacts, and artifact bytes;
- evidence status counts;
- artifact type counts and bytes;
- retention policy counts and bytes;
- aggregate token usage across indexed runs.

Human output should remain compact and line-oriented so it can be read in a
terminal without scanning long logs.

## Non-Goals

- no DB-primary storage;
- no automatic `db rebuild`;
- no deletion or cleanup action;
- no live model calls;
- no dashboard or server process.

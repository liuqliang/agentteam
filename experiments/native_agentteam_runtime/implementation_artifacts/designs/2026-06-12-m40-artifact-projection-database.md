# M40 Artifact Projection Database Design

## Purpose

M40 adds a project-level SQLite projection database at
`<work_root>/agentteam.db`. The database exists to make long-running AgentTeam
projects easier to inspect, summarize, and clean up. It is not a new authority
layer.

The authoritative records remain the existing file-backed artifacts:

- frozen taskpacks under `frozen/`;
- run directories under `runs/`;
- append-only `events.jsonl`;
- scheduler state snapshots;
- operator reports;
- patch, context, and integration artifacts.

Deleting `agentteam.db` must never delete history. Running `agentteam db
rebuild` must recreate the projection from the authoritative files.

## Architecture

The projection database is project-scoped, not run-scoped. This is separate
from the existing per-run `state/scheduler_state.sqlite` index. The per-run
index remains a fast local view of one scheduler event log. The M40 database
aggregates many runs and frozen taskpacks for one project.

The projection module owns:

- schema creation and schema version checks;
- rebuild from `work_root`;
- consistency check summaries;
- compact read summaries for future `status`, `logs`, `taskpack list`,
  `report`, `gc`, and statistics commands.

Writes are rebuildable and best-effort. Runtime workers and scheduler event
writes must not depend on database writes succeeding.

## Schema Slices

M40a started with only the tables needed for reliable rebuild and check:

- `schema_info`: schema version and creation metadata;
- `runs`: one row per run directory, including run id, run dir, status, event
  count, latest event, and report path when present;
- `taskpacks`: one row per frozen taskpack, including taskpack id, path, and
  validation metadata when available;
- `events`: projected runtime events keyed by run id and event sequence;
- `tasks`: projected task status rows from per-run scheduler state or event
  replay;
- `evidence_summaries`: compact evidence level/status rows from scheduler
  step results.

M40c adds the first artifact-level index:

- `artifacts`: physical artifact path, logical type, run/task/attempt ids when
  known, content hash, size, source, authority, and retention policy;
- `run_stats`: per-run task/event/evidence/artifact counts, artifact bytes, and
  token usage aggregates.

`evidence_summaries` also carries a deterministic content hash and size for
the serialized summary row. These rows are still projected facts, not a new
authority.

Later slices may add release references, manual gates, permission requests,
integration queue details, and DB-backed statistics views.

## CLI Contract

M40a adds:

```bash
agentteam db rebuild --project-root <repo>
agentteam db check --project-root <repo>
```

Both commands support `--json`. Human output is intentionally compact:

```text
db_status: rebuilt
db_path: /path/to/work_root/agentteam.db
runs: 3
taskpacks: 5
events: 120
evidence: complete=4, incomplete=1
```

`check` validates that the database exists, has the expected schema version,
and that projected counts plus artifact digest match a fresh file scan. If the
database is missing or stale, it reports the mismatch; it does not rebuild
unless the operator runs `rebuild`.

`gc --dry-run` may read a fresh projection to summarize artifact counts, bytes,
retention policies, and token usage. It explains authoritative artifacts and
rebuildable context artifacts, but M40c does not delete run artifacts.

## Failure Handling

Rebuild writes through a temporary database in the same work root and replaces
the target only after a successful transaction. A failed rebuild must leave the
previous `agentteam.db` untouched.

The rebuild path uses the normal rollback journal for the temporary database.
WAL can be enabled later for long-lived read paths, but M40a avoids WAL during
atomic replace so SQLite sidecar files cannot be separated from the final
database.

## Non-Goals

- no DB-primary artifact storage;
- no replacement of `events.jsonl`;
- no DB-primary `status` or `logs` authority;
- no automatic run artifact deletion in M40c;
- no live model calls in tests.

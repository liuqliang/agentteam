# AgentTeam Command Reference

This document is the operator reference for the `agentteam` command. It explains
what each command is for, when to use it, and which outputs or side effects to
expect.

For exact parser flags, run:

```bash
agentteam help
agentteam help <command>
agentteam <command> --help
```

## Mental Model

AgentTeam is normally launched from a target project repository. The target
repository owns `.agentteam/profile.json`; runtime data lives under the
profile's `work_root`, usually outside the repository.

The usual lifecycle is:

```bash
agentteam init --interactive
agentteam doctor
agentteam start
agentteam status
agentteam report
agentteam integrate --taskpack <taskpack-id>
agentteam next --from-taskpack <taskpack-id> --goal "continue with the next optimization"
agentteam pursue --goal "long-running optimization goal" --max-rounds 3
```

Use `--json` when another program needs structured output. Text output is kept
compact for terminal use.

## Command Groups

| Group | Commands | Purpose |
| --- | --- | --- |
| Project setup | `init`, `doctor`, `grounding`, `update`, `db`, `stats`, `gc` | Configure, inspect, and maintain the local AgentTeam installation for a project. |
| Run lifecycle | `start`, `next`, `queue`, `pursue`, `continue`, `stop`, `status`, `explain-status`, `watch`, `logs`, `report`, `paths` | Start work, inspect progress, stop safely, and understand completed runs. |
| Result integration | `integrate` | Merge verified integration-baseline changes back to the target repository. |
| Notification | `notify` | Test Feishu delivery or resend completion summaries. |
| Semantic feedback | `feedback` | Record implementation evidence as review-gated semantic feedback proposals. |
| Operator intervention | `resume`, `answer`, `permissions`, `chat` | Resolve manual gates, permission requests, or discuss a run with diagnostic context. |
| Taskpack management | `taskpack` | Draft, validate, freeze, list, and delete taskpacks. |
| Low-level runtime | `submit`, `run` | Lower-level commands used by scripts or advanced debugging. |

## Project Setup

### `agentteam init`

Creates or updates `.agentteam/profile.json` in the target repository.

Use it when:

- A repository has not been initialized for AgentTeam.
- You need to change the project key, `work_root`, runtime defaults, Feishu env
  variable names, or verification profile.

Common examples:

```bash
agentteam init --interactive
agentteam init --project-key verisilicon --work-root ~/.local/share/agentteam/verisilicon
agentteam init --verification-command-json '["python3", "-m", "unittest", "discover"]'
agentteam init --performance-command-json '["python3", "tools/bench.py", "--json"]' --metric latency_ms --metric accuracy
```

Important options:

- `--project-root`: target repository root. Defaults to current directory.
- `--project-key`: stable local project identifier.
- `--work-root`: where drafts, frozen taskpacks, runs, artifacts, and releases are stored.
- `--author-runtime`: taskpack author runtime, currently `fake` or `codex`.
- `--runtime`: worker runtime, currently `auto`, `fake`, or `codex`.
- `--verification-command-json`: correctness verification command recorded in the project profile.
- `--performance-command-json`: benchmark command recorded in the project profile.
- `--metric`: tracked metric name. Repeat for multiple metrics.
- `--feishu-webhook-env`: env var name that contains the Feishu webhook URL.
- `--force`: overwrite an existing profile.

Side effects:

- Writes `.agentteam/profile.json`.
- Adds `.agentteam/` to `.git/info/exclude` when possible.

### `agentteam doctor`

Checks whether the current project is ready to run AgentTeam.

Use it when:

- A new project was just initialized.
- A command fails and you want a quick environment diagnosis.
- You are unsure whether the profile, git repository, verification command, or
  Feishu settings are valid.

Examples:

```bash
agentteam doctor
agentteam doctor --project-root /path/to/repo --json
```

What it checks:

- The project root is inside a git repository.
- `.agentteam/profile.json` loads successfully.
- `work_root` exists or can be created later.
- The verification profile has a correctness command.
- Feishu webhook env configuration is present when enabled.
- The `codex` CLI is available on `PATH`.

Output status:

- `passed`: no failed checks.
- `failed`: at least one required check failed.
- Individual checks may be `passed`, `warning`, `failed`, or `skipped`.

### `agentteam grounding`

Summarizes the target repository before taskpack authoring or follow-up
planning.

Use it when:

- You want to see which languages, project tools, and test entrypoints the
  framework can detect.
- A broad optimization goal needs a lightweight repo-level grounding before
  decomposition.
- You want candidate verification commands without executing them.

Examples:

```bash
agentteam grounding
agentteam grounding --project-root /path/to/repo
agentteam grounding --project-root /path/to/repo --json
```

Behavior:

- Reads tracked files with `git ls-files`, with an `rg --files` fallback for
  nonstandard repositories.
- Detects common source languages, project tool files such as `pyproject.toml`,
  `package.json`, `Makefile`, `CMakeLists.txt`, `Cargo.toml`, `go.mod`, `pom.xml`,
  `build.gradle`, and `meson.build`.
- Reports test entrypoint hints from common test file names and `tests/`
  directories.
- Reports candidate verification commands such as `python3 -m unittest
  discover`, `npm test`, `make test`, `cargo test`, or `go test ./...`.
- Does not run candidate commands, install dependencies, start workers, write
  taskpacks, or mutate the target repository.

### `agentteam update`

Manages side-by-side AgentTeam runtime releases for a target project.

Use it when:

- You changed AgentTeam itself and need a target project to use the new runtime.
- You want to see the active release.
- You need to activate or roll back to a release.
- You want to prune old releases.

Examples:

```bash
agentteam update --status
agentteam update --from-git /home/liuql/projects/agentteam --ref native-runtime-m0
agentteam update --from-git https://github.com/liuqliang/agentteam.git --ref v0.1.3
agentteam update --from /home/liuql/projects/agentteam/.worktrees/native-runtime-m0 --release-id native-runtime-m0-<id>
agentteam update --activate native-runtime-m0-<id>
agentteam update --rollback native-runtime-m0-<older-id>
agentteam update --prune
```

Notes:

- `--from-git` installs from a local git repository or remote git URL at an
  explicit ref, resolves it to a commit, stores the code under the global
  runtime release cache, and activates a project-local pointer.
- `--from` installs from a clean AgentTeam checkout and activates the new release.
- Legacy `--from` installs prune old completed-run project-local releases by
  default, while protecting the active release and releases pinned by
  nonterminal runs.
- Git-backed releases are stored once under
  `~/.local/share/agentteam/runtime-releases/<source-key>/<release-id>/`; each
  project stores only refs, active release metadata, events, and run pins.
- Use `--status` to see `active_release`, `latest_installed_release`, and whether
  active is latest.

### `agentteam db`

Rebuilds or checks the project-level artifact projection database at
`<work_root>/agentteam.db`.

Use it when:

- You want faster project-level inspection in future DB-backed commands.
- You suspect the projection is stale or missing.
- You deleted `agentteam.db` and want to regenerate it from authoritative files.

Examples:

```bash
agentteam db rebuild
agentteam db check
agentteam db rebuild --project-root /path/to/repo --json
agentteam db check --project-root /path/to/repo --json
```

Important behavior:

- `agentteam.db` is a rebuildable projection. Frozen taskpacks, run
  directories, `events.jsonl`, reports, patches, and state snapshots remain the
  authoritative records.
- `rebuild` scans `frozen/` and `runs/`, writes a temporary database, then
  replaces the old projection only after a successful rebuild.
- `check` compares projected counts and artifact digest with a fresh file scan
  and reports mismatches such as stale event counts or changed artifact
  content. It does not mutate files.
- The projection indexes runs, taskpacks, events, tasks, compact evidence
  summaries, artifact hashes/sizes, and per-run token/stat aggregates.
- M60 read-through commands use the DB only when `agentteam db check` would
  report a fresh projection. Fresh reads report `projection_source` set to
  `db`, `projection_status` set to `fresh`, and `projection_db_path`.
- If the projection is missing, stale, corrupt, or unreadable, read-through
  commands fall back to authoritative files and report `projection_source` set
  to `files`, `projection_warning` set to `projection_db_unavailable`, and
  `next_action` set to `run agentteam db rebuild`. JSON payloads that expose
  operator hints may also include `operator_hint: agentteam db rebuild`.
- Operators can run commands normally, inspect `projection_source`, and run
  `agentteam db rebuild` only when a fallback warning asks for it. Correctness
  does not depend on manually maintaining `agentteam.db`.
- M60 does not delete artifacts, does not make `agentteam.db` authoritative,
  and does not add automatic rebuilds to read-only operator commands.

### `agentteam stats`

Shows compact project-level statistics from AgentTeam runtime artifacts.

Use it when:

- You want to know how many runs, taskpacks, events, tasks, and artifacts exist.
- You want an artifact footprint summary without reading long logs.
- You want a quick token usage summary across indexed runs.
- You want to see whether the command used a fresh DB projection or file scan.

Examples:

```bash
agentteam stats
agentteam stats --json
agentteam stats --project-root /path/to/repo --json
```

Behavior:

- Uses a fresh `<work_root>/agentteam.db` projection when available.
- Falls back to scanning authoritative files under `frozen/` and `runs/` when
  the DB is missing, stale, or unreadable.
- File-scan fallback includes `projection_warning: projection_db_unavailable`
  and `next_action: run agentteam db rebuild`.
- Does not rebuild the DB automatically and does not mutate runtime state.
- JSON output includes counts, evidence status counts, artifact type/retention
  summaries, artifact bytes, and aggregate token usage.

### `agentteam gc`

Cleans local AgentTeam storage for a project.

Use it when:

- Old runtime releases are accumulating under `work_root/releases`.
- Git-backed runtime releases are accumulating under the shared global release
  store.
- You want a cleanup entry point separate from update.
- You want to repair stale run state with `--stale-runs`.

Examples:

```bash
agentteam gc
agentteam gc --global-releases
agentteam gc --force
agentteam gc --keep-releases 2 --force
agentteam gc --global-releases --force
agentteam gc --stale-runs --force
agentteam gc --artifacts --json
agentteam gc --artifacts --artifact-limit 50 --json
agentteam gc --artifacts --delete-artifacts --force --json
```

Behavior:

- Without `--force`, reports dry-run cleanup metadata and does not delete releases.
- With `--force`, deletes eligible old releases through the release manager.
- Keeps the configured number of latest releases plus protected active or
  nonterminal-run releases.
- If `<work_root>/agentteam.db` is fresh, dry-run output also includes an
  `artifact_projection` summary: artifact count, bytes, artifact types,
  retention policy counts, token usage rows, and explanations for
  authoritative versus rebuildable artifacts.
- With `--artifacts`, includes an `artifact_retention_plan` section. It lists
  bounded rebuildable artifact candidates such as role/repo context files and
  reports why authoritative artifacts remain protected.
- `--artifact-limit` controls how many rebuildable candidate rows are included.
  It does not change counts, and it bounds which rebuildable candidates can be
  deleted in one guarded cleanup command.
- `--delete-artifacts` requires both `--artifacts` and `--force`. It deletes
  only listed candidates whose retention policy is `rebuildable` and whose
  size/hash validation status is `passed`.
- Authoritative artifacts are protected and are never deletion candidates:
  `events.jsonl`, reports, state snapshots, patches, frozen taskpacks, and
  integration evidence stay on disk.
- Retention plans validate rebuildable candidate files against the projection
  row before deletion. JSON includes `validation_status`,
  `validated_candidate_count`, `invalid_candidate_count`, per-candidate
  size/hash validation details, and `artifact_deletion` when deletion runs.
- After deleting rebuildable artifacts, run `agentteam db rebuild`; the existing
  projection still describes files that were intentionally removed.
- If the projection DB is missing or stale, the artifact retention plan reports
  `projection_warning: projection_db_unavailable` and
  `next_action: run agentteam db rebuild`.
- With `--global-releases`, also scans
  `~/.local/share/agentteam/runtime-releases/<source-key>/<release-id>/`.
  Global releases are protected when any known work root references them through
  `active.json`, `releases/refs/*.json`, or a nonterminal run pin.
- Global release deletion is explicit: `agentteam gc --global-releases` only
  explains protected and deletable releases; `--force` is required to delete
  orphaned global release roots.

## Run Lifecycle

### `agentteam start`

Authors a taskpack from a human goal, freezes it, and runs it.

Use it when:

- You want AgentTeam to turn a goal into bounded work and execute it.
- You are starting a new independent task for the current project.

Examples:

```bash
agentteam start
agentteam start --goal "read this competition repository and optimize algorithm accuracy and latency"
agentteam start --goal "profile algorithm latency by module" --taskpack-id algo-module-latency-profile
agentteam start --goal "optimize this repo" --json
```

Important options:

- `--goal`: human-readable goal. If omitted, the CLI prompts for it.
- `--taskpack-id`: stable taskpack/run id slug.
- `--author-runtime`: override profile author runtime.
- `--runtime`: override profile worker runtime.
- `--max-inflight`: maximum daemon inflight attempts.
- `--max-attempts`: maximum attempts per task.
- `--commit-verified-integration`: commit integration worktree changes after verification passes.
- `--notification-project` and Feishu env options: override notification config.

Output:

- Text mode prints a compact completion summary.
- JSON mode prints the full execution result.
- Run details are stored under `work_root/runs/<taskpack-id>`.

### `agentteam next`

Creates and runs a follow-up taskpack from a previous run.

Use it when:

- A completed run produced useful context and you want to continue from it.
- You want to assign a new goal while preserving source run context.

Examples:

```bash
agentteam next --from-taskpack algo-module-latency-profile --goal "optimize the slowest module found in the report"
agentteam next --goal "continue from the latest run and propose the next safe optimization"
agentteam next --from-run-dir ~/.local/share/agentteam/verisilicon/runs/algo-module-latency-profile --json
```

Notes:

- If `--from-taskpack` and `--from-run-dir` are omitted, it uses the latest run.
- The follow-up author sees the previous report/context and should produce a new
  taskpack rather than mutating the old one.
- Completion reports may include a `follow_up_recommendation` with an
  `agentteam next --from-taskpack ... --goal ...` command. Treat it as an
  operator-facing suggestion, not an automatic start.

### `agentteam queue`

Inspects the suggested follow-up queue for a completed run.

Use it when:

- A run or pursue loop completed and you want to see the next bounded task
  before launching another worker.
- You want a compact `agentteam next ...` command derived from the latest report
  and goal memory.

Examples:

```bash
agentteam queue show --taskpack <taskpack-id>
agentteam queue show --taskpack <taskpack-id> --json
agentteam queue next --taskpack <taskpack-id>
agentteam queue next --run-dir ~/.local/share/agentteam/verisilicon/runs/<taskpack-id>
```

Notes:

- `queue show` and `queue next` are read-only. They do not draft a taskpack,
  start workers, merge code, or mutate run artifacts.
- When `--run-dir` is supplied, the run directory determines the work root.
  This lets you inspect historical runs from outside the original project
  directory. If the current project has a profile, AgentTeam keeps profile
  metadata such as the project key but does not use that profile's work root.
- Queue items are built from structured report `next_steps`,
  `follow_up_recommendation`, and long-goal `goal_memory.follow_up_queue` when
  available.
- `agentteam pursue` consumes the same queue summary between rounds. The queue
  command is the read-only way to inspect what the pursue loop would use next.
- `queue next` prints the next suggested goal and command plus the selected
  item's provenance and readiness: source kind, source taskpack, source report,
  `selected_readiness`, evidence blockers when present, and the structured
  verification line that should guide the next task. Run the printed
  `agentteam next --from-taskpack ... --goal ...` command when you want to
  launch the next taskpack.
- JSON output includes the same selected item as `selected_item`; it is derived
  from existing report or goal-memory fields and does not mutate the run.

### `agentteam pursue`

Runs a bounded long-goal loop across ordinary taskpacks.

Use it when:

- You want AgentTeam to keep working through `start` and follow-up rounds within
  an explicit budget.
- You want the runtime to stop automatically at operator gates instead of asking
  you to launch each safe round manually.

Examples:

```bash
agentteam pursue --goal "持续优化这个比赛仓库的准确率和延迟" --max-rounds 3
agentteam pursue --goal "持续优化这个比赛仓库的准确率和延迟" --max-rounds 3 --json
agentteam pursue --goal "continue optimization" --max-rounds 2 --allow-review-gate-follow-up
```

Notes:

- Each round is still a normal taskpack/run with normal reports and artifacts.
- The default stop policy halts at manual gates, permission requests, blockers,
  failed runs, and integration/source review gates.
- `--allow-review-gate-follow-up` lets the loop author another follow-up from
  the previous report, but it still does not merge source changes or bypass
  operator review.
- Between successful rounds, `pursue` builds the same `follow_up_queue.v1`
  summary used by `agentteam queue`, records the compact queue selection in the
  pursue recap, and uses the selected `next_goal` for the next round.
- `--max-rounds` is a hard budget. The command never runs indefinitely.
  When the loop stops because the budget is reached, the next action points to
  `agentteam queue next --taskpack <latest>` so you can inspect the next
  bounded continuation before launching more work.

#### Review Gate Sequence

When `agentteam pursue` stops with `review_gate_required`, treat the source
branch as unchanged until you explicitly accept the result. Use this sequence:

```bash
agentteam report --taskpack <taskpack-id>
agentteam paths --taskpack <taskpack-id>
git -C <integration-baseline-worktree> diff --stat <base-head>..<baseline-head>
agentteam integrate --taskpack <taskpack-id>
agentteam next --from-taskpack <taskpack-id> --goal "<accepted follow-up goal>"
```

The first three commands are read-only inspection steps: review the final report,
locate the integration baseline, and inspect the diff without merging into the
target branch. Run `agentteam integrate` only after accepting the report and
diff. After integration, use `agentteam next` or `agentteam pursue` only if more
work is still desired. Older run artifacts that do not include `base_sha` may
show a fallback diff command using `<baseline-head>..HEAD`.

For AgentTeam-as-target work, the review gate is mandatory: workers may prepare
patches, reports, evidence, and integration baselines, but source merge, push,
and release activation remain operator decisions.

### AgentTeam-As-Target Work

AgentTeam can be the target repository for ordinary implementation work. Use the
same `start` and `next` commands with a functional or semantic goal; there is no
dedicated `self-improve` command.

When the target repository is AgentTeam itself, generated taskpacks and reports
must keep source merge, push, and release activation under operator review. The
worker may produce patches, evidence, and verification results, but the operator
reviews before integrating those changes into the source branch.

### `agentteam feedback`

Creates or lists semantic feedback proposal artifacts.

Use it when:

- Implementation evidence shows that a semantic architecture, design, or SOP
  document may need clarification.
- You want to record the evidence without letting an implementation worker edit
  authority documents directly.

Examples:

```bash
agentteam feedback propose --taskpack <taskpack-id> \
  --proposal-id design-gap-1 \
  --target-artifact design/system.md \
  --summary "实现证据显示系统边界需要补充" \
  --rationale "worker 在实现阶段发现 review gate 归属不清"
agentteam feedback list
agentteam feedback list --json
```

Notes:

- Proposals are written under `<work_root>/semantic_feedback/`.
- Proposal status starts as `pending_review`.
- Proposal artifacts include source taskpack/report paths, target artifacts,
  summary, rationale, and an explicit authority boundary.
- The command does not modify design authority documents, roadmap files,
  source code, taskpacks, or run reports.

### `agentteam continue`

Continues an existing frozen taskpack run.

Use it when:

- A previous `start` or `run` stopped before fully finishing.
- You want to resume an existing taskpack instead of authoring a new one.

Examples:

```bash
agentteam continue --taskpack <taskpack-id>
agentteam continue --run-dir <run-dir>
agentteam continue --taskpack <taskpack-id> --json
```

Notes:

- `continue` does not create a new taskpack.
- It uses the selected run directory and existing frozen taskpack state.

### `agentteam stop`

Stops or repairs an existing run safely.

Use it when:

- A run is occupying a terminal and you want it to stop gracefully.
- A taskpack authoring process is still active.
- Status shows stale running state but the registered process is gone.

Examples:

```bash
agentteam stop
agentteam stop --taskpack <taskpack-id>
agentteam stop --authoring
agentteam stop --stale
agentteam stop --force
```

Behavior:

- Runtime stop signals registered worker PIDs and owned descendants.
- `--authoring` stops the latest live Codex taskpack author recorded under
  `work_root/drafts/.<taskpack-id>-author/author_state.json`.
- `--stale` repairs stale state without terminating live processes.
- `--force` sends SIGKILL if registered PIDs do not exit after the grace period.

### `agentteam status`

Shows the latest run state.

Use it when:

- You need to know whether AgentTeam is running, idle, blocked, or waiting.
- You need the latest run id, run directory, worker counts, liveness, integration
  state, token usage, manual gates, or permission requests.

Examples:

```bash
agentteam status
agentteam status --run-dir <run-dir>
agentteam status --json
```

When `--run-dir` is supplied, `status` can summarize that run even if the
current directory has no `.agentteam/profile.json`. If a project profile is
available, its project key is retained, but the run directory still determines
the work root used for projection DB checks and related artifacts.

Text output includes:

- `overall_status`
- `run_status`
- `liveness`
- task counts
- integration blocked count
- integration baseline branch/head
- token usage when available
- inflight/manual gate/permission request counts
- waiting permission request details, including request id, capability, reason,
  and `agentteam permissions approve/deny` commands when applicable
- worker summary
- run directory

When `<work_root>/agentteam.db` exists and is fresh, status output may replay
events from the projection database and reports `projection_source: db`. Live
process/liveness, worker registry, and scheduler state are still read from
files so current execution state stays accurate. If the projection is missing,
stale, corrupt, or unreadable, status falls back to `events.jsonl` and reports
`projection_source` set to `files`, `projection_warning` set to
`projection_db_unavailable`, and `next_action` set to
`run agentteam db rebuild`.

### `agentteam explain-status`

Turns status into a short natural-language explanation and next action.

Use it when:

- `status` is technically correct but you want to know what it means.
- You want a compact operator-facing answer such as "paused for permission" or
  "idle; review the report or start a follow-up."

Examples:

```bash
agentteam explain-status
agentteam explain-status --taskpack <taskpack-id>
agentteam explain-status --json
```

### `agentteam watch`

Prints compact progress lines while a run is active.

Use it when:

- You want a lightweight terminal view of state changes.
- You do not want full worker logs.

Examples:

```bash
agentteam watch
agentteam watch --max-lines 20
agentteam watch --interval 5
agentteam watch --json-lines
```

Notes:

- `watch` is read-only.
- It stops when the run reaches a terminal or idle state.

### `agentteam logs`

Tails compact event records from `events.jsonl`.

Use it when:

- You want recent runtime events without reading full files.
- You need to inspect why the state changed.

Examples:

```bash
agentteam logs
agentteam logs --taskpack <taskpack-id> --lines 10
agentteam logs --run-dir <run-dir> --json
```

When `--run-dir` is supplied, `logs` follows the same profileless run-directory
resolution as `status`.

Text output shows the run id, returned event count, run directory, and compact
event lines.

When `<work_root>/agentteam.db` exists and is fresh, logs may read events from
the projection database and reports `projection_source: db`. If the projection
is missing, stale, corrupt, or unreadable, the command falls back to
`events.jsonl` and reports `projection_source` set to `files`,
`projection_warning` set to `projection_db_unavailable`, and `next_action` set
to `run agentteam db rebuild`.

### `agentteam report`

Renders a human-readable run completion report.

Use it when:

- A run completed or became blocked and you need to know what changed.
- You need the natural-language work summary, verification status, integration
  status, merge recommendation, token usage, and next steps.
- You want a Chinese operator-facing digest that says what changed, which files
  changed, what was verified, measured results if reported, merge guidance, and
  suggested follow-up.

Examples:

```bash
agentteam report
agentteam report --taskpack <taskpack-id>
agentteam report --run-dir <run-dir>
agentteam report --json
```

When `--run-dir` is supplied, `report` follows the same profileless
run-directory resolution as `status`.

Side effects:

- Writes report artifacts under the run's report/artifact area.
- Does not change the target repository.

When `<work_root>/agentteam.db` exists and is fresh, JSON output includes
projected run/report metadata such as the indexed `report_path` and marks the
projection source as `db`. Report content is still generated from authoritative
run files. If the projection is missing, stale, corrupt, or unreadable, JSON
output marks the projection source as `files`, includes the projection warning,
and suggests `agentteam db rebuild`.

Completion summaries include:

- `chinese_operator_brief`: compact Chinese scan summary.
- `operator_digest`: deterministic Chinese work report built only from
  structured fields. Multi-task runs aggregate unique task-level changes,
  changed files, verification evidence, measured results, merge guidance, and
  next steps into bounded Chinese lines instead of showing only the first task.
- `changed_files_note`: when every task explicitly reports `changed_files: []`
  and declares itself as a no-code investigation, audit, review, or planning
  task, the summary records that no source files changed instead of treating
  the empty change list as an evidence gap. Missing `changed_files` fields are
  still reported as evidence gaps.
- `follow_up_recommendation`: suggested `integrate`, `next`, or blocker-review
  action with command text when the structured report supports it.
- `review_gate`: concise review-gate guidance when accepted changes are waiting
  in an integration baseline. It includes the integration branch, baseline head,
  integration worktree, read-only report/paths/diff commands, and the explicit
  `agentteam integrate` acceptance command.

### `agentteam paths`

Shows the important local paths for a project or run.

Use it when:

- You need to locate `work_root`, drafts, frozen taskpacks, runs, artifacts, or
  the integration baseline worktree.
- You want the read-only report, paths, and diff commands for a run waiting at
  the review gate before deciding whether to integrate.

Examples:

```bash
agentteam paths
agentteam paths --taskpack <taskpack-id>
agentteam paths --json
```

Text output includes `review_report`, `review_paths`, and `review_diff` when an
integration baseline exists. These are inspection commands only. It also prints
`review_integrate` as the explicit acceptance command; do not run it until the
report and diff have been reviewed. JSON output keeps the same split in
`read_only_review_commands` and `accept_command`, while `review_commands`
contains the complete command set for compact clients.

## Result Integration

### `agentteam integrate`

Fast-forwards a verified integration baseline into the current target repository
branch.

Use it when:

- A run has completed and produced accepted changes in its integration baseline.
- You reviewed the report and want to bring the result into the project branch.

Examples:

```bash
agentteam integrate --taskpack <taskpack-id>
agentteam integrate --taskpack <taskpack-id> --rebase
agentteam integrate --run-dir <run-dir> --json
```

Requirements:

- The target repository must be clean.
- The selected run must be idle or completed.
- The integration baseline branch must exist.

Behavior:

- Without `--rebase`, only a fast-forward merge is allowed.
- With `--rebase`, AgentTeam rebases the integration baseline onto current
  target `HEAD`, then fast-forwards if the rebase succeeds.
- On conflict, it aborts the rebase, reports conflicted files, and leaves the
  target repository unchanged.

## Notification

### `agentteam notify test`

Sends or dry-runs one diagnostic Feishu notification.

Use it when:

- You just configured Feishu.
- You want to confirm the webhook env var is readable and delivery works.

Examples:

```bash
agentteam notify test
agentteam notify test --dry-run --json
agentteam notify test --message "AgentTeam notification check"
```

### `agentteam notify diagnose`

Checks Feishu notification delivery variants without exposing webhook secrets.

Use it when:

- `notify test` or a run-completed notification fails.
- You want to compare the normal rich text payload with the concise text
  fallback payload.
- You want a dry-run summary of payload construction before sending anything.

Examples:

```bash
agentteam notify diagnose --dry-run --json
agentteam notify diagnose --message "AgentTeam delivery diagnosis"
```

The command prints the webhook env var name, whether the env value is present,
signing status, and one row each for `rich_text` and `concise_text`. It does
not print the webhook URL, hook token, or signing secret.

### `agentteam notify run-completed`

Sends or resends a completion summary for an existing run.

Use it when:

- A run completed but Feishu did not receive the message.
- You want to send the report summary without rerunning the task.
- You want the same bounded Chinese multi-task operator digest that
  `agentteam report` renders, without reading the full terminal log.
- You want completion notifications to include worker diagnostics, including
  pool diagnostic status, diagnostic-state counts, and notable stale or failed
  workers when the run recorded them.

Examples:

```bash
agentteam notify run-completed --taskpack <taskpack-id>
agentteam notify run-completed --run-dir <run-dir> --dry-run --json
```

## Operator Intervention

### `agentteam resume`

Interactively answers waiting manual gates.

Use it when:

- `status` says `manual_gate_required`.
- A worker or scheduler needs operator input before continuing.

Examples:

```bash
agentteam resume --run-dir <run-dir> --interactive
agentteam resume --run-dir <run-dir> --list
```

Interactive commands include `/gates`, `/task`, `/why`, `/events`,
`/context`, `/answer <text>`, and `/help`.

### `agentteam answer`

Answers one manual gate directly by question id.

Use it when:

- You already know the exact `question_id` and answer.

Example:

```bash
agentteam answer --run-dir <run-dir> --question-id <id> --answer "Use option A."
```

### `agentteam permissions`

Lists, approves, or denies runtime permission requests.

Use it when:

- A worker hit a sandbox or permission boundary and the runtime paused.
- You need to approve a bounded retry or explicitly deny the request.

Examples:

```bash
agentteam permissions list --run-dir <run-dir>
agentteam permissions approve --run-dir <run-dir> --request-id <id> --reason "Allow benchmark command."
agentteam permissions deny --run-dir <run-dir> --request-id <id> --reason "Outside task scope."
```

Subcommands:

- `list`: show waiting permission requests.
- `approve`: record approval and allow the runtime to continue.
- `deny`: record denial and keep the task blocked.

### `agentteam chat`

Builds a read-only diagnostic context for discussing a run, optionally launching
Codex interactively.

Use it when:

- You want to discuss why a run failed or what a patch did.
- You need context for integration failures, permission issues, or patch review.

Examples:

```bash
agentteam chat --taskpack <taskpack-id>
agentteam chat --run-dir <run-dir> --topic integration-failure
agentteam chat --run-dir <run-dir> --interactive
```

Notes:

- By default, it prints the diagnostic context and does not launch a model.
- `--interactive` starts Codex with the same read-only context.

## Taskpack Management

### `agentteam taskpack new`

Creates an explicit operator-authored taskpack from profile defaults.

Use it when:

- You already know the task and do not need Codex authoring.
- You want tighter control over read/write scope and verification.

Examples:

```bash
agentteam taskpack new --goal "profile algorithm latency by module" --write-scope output/current/
agentteam taskpack new --goal "profile algorithm latency by module" --read-scope . --write-scope output/current/ --freeze
agentteam taskpack new --goal "run benchmark sweep" --verification-command-json '["python3", "tools/check.py"]' --freeze
```

Important options:

- `--read-scope`: repository-relative scope the worker may inspect. Repeatable.
- `--write-scope`: repository-relative scope the worker may modify. Repeatable.
- `--verification-command-json`: verification command for the taskpack.
- `--allow-merge`: set `policy.allow_merge`.
- `--freeze`: freeze immediately after validation.

### `agentteam taskpack draft`

Drafts a taskpack from a goal without running it.

Use it when:

- You want to inspect or validate a Codex-authored taskpack before freezing.
- You want Codex authoring to turn a roadmap-derived follow-up goal into a
  bounded taskpack with evidence paths, non-goals, success metrics or an
  explicit no-metric-delta rationale, verification guidance, and review-gate
  constraints.
- Codex authoring uses a direct artifact-production prompt: it asks the model
  to write the five required files before optional exploration and avoid
  external planning workflows. Timeout results include required-file counts,
  missing files, largest output stream, and a compact next action.
- Non-timeout Codex author failures also report compact required-file
  diagnostics and the `author_result.json` / `author_state.json` paths instead
  of printing raw model output.
- Codex authoring writes `required_file_templates.json` under the author
  context directory and points the prompt at it. The bundle is a scaffold for
  the five required files; it is not placed in the taskpack directory and does
  not make an empty draft valid.
- Author stdout/stderr are spooled to `author_stdout.log` and
  `author_stderr.log` in the author context directory. `author_result.json` and
  `author_state.json` keep paths, byte counts, and bounded excerpts instead of
  embedding full raw streams.
- If Codex times out after writing all five required files, AgentTeam attempts
  a safe salvage: it canonicalizes the draft, applies the verification profile,
  and accepts the draft only when `validate_taskpack` passes. Accepted salvages
  are recorded as `accepted_after_timeout` in `author_result.json` and
  `author_state.json`.

Example:

```bash
agentteam taskpack draft --project-root . --goal "optimize parser latency" --draft-root /tmp/parser-taskpack --author-runtime codex
```

### `agentteam taskpack validate`

Validates a draft or frozen taskpack directory.

Example:

```bash
agentteam taskpack validate /path/to/taskpack
```

### `agentteam taskpack freeze`

Freezes an accepted draft taskpack into a frozen taskpack directory.

Example:

```bash
agentteam taskpack freeze /path/to/draft --frozen-root ~/.local/share/agentteam/project/frozen
```

### `agentteam taskpack materialize`

Converts a deterministic semantic skeleton into an executable taskpack.

Use it when:

- A mechanical skeleton preserved deterministic context, but marked
  `semantic_authoring_required`.
- A semantic author has filled in the concrete objective, goal alignment,
  read/write scopes, deliverables, verification command, and evidence paths.
- You want the resulting taskpack to enter the normal freeze/run path.

Example semantic file:

```json
{
  "objective": "Implement the bounded parser cache fix described by the source report.",
  "goal_alignment": "Uses source_report_path evidence to select one bounded implementation change.",
  "read_scope": ["src/", "tests/"],
  "write_scope": ["src/parser.py", "tests/test_parser.py"],
  "work_type": "code_implementation",
  "required_deliverables": [
    "implemented_changes_or_no_safe_change_rationale",
    "verification_summary",
    "recommended_next_implementation_tasks"
  ],
  "verification_command": ["python3", "-m", "unittest", "discover"],
  "evidence_paths": ["/path/to/source/report.md"]
}
```

Example:

```bash
agentteam taskpack materialize /path/to/skeleton \
  --semantic-json-file semantic.json \
  --output-root ~/.local/share/agentteam/project/drafts \
  --taskpack-id parser-cache-fix \
  --freeze \
  --frozen-root ~/.local/share/agentteam/project/frozen
```

Notes:

- `validate` and `freeze` can accept a semantic skeleton as an authoring
  artifact, but `run`, `continue`, `start`, and `next` will reject it before
  launch while `semantic_authoring_required` is present.
- Materialization clears the semantic blocker only after the required semantic
  fields are supplied and the resulting taskpack validates.

### `agentteam taskpack list`

Lists frozen taskpacks for a project, including liveness-aware run status.

Examples:

```bash
agentteam taskpack list
agentteam taskpack list --json
```

When `<work_root>/agentteam.db` exists and `agentteam db check` would pass,
JSON and text output may read frozen taskpack rows from the projection database
and reports `projection_source: db`. Run liveness is still checked from live
run files so stale/running state remains accurate. If the projection is
missing, stale, corrupt, or unreadable, the command falls back to file scanning
and reports `projection_source` set to `files`, `projection_warning` set to
`projection_db_unavailable`, and `next_action` set to
`run agentteam db rebuild`.

### `agentteam taskpack delete`

Deletes draft/frozen taskpack files, and optionally the run directory.

Examples:

```bash
agentteam taskpack delete --taskpack <id> --dry-run
agentteam taskpack delete --taskpack <id> --force
agentteam taskpack delete --taskpack <id> --delete-run --force
```

Safety rules:

- `--force` is required for actual deletion.
- If a run directory exists, deletion requires both `--delete-run` and `--force`.
- Use `--dry-run` first when unsure.

## Low-Level Runtime Commands

### `agentteam submit`

Lower-level command that drafts, freezes, and runs in one flow.

Use it for:

- Scripted flows.
- Debugging the combined draft/freeze/run pipeline.

Example:

```bash
agentteam submit --interactive
```

Most users should prefer `agentteam start`.

### `agentteam run`

Runs an already frozen taskpack directory.

Use it for:

- Runtime debugging.
- Running a frozen taskpack produced by another command.

Example:

```bash
agentteam run <frozen-taskpack-dir> --run-root <runs-dir>
agentteam run <frozen-taskpack-dir> --run-root <runs-dir> --json
```

Most users should prefer `agentteam start` or `agentteam continue`.

## Practical Recipes

### Initialize A New Project

```bash
cd /path/to/project
agentteam init --interactive
agentteam doctor
```

### Start An Optimization Task

```bash
agentteam start --goal "read this repository and optimize algorithm accuracy and latency"
agentteam status
agentteam report
```

### See Whether A Run Is Still Working

```bash
agentteam status
agentteam explain-status
agentteam logs --lines 10
```

### Stop A Run Without Killing Random Processes

```bash
agentteam stop --taskpack <taskpack-id>
agentteam status
```

### Recover From A Missed Completion Notification

```bash
agentteam report --taskpack <taskpack-id>
agentteam notify run-completed --taskpack <taskpack-id>
```

### Merge Verified Work Back To The Target Repository

```bash
git status --short
agentteam report --taskpack <taskpack-id>
agentteam integrate --taskpack <taskpack-id>
```

If the target branch moved after the run:

```bash
agentteam integrate --taskpack <taskpack-id> --rebase
```

### Continue From A Completed Run

```bash
agentteam next --from-taskpack <taskpack-id> --goal "continue with the next safe optimization from the report"
```

### Clean Old Runtime Releases

```bash
agentteam update --status
agentteam gc
agentteam gc --global-releases
agentteam gc --force
agentteam gc --global-releases --force
```

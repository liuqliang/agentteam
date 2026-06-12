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
```

Use `--json` when another program needs structured output. Text output is kept
compact for terminal use.

## Command Groups

| Group | Commands | Purpose |
| --- | --- | --- |
| Project setup | `init`, `doctor`, `update`, `db`, `gc` | Configure and maintain the local AgentTeam installation for a project. |
| Run lifecycle | `start`, `next`, `continue`, `stop`, `status`, `explain-status`, `watch`, `logs`, `report`, `paths` | Start work, inspect progress, stop safely, and understand completed runs. |
| Result integration | `integrate` | Merge verified integration-baseline changes back to the target repository. |
| Notification | `notify` | Test Feishu delivery or resend completion summaries. |
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
- Artifact projection output is explanatory in M40c. `agentteam gc` does not
  delete run artifacts or context artifacts yet.
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

Text output includes:

- `overall_status`
- `run_status`
- `liveness`
- task counts
- integration blocked count
- integration baseline branch/head
- token usage when available
- inflight/manual gate/permission request counts
- worker summary
- run directory

When `<work_root>/agentteam.db` exists and is fresh, status JSON may replay
events from the projection database. Live process/liveness, worker registry,
and scheduler state are still read from files so current execution state stays
accurate. If the projection is missing or stale, status falls back to
`events.jsonl`.

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

Text output shows the run id, returned event count, run directory, and compact
event lines.

When `<work_root>/agentteam.db` exists and is fresh, logs may read events from
the projection database. If the projection is missing, stale, or unreadable,
the command falls back to `events.jsonl`.

### `agentteam report`

Renders a human-readable run completion report.

Use it when:

- A run completed or became blocked and you need to know what changed.
- You need the natural-language work summary, verification status, integration
  status, merge recommendation, token usage, and next steps.

Examples:

```bash
agentteam report
agentteam report --taskpack <taskpack-id>
agentteam report --run-dir <run-dir>
agentteam report --json
```

Side effects:

- Writes report artifacts under the run's report/artifact area.
- Does not change the target repository.

When `<work_root>/agentteam.db` exists and is fresh, JSON output includes
projected run/report metadata such as the indexed `report_path`. Report content
is still generated from authoritative run files. If the projection is missing
or stale, JSON output marks the projection source as `files`.

### `agentteam paths`

Shows the important local paths for a project or run.

Use it when:

- You need to locate `work_root`, drafts, frozen taskpacks, runs, artifacts, or
  the integration baseline worktree.

Examples:

```bash
agentteam paths
agentteam paths --taskpack <taskpack-id>
agentteam paths --json
```

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

Sends or dry-runs a diagnostic Feishu notification.

Use it when:

- You just configured Feishu.
- You want to confirm the webhook env var is readable and delivery works.

Examples:

```bash
agentteam notify test
agentteam notify test --dry-run --json
agentteam notify test --message "AgentTeam notification check"
```

### `agentteam notify run-completed`

Sends or resends a completion summary for an existing run.

Use it when:

- A run completed but Feishu did not receive the message.
- You want to send the report summary without rerunning the task.

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

### `agentteam taskpack list`

Lists frozen taskpacks for a project, including liveness-aware run status.

Examples:

```bash
agentteam taskpack list
agentteam taskpack list --json
```

When `<work_root>/agentteam.db` exists and `agentteam db check` would pass,
JSON output may read frozen taskpack rows from the projection database. Run
liveness is still checked from live run files so stale/running state remains
accurate. If the projection is missing or stale, the command falls back to file
scanning.

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

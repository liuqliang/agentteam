# Native AgentTeam Runtime Experiment

Status: isolated experiment, not current SOP authority.

This directory explores a native AgentTeam runtime where role agents are
long-lived actors managed by AgentTeam, not temporary Codex subagents.

The experiment keeps the current `design/` SOP stable. If this runtime model
works, its results can later be promoted into the design documents through the
normal artifact update path.

## Goal

Validate this model:

```text
long-lived role agent
  = stable identity
  + durable state
  + mailbox
  + event subscriptions
  + runtime adapter
```

Codex remains useful as a runtime backend for model calls, tools, MCP, sandbox,
and command execution. It should not define AgentTeam's long-term agent
lifecycle.

## Non-Goals

- Do not replace the current autonomous implementation SOP.
- Do not depend on Codex `spawn_agent` as the primary long-lived agent model.
- Do not implement distributed execution in M0.
- Do not let role agents write central authority artifacts directly.
- Do not treat natural-language agent output as validated result state.

## Directory Layout

```text
experiments/native_agentteam_runtime/
  README.md
  runtime_model.md
  m0_experiment_plan.md
  m0_runtime/
    agentteam_runtime/
      agentteam.py
      profile.py
      taskpack.py
      taskpack_author.py
  schemas/
    agent_pool.schema.json
    agent_state.schema.json
    mailbox_message.schema.json
    event.schema.json
  fixtures/
    sample_agent_pool.json
    sample_backlog.json
    sample_mailbox_message.json
    sample_events.jsonl
```

## M0 Scope

M0 validates the scheduling model with files only:

- a scheduler loop can read agent state, backlog, and events;
- role agents can be represented by durable `agent_state` records;
- mailbox messages can wake role agents without relying on chat context;
- events can reconstruct what happened;
- a Codex runtime adapter can be selected by runtime profile;
- a taskpack can be drafted, validated, frozen, and launched through the
  existing Python runtime CLI.

The current implementation is still an experiment, but it now has a thin
repository launcher at the repo root:

```bash
./agentteam --help
```

For repeated local use, install a user-level command:

```bash
./scripts/install-local.sh
export PATH="$HOME/.local/bin:$PATH"
```

The installer copies a stable launcher to `~/.local/bin/agentteam` and records
the development checkout path in `~/.local/share/agentteam/launcher.json`. For
projects with an active versioned release, the launcher reads the project
profile and loads that release runtime before falling back to the development
checkout. It does not write project profiles or secrets.

## Taskpack Authoring Flow

The taskpack path turns a target repository plus a human goal into runtime
artifacts that the scheduler can consume.

For the normal operator path, run AgentTeam from the target project. The first
run creates a project-local `.agentteam/profile.json`; later runs reuse it:

```bash
cd /path/to/repo
agentteam init --interactive
agentteam start
```

The profile belongs to the target project, not this AgentTeam framework
repository. It stores non-secret defaults such as `work_root`,
`author_runtime`, `default_runtime`, and Feishu environment variable names.
Runtime artifacts are written under the configured `work_root`, typically
`~/.local/share/agentteam/<project-key>/`.

For a scripted local smoke run without installing the command, use the launcher
directly:

```bash
./agentteam init \
  --project-root /path/to/repo \
  --project-key example \
  --work-root /tmp/agentteam-taskpacks \
  --author-runtime fake \
  --runtime auto \
  --one-shot

./agentteam start \
  --project-root /path/to/repo \
  --goal "optimize the target behavior under an explicit metric" \
  --taskpack-id example-taskpack \
  --one-shot
```

Interactive prompts are written to stderr, so stdout remains the final JSON
summary. `start` loads `.agentteam/profile.json`, prompts for `--goal` when it
is omitted, and then reuses the existing `submit` implementation.

`default_runtime: auto` is the default. It runs fake-authored taskpacks with the
`fake` runtime for smoke tests, and Codex-authored taskpacks with the `codex`
runtime for live work. Use `--runtime fake` or `--runtime codex` to make that
choice explicit.

The lower-level module CLI remains available for development and debugging:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam submit --interactive
```

To see the command map and choose the right operator command, run:

```bash
agentteam help
agentteam help stop
agentteam help taskpack
```

For explicit review before execution, run the lower-level commands separately:

1. Draft a taskpack:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam taskpack draft \
  --project-root /path/to/repo \
  --goal "optimize the target behavior under an explicit metric" \
  --draft-root /tmp/agentteam-taskpacks/drafts \
  --taskpack-id example-taskpack \
  --author-runtime fake
```

2. Validate the draft:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam taskpack validate \
  /tmp/agentteam-taskpacks/drafts/example-taskpack
```

3. Freeze the accepted draft:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam taskpack freeze \
  /tmp/agentteam-taskpacks/drafts/example-taskpack \
  --frozen-root /tmp/agentteam-taskpacks/frozen
```

4. Run the frozen taskpack:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam run \
  /tmp/agentteam-taskpacks/frozen/example-taskpack \
  --run-root /tmp/agentteam-runs \
  --one-shot
```

Successful `submit` and `taskpack` commands print JSON to stdout. Draft,
validation, freeze, and pre-launch failures print JSON to stderr and exit `1`.
After pre-launch translation succeeds, `run` delegates to `agentteam_runtime.cli`
and forwards that child process's stdout, stderr, and exit code. `submit`
captures the delegated run output and includes it in its JSON summary.

## Operator Control

Use these commands from the target project or pass `--project-root` explicitly:

```bash
agentteam status --project-root /path/to/repo
agentteam watch --project-root /path/to/repo --max-lines 20
agentteam stop --project-root /path/to/repo
agentteam stop --project-root /path/to/repo --stale
agentteam continue --project-root /path/to/repo --taskpack example-taskpack
```

`status` and `taskpack list` are liveness-aware. A raw scheduler state of
`running` is reported as `running-alive` only when a registered worker PID is
still alive; otherwise it is `running-stale`. `start` and `continue` keep stdout
reserved for the final JSON result and print compact runtime progress to stderr
only when the run summary changes or new run events appear. The line includes
task counts, inflight count, manual gates, permission requests, worker counts,
and the latest event type. `watch` is still useful from a second terminal
because it is read-only and can follow an already running taskpack. `stop` is
scoped to the selected run: it writes registered stop files and signals only
registered worker PIDs and owned descendants. It never searches for process
names such as `codex`.

To clean old taskpacks, start with a dry run:

```bash
agentteam taskpack delete --project-root /path/to/repo --taskpack old-id --dry-run
agentteam taskpack delete --project-root /path/to/repo --taskpack old-id --delete-run --force
```

Deletion is scoped to the profile `work_root`. A run directory is never deleted
unless both `--delete-run` and `--force` are present.

Versioned framework updates are side-by-side releases under the project
`work_root`:

```bash
agentteam update --project-root /path/to/repo --status
agentteam update --project-root /path/to/repo --from /path/to/agentteam/checkout --release-id m37-local
agentteam update --project-root /path/to/repo --rollback previous-release-id
```

`update --from` requires a clean source checkout, copies the launcher and runtime
package into `<work_root>/releases/<release-id>`, and switches `active.json` for
future commands. Existing run state is not rewritten. New runs record
`runtime_release_id` and `runtime_release_root` when an active release exists.

If a runtime worker cannot safely continue without operator guidance, it returns
`result_status: "blocked"` with `output.manual_gate.question`. The two-phase
scheduler records `manual_gate_required`, blocks the task with the generated
question id, and `submit` reports `status: "manual_gate_required"` when a
waiting gate is present. To answer from another terminal without copying the
question id, use interactive resume:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam resume \
  --run-dir /tmp/agentteam-taskpacks/runs/example-taskpack \
  --interactive \
  --operator liuql
```

`resume --interactive` prints the waiting question to stderr and accepts either
plain answer text or a small command loop before the final answer:

- `/gates` prints all waiting manual gates in the run.
- `/task` prints the blocked backlog item, objective, risk, role, scopes, and
  blockers.
- `/why` prints the worker question, options, and reason.
- `/events` prints recent related scheduler events.
- `/context` prints task, reason, and event context together.
- `/answer <text>` submits the final answer. Plain text without a slash also
  submits the answer.

When multiple gates are waiting, use `--question-id Q-TASK-001-ATTEMPT-001` to
answer only that gate. If the id is not currently waiting, the command exits
with JSON on stderr that includes the available `waiting_question_ids`.
To inspect waiting gates without answering, run the same command with `--list`
and omit `--interactive`; it prints a JSON summary to stdout with question ids,
task ids, task objectives, risk targets, and worker questions when available.

After the answer is submitted, the runtime writes `operator_answer_received`,
clears the task blocker in the two-phase scheduler state, and appends a
`backlog_updated` event with `task_status: "ready"`. The next dispatch payload
includes `operator_guidance` with the recorded question id, answer, and
operator.

If a Codex worker fails because it needs an operator-approved runtime
capability, for example a sandbox escalation, the adapter normalizes the failure
to `result_status: "blocked"` with `output.permission_request`. The scheduler
records `permission_request_required`, blocks the task with a generated
`PERM-...` request id, and notifications use the same Feishu path as manual
gates.

Inspect and resolve permission requests from another terminal:

```bash
agentteam permissions list \
  --run-dir /tmp/agentteam-taskpacks/runs/example-taskpack

agentteam permissions approve \
  --run-dir /tmp/agentteam-taskpacks/runs/example-taskpack \
  --request-id PERM-TASK-001-ATTEMPT-001 \
  --operator liuql \
  --reason "Allow one bounded retry."

agentteam permissions deny \
  --run-dir /tmp/agentteam-taskpacks/runs/example-taskpack \
  --request-id PERM-TASK-001-ATTEMPT-001 \
  --operator liuql \
  --reason "Keep the task blocked until the plan changes."
```

Approval clears the blocker and stores `permission_grants` on the task. The
next dispatch includes those grants in the mailbox payload. Denial keeps the
task blocked and records the decision without retrying.

To send a Feishu custom-bot notification when a manual gate is recorded, keep
the webhook and optional signing secret in environment variables and pass the
environment variable names into the run:

```bash
export AGENTTEAM_FEISHU_AGENTTEAM_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/..."
export AGENTTEAM_FEISHU_AGENTTEAM_SECRET="..."

PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam run \
  /tmp/agentteam-taskpacks/frozen/example-taskpack \
  --run-root /tmp/agentteam-runs \
  --notification-project agentteam \
  --feishu-webhook-env AGENTTEAM_FEISHU_AGENTTEAM_WEBHOOK \
  --feishu-signing-secret-env AGENTTEAM_FEISHU_AGENTTEAM_SECRET
```

The runtime records `notification_sent` or `notification_failed` telemetry
after durable operator events such as `manual_gate_required` and
`permission_request_required`. Missing Feishu environment variables disable
notification sending without failing the run. Event payloads never include the
webhook URL or signing secret.

For scripts, answer a known question id directly:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam answer \
  --run-dir /tmp/agentteam-taskpacks/runs/example-taskpack \
  --question-id Q-TASK-001-ATTEMPT-001 \
  --answer "Choose the minimal CLI operator-answer route first." \
  --operator liuql
```

`--author-runtime fake` creates deterministic fixture taskpacks for tests and
smoke runs. `--author-runtime codex` asks the Codex CLI to author the draft. The
Codex author path requires a clean target Git repository, writes scratch context
outside the taskpack directory, and fails validation if the author edits the
target repository or leaves unexpected files in the taskpack.

Frozen taskpacks are immutable launch inputs. The freezer rejects extra draft
files and symlinks before copying artifacts and writing `manifest.json`.

The taskpack launcher currently translates `fake` and `codex` runtime backends.
It intentionally rejects `shell` taskpack launches until shell command mapping
has an explicit design.

## Relationship To Codex

Codex compatibility should be implemented through adapters:

```text
AgentRuntime adapter:
  spawn / send / wait / close / resume

ToolRuntime adapter:
  list tools / call tool / load MCP

WorkspaceRuntime adapter:
  exec / sandbox / apply patch / git
```

The native runtime should be able to run without Codex subagents in M0. Codex
integration can be added after the actor, mailbox, and event model is stable.

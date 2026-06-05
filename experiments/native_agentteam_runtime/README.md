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

The current implementation is still an experiment. It uses a module CLI rather
than an installed `agentteam` executable:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam --help
```

Shell scripts in or around this experiment are development helpers. They are
not the primary operator interface.

## Taskpack Authoring Flow

The taskpack path turns a target repository plus a human goal into runtime
artifacts that the scheduler can consume.

For the normal operator path, use `submit`. It drafts, validates, freezes, and
runs the taskpack in one command. For manual use, start the interactive form and
answer the prompts:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam submit --interactive
```

Interactive prompts are written to stderr, so stdout remains the final JSON
summary. For scripts and repeatable runs, pass the same inputs as flags:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam submit \
  --project-root /path/to/repo \
  --goal "optimize the target behavior under an explicit metric" \
  --work-root /tmp/agentteam-taskpacks \
  --taskpack-id example-taskpack \
  --author-runtime fake \
  --one-shot
```

`submit --runtime auto` is the default. It runs fake-authored taskpacks with the
`fake` runtime for smoke tests, and Codex-authored taskpacks with the `codex`
runtime for live work. Use `--runtime fake` or `--runtime codex` to make that
choice explicit.

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
and omit `--interactive`; it prints a JSON summary to stdout.

After the answer is submitted, the runtime writes `operator_answer_received`,
clears the task blocker in the two-phase scheduler state, and appends a
`backlog_updated` event with `task_status: "ready"`. The next dispatch payload
includes `operator_guidance` with the recorded question id, answer, and
operator.

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

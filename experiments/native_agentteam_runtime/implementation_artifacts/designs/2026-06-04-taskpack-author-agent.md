# Taskpack Author Agent Design

## Goal

Make real-repository AgentTeam runs usable without requiring the operator to
hand-write `agent_pool.json`, `backlog.json`, verification commands, and output
directory wiring for every task.

The first target is the Verisilicon runtime-F1 optimization task, but the
mechanism must be general: the operator should provide a repository root, a
natural-language goal, and optional constraints. A dedicated authoring step
turns that intent into a frozen taskpack. The runtime only executes the
taskpack after deterministic validation accepts it.

## Problem

The current runtime can dispatch tasks through scheduler, mailbox workers,
Codex runtime adapters, integration queues, and verified batch merge. It is
still inconvenient to use on a real project because the human has to assemble
multiple low-level artifacts:

- agent pool definitions;
- backlog tasks;
- role and runtime profiles;
- read and write scopes;
- verification command JSON;
- output directory conventions;
- merge and integration policy.

This is acceptable for smoke tests, but it does not match the intended daily
workflow. The operator should not need to know the internal JSON shapes before
running a multi-agent task.

## Design Principle

Taskpack authoring is planning, not execution.

An agent may draft a taskpack, but it must not directly start workers, edit the
target repository, run arbitrary commands, or grant itself broader permissions.
The draft becomes executable only after a deterministic validator checks and
freezes it.

## Recommended Flow

The operator-facing flow should become:

```bash
agentteam daemon start
agentteam submit --project-root /path/to/repo --goal "optimize X under Y"
agentteam status
agentteam logs
agentteam result
```

For explicit review before execution:

```bash
agentteam taskpack draft \
  --project-root /path/to/repo \
  --goal "optimize X under Y"

agentteam taskpack validate <taskpack-id>
agentteam run <taskpack-id>
```

The first implementation can keep the existing Python CLI underneath this
interface. Shell scripts may exist as development helpers, but they should not
be the primary long-term entry point.

## Components

### Taskpack Author Agent

The author agent reads the operator goal, repository metadata, existing
semantic artifacts, and lightweight repo-map context. It produces a draft
taskpack, not a patch.

The draft contains:

- `taskpack.yaml`, with metadata, project root, goal, policy, and lifecycle;
- `agent_pool.json`, with scheduler and worker role definitions;
- `backlog.json`, with scoped tasks and dependencies;
- `verification.json`, with allowed verification commands and success
  criteria;
- `README.md`, with generated operator notes and assumptions.

The author agent should prefer small executable tasks and declare when the goal
is too broad for one run.

### Taskpack Validator

The validator is deterministic code. It checks whether a draft can become a
frozen taskpack.

Required checks:

- `project_root` exists and is a Git repository;
- every task has bounded `read_scope` and `write_scope`;
- write scopes do not include repository-wide wildcards unless explicitly
  allowed by policy;
- verification commands match an allowlist or an approved command profile;
- merge policy is explicit;
- task dependencies are acyclic;
- runtime profiles use supported backends, currently Codex by default;
- success criteria are measurable enough to evaluate after execution;
- generated files do not request direct access to credentials or unrelated
  host paths.

Rejected taskpacks remain inspectable but are not runnable.

### Taskpack Freezer

After validation, the draft is copied into an immutable run directory with a
manifest digest. The scheduler consumes the frozen copy, not the editable
draft.

This prevents a planning agent, worker, or accidental human edit from changing
the taskpack while a run is active.

### Runtime Launcher

The launcher adapts the frozen taskpack to the existing runtime CLI:

- loads the frozen agent pool and backlog;
- sets the project root;
- creates the output directory;
- starts the daemon or one-shot run mode;
- chooses Codex runtime profiles;
- passes verification commands into the integration batch verifier.

The launcher should expose stable human commands such as `agentteam run`,
`agentteam daemon start`, `agentteam submit`, `agentteam status`, and
`agentteam logs`.

## Data Flow

1. Operator submits `project_root`, `goal`, and optional policy flags.
2. Taskpack Author Agent drafts a taskpack in a draft directory.
3. Deterministic validator checks scope, commands, dependencies, policy, and
   runtime compatibility.
4. Freezer copies the accepted draft to a frozen run directory and records a
   manifest digest.
5. Scheduler reads the frozen backlog and dispatches tasks to mailbox workers.
6. Workers use Codex runtime adapters to produce patches and structured
   results.
7. Integration queue verifies accepted patches in an integration worktree.
8. Runtime reports result, logs, diffs, verification output, and merge status.

## Verisilicon Taskpack Target

The first concrete taskpack should wrap the known runtime-F1 optimization
workflow for `/home/liuql/projects/verisilicon`.

The taskpack should express:

- goal: improve runtime gesture Macro F1 without increasing
  `others_predictions`;
- read scope: README, evaluation scripts, runtime classifier code, existing
  result artifacts, and relevant tests;
- write scope: runtime classifier thresholds and narrowly related test or
  result documentation files;
- runtime backend: Codex only;
- verification: baseline-aware unit or replay command that checks Macro F1 and
  `others_predictions`;
- merge policy: no merge unless integration verification passes.

This target is useful because it prevents the next run from being another
manual single-session optimization. The runtime must produce the patch through
the scheduler and worker path.

## Error Handling

Draft errors should stop before runtime execution. Examples:

- repository is not clean enough for the requested mode;
- verification command is missing or not allowed;
- write scope is too broad;
- task dependency graph is invalid;
- the author agent cannot identify a measurable success criterion.

Runtime errors should preserve artifacts:

- failed author result;
- rejected validator report;
- frozen taskpack manifest;
- worker mailbox messages;
- Codex last-message JSON;
- patch, diff audit, verification output, and merge status.

The operator should be able to inspect the failed run without reconstructing
the command line.

## Testing Strategy

Deterministic tests should cover:

- taskpack schema loading;
- validator acceptance and rejection cases;
- freezing and manifest digest stability;
- launcher translation from taskpack to existing runtime CLI arguments;
- fake-Codex execution of a generated small fixture taskpack.

Live tests should remain explicitly gated. A Verisilicon live run should be a
manual or opt-in test because it uses a real repository, real Codex execution,
and potentially long verification commands.

## Non-Goals

This design does not require:

- automatic long-horizon research loops;
- support for Claude, DeepSeek, or other backends in the first version;
- unrestricted shell command generation by agents;
- direct merge of unverified patches;
- replacing the existing scheduler, mailbox, worker pool, or integration queue.

## Acceptance Criteria

The feature is ready for first use when:

- a user can draft a taskpack from `project_root` and `goal`;
- the validator rejects unsafe or underspecified taskpacks;
- an accepted taskpack can be frozen and launched through a stable `agentteam`
  command;
- the Verisilicon runtime-F1 task can be represented without hand-writing all
  low-level JSON;
- execution still goes through scheduler, mailbox worker, Codex adapter,
  integration verification, and explicit merge policy.

## Open Boundary

The author agent can make task decomposition suggestions, but the deterministic
validator owns executability. This keeps architecture authority outside the
implementation worker while still allowing the system to generate useful work
plans automatically.

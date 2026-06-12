# Native Runtime Long-Term Roadmap

Status: active implementation roadmap for the native AgentTeam runtime
experiment.

This document records the medium and long-term direction for the native
runtime implementation after M22. It is not a semantic architecture authority
document and it is not a per-milestone implementation plan. Its job is to keep
the implementation sequence coherent across multiple milestones.

## Artifact Role

The roadmap answers:

- what capability layer should be built next;
- what milestone-level acceptance means;
- which design risks require an explicit decision before implementation;
- which tempting features are intentionally outside the current route.

The roadmap does not replace:

- `system_framework.md`, which explains the top-level system architecture;
- `runtime_model.md`, which explains the runtime actor and message model;
- `implementation_artifacts/m0_file_runtime.md`, which records implemented
  runtime behavior;
- milestone design and plan files under `implementation_artifacts/designs/` and
  `implementation_artifacts/plans/`.

Milestone plans may refine the details of a roadmap item. If implementation
evidence shows that the roadmap is stale, the controller or integrator should
update this file after the milestone result is validated. A worker agent may
emit a roadmap feedback proposal, but it should not directly mutate this
roadmap as part of ordinary code work.

## Current Baseline

The implementation has already proven these layers:

1. File-backed scheduler state, mailbox dispatch, event replay, and SQLite
   state index.
2. Runtime adapters for fake workers, shell commands, and Codex CLI execution.
3. Attempt-scoped worktrees, diff audit, patch capture, integration worktree
   apply, verification, and opt-in integration commit checkpoints.
4. Scheduler and daemon loops, worker registry, one-shot mailbox subprocess
   workers, and long-running mailbox worker processes.
5. Static worker pools and two-phase dispatch/collect scheduling with retry,
   timeout, and bounded inflight execution.
6. Integration gate separation: task result acceptance, patch integration,
   integration verification, and integration commit remain distinct gates.
7. Worker health supervision with restart accounting and failed-worker state.
8. Planner-generated backlog proposals for decomposition tasks.
9. Bounded planner context packages that expose current state, available roles,
   allowed scopes, and proposal contract hints without dumping the full repo.
10. Codex planner prompt contract, no-worktree fallback execution, fallback
    dirty-check rejection, and fake Codex planner worker-pool coverage.
11. Selected semantic artifact context ingestion with digest, timestamp,
    heading, bounded excerpt, and warning metadata.
12. Proposal quality gate for self-dependencies, generated dependency cycles,
    risk-target enforcement, L0/L1 scope size limits, L2 review blocking, and
    inspectable decomposition rejection details.
13. Rolling milestone decomposition waves with generated task lineage,
    milestone decomposition state, max-wave terminal status, and default
    single-wave compatibility.
14. File-backed worker-pool resume from process registry with attached PID
    health and stop-file shutdown.
15. Durable accepted-patch integration queue with `pending`, `applied`,
    `verified`, `blocked`, and `committed` states, plus replay visibility.
16. Batch integration verification over queued patch sets in a dedicated batch
    worktree, with persisted batch results.
17. Verified batch fast-forward merge back to the source branch, with source
    cleanliness, batch commit, and `--ff-only` safety gates.
18. Worker-pool restart budgets with quarantine state after repeated process
    exits.
19. Two-phase dispatch avoidance for quarantined agents, allowing new work to
    route to another compatible idle agent.
20. Codex role runtime profiles, prompt contracts, and bounded context packages,
    so scheduler and resident worker-pool paths can inherit Codex model,
    sandbox, timeout, command, fallback worktree settings, model-facing role
    guidance, and explicit context references from the agent pool.
21. Repository-map-backed context generation with tracked-file inventory,
    Python AST symbol summaries, task-scoped `repo_context.v1` packages,
    dispatch payload paths, Codex prompt references, and conservative cache
    reuse for clean same-HEAD repositories.
22. Repo-context observability with attempt-level `repo_context_path` replay,
    SQLite state-index visibility, and a CLI `repo-contexts` drilldown view.
23. Repo-context quality improvements beginning with candidate test selection
    derived from selected source files, Python imports, test paths, and task
    objective tokens.
24. A gated live-Codex pipeline smoke that exercises one real implementation
    attempt through repo context, role context, patch capture, integration
    queue, batch verification, verified merge, and source-repo verification.

This means the experiment is no longer only a file-format prototype. It is now a
small local multi-process runtime with a deterministic scheduler, durable
communication files, scoped worker execution, proposal validation, and Codex as
the only live LLM backend on the current implementation route.

## Backend Constraint

Current implementation constraint: all live LLM worker execution must use Codex.
This is not a permanent architecture ban on other models. The user may later
introduce API-based models such as DeepSeek or Claude Opus, but those backends
are not active targets now.

Role differentiation is implemented through Codex runtime profiles and prompt
contracts: different roles can carry different Codex model, sandbox, timeout,
command, worktree policy, model-facing role guidance, and bounded role context
packages. Fake and shell adapters remain test harnesses, not production
multi-agent backends.

## Roadmap Principles

- The scheduler remains deterministic software. LLM agents propose work and
  results; they do not own leases, retries, artifact authority, or merge policy.
- Context packages are bounded. The planner receives selected state, role,
  scope, and artifact summaries instead of the full repository.
- Each writable attempt owns one isolated worktree. Long-lived role identity is
  independent from short-lived runtime processes.
- Planner output is a proposal, not authority. The scheduler validates role,
  scope, shape, dependency, and risk constraints before adding tasks to the
  backlog.
- Live model calls are opt-in smoke coverage. Local tests must remain
  deterministic with fake or shell adapters.
- A milestone should deliver one independently verifiable runtime capability.

## Near-Term Route

### M23: Codex Planner Prompt Contract

Status: implemented.

Goal: make the real Codex runtime able to produce the same structured
`task_proposal` shape that the fake planner currently returns.

Scope:

- add a planner-specific prompt path for `task_kind == "decompose_backlog"`;
- include the planner context file path and result schema in the prompt;
- require Codex to write one JSON proposal through the existing
  `--output-last-message` contract;
- keep fake Codex command coverage for deterministic tests;
- add a gated live smoke that can be enabled for one real planner call;
- preserve scheduler-side role and write-scope enforcement.

Acceptance:

- fake planner, fake Codex planner, and scheduler loop tests pass;
- invalid Codex planner output is rejected without mutating backlog authority;
- a live planner smoke is skipped unless the live gate is enabled;
- when enabled, the live smoke produces at least one accepted bounded worker
  task inside the allowed scope.

### M24: Semantic Artifact Context Ingestion

Status: implemented.

Goal: let decomposition use selected design and implementation artifacts without
placing large documents or source files directly into the model context.

Scope:

- define a small allowlist of source artifacts for planner context;
- summarize roadmap, architecture, backlog, and milestone state into compact
  context sections;
- include source path, source digest, timestamp, and excerpt budget metadata;
- keep source-code indexing out of the planner context unless a later milestone
  adds a dedicated repo map source.

Acceptance:

- planner context includes compact artifact summaries with source provenance;
- context size is bounded by explicit per-section limits;
- stale or missing artifacts produce clear context warnings instead of silent
  hallucinated state;
- tests verify that full document bodies are not embedded by default.

### M25: Proposal Quality Gate

Status: implemented.

Goal: reject bad automatic task splits before they become executable backlog
state.

Scope:

- enforce task size and risk rules for generated L0/L1/L2 work;
- reject duplicate task ids, self-dependencies, dependency cycles, and
  impossible role or scope combinations;
- require high-risk generated tasks to route through review before execution;
- record compact rejection reasons in events and scheduler state.

Acceptance:

- malformed, cyclic, duplicate, over-broad, and out-of-policy proposals are
  rejected deterministically;
- accepted proposals contain enough fields for execution without asking the
  user to manually split the task;
- rejection events are inspectable from the state index.

### M26: Rolling Milestone Decomposition

Status: implemented.

Goal: turn automatic decomposition from a one-shot generated task into a
milestone-level loop.

Scope:

- generate a bounded batch of executable tasks for the current milestone;
- mark milestone decomposition status separately from worker task status;
- prevent infinite decomposition loops;
- update backlog state after a milestone completes;
- open the next decomposition task only when evidence shows the current batch
  is done, blocked, or insufficient.

Acceptance:

- a scheduler run can decompose, execute, collect, and advance one milestone
  without manual task injection;
- generated tasks remain bounded and executable;
- completed milestone state records the proposal source, accepted task ids, and
  validation outcome;
- the scheduler does not generate repeated duplicate decomposition tasks.

## Mid-Term Route

### M27: Persistent Runtime Process Model

Status: implemented.

Goal: make resident role agents feel like durable workers rather than short
experiments launched only for a CLI run.

Scope:

- define process lifecycle state for long-running workers;
- support resume after scheduler restart;
- separate worker process health from logical agent availability;
- make mailbox consumption idempotent across restarts.

Decision gate: choose whether the first persistent supervisor is still file
based or moves worker/session state into SQLite.

### M28: Worktree Isolation And Integration Queue

Goal: make parallel writable work practical for real repositories.

Status: implemented with feature-level verified batch merge.

Scope:

- create one worktree per writable attempt;
- retain rejected worktrees for inspection;
- queue accepted patches for integration;
- verify an integrated batch before merge;
- require the whole task or feature slice to pass before merging to the main
  branch.

Decision: use feature-level verified batch merge. Task-level integration commits
remain checkpoints for audit and debugging, while final source-branch delivery
is gated by batch verification and fast-forward merge.

### M29: Health-Driven Reassignment

Goal: let the scheduler react to unhealthy workers without user intervention.

Status: restart-budget, quarantine, new-dispatch avoidance, and explicit
reassignment event lineage implemented. Remaining work is inflight migration
policy.

Scope:

- restart failed worker processes within policy;
- quarantine repeatedly failing runtime profiles;
- reassign eligible tasks to another compatible role or backend;
- preserve the original attempt lineage.

Decision gate: define the maximum automatic retry and reassignment budget for a
single task before escalation.

### M30: Runtime Observability

Goal: make long-running operation inspectable without reading raw JSONL files.

Status: implemented for the current route. CLI-only summary, resource-specific
drilldown views, current milestone visibility, and next decomposition visibility
are available.

Scope:

- add CLI views for backlog, leases, workers, events, sessions, and integration
  queue;
- expose latest failure reasons and blocked tasks;
- show current roadmap milestone and next scheduled decomposition;
- keep the underlying event log as the source of truth.

Decision: the first monitor remains CLI-only. A local dashboard is deferred
until the CLI views prove the data shape.

### M31: Codex Role Runtime Profiles

Goal: make resident role agents configurable without duplicating runtime
settings onto every agent entry.

Status: implemented for the current route. M31a added runtime profiles, M31b
added prompt contracts, M31c added bounded role context packages, and M31d
exposes effective runtime profile source in session state. M31e adds runtime
profile source counts to the observability summary. M31f allows role context
packages to reference repo map files as navigation pointers without embedding
repo content.

Scope:

- add `agent_pool.role_runtime_profiles` keyed by role name;
- keep profile precedence deterministic: `agent.runtime_profile`,
  `role_runtime_profiles[role]`, runtime defaults, then fake;
- route both core scheduler execution and resident worker-pool startup through
  the same role profile rule;
- attach role prompt contracts to dispatch payloads and render them explicitly
  in Codex prompts;
- write bounded role context files and pass `role_context_path` in dispatch
  payloads;
- record the effective runtime profile source on runtime sessions for state
  index and observability queries;
- summarize runtime profile source counts in the default runtime observability
  view;
- optionally add repo map manifest, inventory, and symbol-map references to a
  role context package as navigation pointers;
- keep Codex as the only live LLM backend on this route;
- keep CLI/default Codex command settings usable as local environment defaults.

Acceptance:

- a scheduler run can use a role-level Codex profile when the selected agent has
  no agent-level runtime profile;
- a resident worker pool starts a role agent as a Codex worker from the role
  profile;
- a dispatch payload can carry the selected agent role and role prompt contract;
- Codex prompts include a dedicated role contract section;
- a dispatch payload can point to a bounded role context file;
- Codex prompts include a dedicated role context package section;
- runtime session state can distinguish explicit adapters, factories,
  agent-level profiles, role-level profiles, defaults, and the fallback fake
  adapter, plus the two-phase external mailbox adapter path;
- the default observability summary reports runtime profile source counts;
- a role context package can point to repo map artifacts without including
  source bodies or task-specific selected files;
- agent pool schemas accept role runtime profiles, prompt contracts, and
  context packages.

Remaining follow-up work:

- add verification-summary references to role context packages once integration
  batch summaries have a stable compact index.

### M32: Repository Map Context Generation

Goal: give implementation workers a compact navigation map for the target
repository without dumping source files into planner or worker prompts.

Status: implemented for the current route. M32a added tracked-file inventory,
M32b added Python AST symbol summaries, M32c added task-scoped repo context
packages, M32d wired context paths into scheduler dispatch and Codex prompts,
and M32e added clean-cache reuse with dirty worktree invalidation.

Scope:

- build `state/repo_map/inventory.json` from `git ls-files`, with `rg --files`
  fallback when Git metadata is unavailable;
- record path, size, extension-derived language, broad category, and bounded
  content digest metadata;
- build `state/repo_map/symbols.json` for Python files using the standard
  library `ast` module;
- keep unsupported languages inventory-only;
- build bounded `repo_context.v1` packages under `repo_contexts/` using task
  objective, `read_scope`, `write_scope`, and symbol/path matches;
- attach `repo_context_path` and `repo_context_schema_version` to dispatch
  payloads when `project_root` is available;
- prompt Codex workers to read the repo context file before selecting
  implementation files;
- reuse repo maps only for clean worktrees at the same HEAD with matching scan
  options and symbol extraction version.

Acceptance:

- repo inventory, Python symbols, task context selection, dispatch wiring,
  prompt rendering, and cache invalidation are covered by deterministic unit
  tests;
- repo context files contain bounded metadata and symbol summaries, not full
  source bodies;
- dirty or unversioned worktrees rebuild the map and record an explicit warning;
- normal unit tests do not require live Codex calls.

Remaining follow-up work:

- measure whether live Codex workers actually inspect and use selected files;
- add language-aware extractors through compilers, LSPs, Tree-sitter, ctags, or
  build-system queries only after the bounded MVP proves useful.

### M33: Repo Context Observability And Effectiveness Smoke

Goal: make M32 context attachment inspectable without opening raw mailbox files
or guessing from filenames, then provide a gated smoke path for checking
whether Codex workers can actually consume attached repo context packages.

Status: M33a-M33c implemented. Dispatch events now record
`repo_context_path` when a repo context is attached, event replay restores that
field onto attempt state, the SQLite state index exposes it on attempts,
runtime observability has a `repo-contexts` drilldown view, and a gated
repo-context Codex smoke can verify that a worker reads the attached context
package. The `repo-contexts` view also reports selected-file hit metrics from
diff audit. The gated repo-context smoke has completed successfully against the
local Codex CLI in a controlled run. M33d keeps role context and repo context as
separate prompt sections and artifact files.

Scope:

- include `repo_context_path` and `repo_context_schema_version` on
  `message_dispatched` events when present in the dispatch payload;
- restore repo context fields into attempt state during event replay;
- include `repo_context_path` in the SQLite `attempts` state-index table;
- add `build_runtime_observability(..., view="repo-contexts")`;
- add CLI support for `--observability-view repo-contexts`;
- summarize selected files, selection reasons, omitted count, warnings, and
  repo-map manifest path without embedding source bodies.
- add `agentteam_runtime.live_codex_repo_context_smoke`, gated by
  `AGENTTEAM_RUN_LIVE_CODEX=1`;
- keep fake-Codex coverage deterministic by using a fake command that reads
  `repo_context_path` and reports the selected file.
- compare `diff_audit.actual_changed_files` with repo-context selected files to
  report changed-selected hits, changed-unselected files, and hit rate.
- document the boundary decision that role context packages should not
  automatically inline or absorb repo context packages on the current route.

Acceptance:

- a completed run with `project_root` exposes the attached repo context path in
  replayed attempt state and in the SQLite state index;
- the `repo-contexts` view links repo context files back to attempt ids;
- the CLI can print the same view as JSON;
- deterministic tests cover direct API, CLI behavior, env-gated smoke skipping,
  fake-Codex repo context consumption, and diff-audit hit metrics.

### M34: Repo Context Selection Quality

Goal: improve repository context usefulness while preserving bounded context
packages and deterministic scheduler authority.

Status: M34a-M34c implemented. Repo context packages now include
`candidate_tests` for selected source files. Source ranking also gives stronger
weight to objective matches against Python symbols than to path-only objective
matches. The `repo-contexts` observability view exposes candidate test
summaries without requiring operators to open the raw context JSON.

Scope:

- keep source-file selection and test-candidate selection as separate context
  fields;
- infer Python module names from selected source paths;
- rank test files higher when they import a selected module;
- add path-name and objective-token matches as weaker signals;
- rank selected source files with weighted objective signals: symbol/import
  matches outrank path-only matches;
- report candidate test count and candidate test summaries in repo context
  observability;
- keep unsupported languages on the existing inventory-only fallback path.

Acceptance:

- a task selecting `pkg/module.py` can report `tests/test_module.py` as a
  candidate test when the test imports `pkg.module`;
- candidate tests do not consume the `selected_files` budget;
- a source file defining an objective-matched symbol outranks a path-only file
  with the same objective term;
- the `repo-contexts` view reports candidate tests with path, language, and
  selection reasons;
- no compiler, LSP, or live model call is required.

Remaining follow-up work:

- add language-specific extractors behind conservative fallbacks.

### M35: Language-Aware Symbol Extractors

Goal: broaden repository context usefulness beyond Python while keeping the
repo map deterministic and dependency-light.

Status: M35a-M35b implemented. The repo map now extracts lightweight JavaScript
and TypeScript symbol summaries with conservative regex scanning. Python
continues to use the standard-library AST extractor. Candidate test selection
can use JS/TS relative imports to detect tests that import selected source
files.

Scope:

- keep the shared `repo_symbols.v1` shape: imports, top-level functions,
  classes, and methods;
- extract ES module imports, exported or top-level function declarations, class
  declarations, and simple class methods for JavaScript and TypeScript files;
- resolve JS/TS relative import paths from test files to selected source file
  module paths for candidate test ranking;
- update the symbol extraction version so clean-cache reuse does not mix old
  Python-only summaries with multi-language summaries;
- keep unsupported languages on inventory-only fallback until they have a
  dedicated extractor.

Acceptance:

- a tracked `.ts` source file appears in `symbols.json`;
- extracted summaries include imports, exported function declarations, class
  declarations, and methods with line numbers;
- a TypeScript test importing `../src/service` can be ranked as a candidate
  test for selected source file `src/service.ts`;
- symbol summaries do not embed source bodies;
- no Node, LSP, Tree-sitter, compiler, or live model call is required.

Remaining follow-up work:

- add CommonJS and re-export import signals if repository evidence shows they
  are needed.

### M36: End-To-End Implementation Pilot

Goal: move from isolated runtime capabilities to a small but complete
implementation workflow that can prove whether the current Codex-only route is
usable on real code changes.

Status: M36a and M36b implemented. Both have completed once against the local
Codex CLI.

Scope:

- M36a adds `agentteam_runtime.live_codex_pipeline_smoke`, gated by
  `AGENTTEAM_RUN_LIVE_CODEX=1`;
- create a temporary Python repository with a failing stdlib `unittest` suite;
- dispatch one implementation task with role context and repo context paths;
- require the Codex worker to edit exactly one source file and report that file
  in `changed_files`;
- accept the result only after diff audit and write-scope validation;
- queue the accepted patch, apply it in a batch worktree, run verification,
  fast-forward merge the verified batch back to the source repository, and run
  source-repo verification again;
- support exact-file `write_scope` entries as well as directory write scopes;
- M36b adds `agentteam_runtime.live_codex_multifile_pipeline_smoke`, also gated
  by `AGENTTEAM_RUN_LIVE_CODEX=1`;
- create a small multi-file fixture where the worker must implement
  `src/toc.py` and update `docs/guide.md` while tests live under `tests/`;
- verify multi-file exact write scopes, repo context selection, role context
  consumption, patch capture, batch verification, and source merge on the same
  pipeline as M36a;
- keep deterministic fake-Codex coverage in normal tests while leaving real
  Codex calls opt-in.

Acceptance:

- the smoke skips without the live gate and creates no output directory;
- with a fake Codex command, the pipeline reports accepted validation, pending
  integration queue status, verified batch status, passed verification, merged
  batch status, exact changed files, repo context path, role context path, and
  passing source-repo tests;
- exact-file write scopes such as `src/text_utils.py` are accepted when the
  changed file matches exactly;
- a multi-file task can report and merge exactly `docs/guide.md` and
  `src/toc.py`, with unit tests confirming that generated documentation content
  matches source behavior;
- normal unit tests still require no live model call.

Remaining follow-up work:

- select the first non-fixture pilot repository or add one more workflow
  capability if the fixture-level live results expose a blocker.

### M37: Operator Control Plane And Versioned Update

Status: implemented in the native-runtime branch, including the follow-up
notification and release lifecycle telemetry listed below.

Goal: make long-running operation understandable and controllable while allowing
the AgentTeam framework itself to be updated without breaking active runs.

Scope:

- make `agentteam status` and `agentteam taskpack list` distinguish true live
  runs from stale `running` state;
- add `agentteam watch` for compact terminal progress and important runtime
  events;
- add scoped `agentteam stop` and stale cleanup without killing unrelated Codex
  sessions;
- expand Feishu notification policy from manual gates to sparse run-level
  operator events;
- add `agentteam update` as a side-by-side release installer for future runs,
  not an in-place overwrite of code used by active runs;
- record the runtime release id on new runs and warn when a run was started from
  an unmanaged development worktree.

Acceptance:

- liveness-aware status reports `running-alive` versus `running-stale`;
- watch can show progress without mutating the run;
- stop can stop a scoped fake worker run and clean stale state safely;
- Feishu receives only sparse run-level notifications by default;
- update installs immutable releases, switches the active release for future
  commands, and leaves existing run release bindings unchanged.
- taskpack delete supports dry-run cleanup and requires explicit run deletion.

Completed follow-up:

- emit dedicated `integration_blocked` and `run_stale_detected` events for the
  sparse notification policy;
- add more lifecycle telemetry for release activate/rollback.

### M38: Git-Backed Runtime Release Store

Status: implemented in the native-runtime branch.

Goal: make AgentTeam runtime updates reproducible and storage-efficient for
local long-term use by installing framework releases from explicit local or
remote git refs into a global immutable release store.

Decision: do not introduce binary packaging or cross-platform release artifacts
yet. Use git as the version authority. A release is identified by source repo,
source ref, resolved commit SHA, and a generated release id. The release code is
stored once under a global cache, while each project stores only active release
pointers, release events, and run-level release pins.

Scope:

- add `agentteam update --from-git <repo> --ref <ref>` for both local git repos
  and remote git URLs;
- resolve every requested ref to an exact commit before installation;
- install the resolved source tree into
  `~/.local/share/agentteam/runtime-releases/<source-key>/<release-id>/`;
- keep project-local release state as pointers under
  `<work_root>/releases/`, not full framework copies;
- write project-local ref metadata so `update --status`, `rollback`, and run
  pinning continue to work per project;
- protect globally cached releases that are active or pinned by any known
  project before global cleanup deletes them;
- preserve the existing side-by-side update rule: existing runs keep using the
  release they started with, and new runs use the active release.

Acceptance:

- installing from a local repo ref records `source_repo`, `source_ref`,
  `source_commit`, `source_key`, `release_id`, and global `release_root`;
- installing from a remote git URL resolves the ref, downloads the matching
  source into the global release store, and activates the project pointer;
- re-installing the same source commit reuses the existing global release store
  entry instead of copying code into every project;
- `agentteam update --status` clearly shows active, latest installed, known
  project references, and whether the active release is latest;
- `agentteam update --rollback <release-id>` activates a project-local pointer
  to an already installed global release;
- cleanup can report and avoid deleting releases referenced by active project
  pointers or nonterminal run pins;
- normal unit tests use local temporary git repositories and require no network.

Short-term slices:

- M38a implemented: global release store layout, local `--from-git --ref`
  install, manifest format, project pointer refs, and status/rollback
  compatibility.
- M38b implemented: remote git URL resolution and download with deterministic
  temporary checkouts, plus reuse of an already installed source commit.
- M38c implemented: global release reference discovery and cleanup protection
  across known work roots, with dry-run explanations and explicit-force orphan
  deletion through `agentteam gc --global-releases`.

### M39: Runtime SOP Evidence Contract

Status: planned after M38c.

Goal: adapt the single-agent implementation SOP into a native runtime contract
that the scheduler can enforce before introducing a database projection layer.

Decision: do not copy the outer `design/` SOP file layout into the runtime.
The runtime should preserve the same risk/evidence intent through its own
objects: taskpacks, backlog items, dispatch events, worker results, reports,
integration gates, and semantic escalation events.

Scope:

- define the runtime-owned evidence levels for `L0`, `L1`, `L2`, and `L3`;
- make `L3` an escalation-only path owned by a dedicated
  `semantic_architecture_agent`, not an ordinary implementation-worker task;
- let the semantic architecture agent maintain semantic design proposals and
  authority-update drafts, while the scheduler pauses for the user only when
  that agent cannot resolve the architecture question;
- require missing `L2` evidence to block integration, not merely worker result
  capture;
- add result/report fields such as `evidence_level`, `evidence_status`,
  `trace_carrier`, and `missing_evidence`;
- expose evidence status through `status`, `report`, and important lifecycle
  events without requiring long per-task trace files;
- keep `events.jsonl` and compact worker results sufficient for normal `L0`
  and `L1` work.

Acceptance:

- the roadmap and runtime design docs explicitly distinguish worker task risk,
  milestone risk, and semantic architecture risk;
- task proposal validation can represent or reject `L3` without dispatching it
  to ordinary writable workers;
- a generated `L3` proposal is routed to semantic escalation state, produces an
  inspectable event, and waits for semantic architecture handling;
- `L2` results with missing required evidence are visible but cannot enter
  verified integration;
- `status` and `report` can summarize evidence completeness for completed,
  blocked, and escalated work;
- normal unit tests use fake/shell workers and require no live model calls.

Short-term slices:

- M39a: runtime SOP contract document and risk/evidence data model.
- M39b: proposal validation and scheduler routing for `L3` semantic
  escalation.
- M39c: `L2` evidence gate, result/report evidence summaries, and regression
  tests.

### M40: Artifact Projection Database

Status: deferred until after M39 establishes the runtime evidence contract.

Goal: add a local SQLite projection layer that makes long-running project state
fast to query, summarize, clean up, and diagnose while keeping file-backed
artifacts as the audit authority.

Decision: use a hybrid store. `events.jsonl`, frozen taskpacks, patch files,
reports, and integration baseline metadata remain authoritative artifacts.
`agentteam.db` is a rebuildable projection and index. DB corruption must not
invalidate an existing run, and DB writes must not be required for a worker
attempt to finish.

Scope:

- create `<work_root>/agentteam.db` as a project-local projection database;
- define schema-versioned tables for runs, taskpacks, tasks, attempts, events,
  artifact metadata, integrations, reports, token usage, manual gates,
  permission requests, releases, and evidence summaries;
- keep SQLite in WAL mode with short single-writer transactions;
- index existing authoritative artifacts by physical path, logical type,
  content hash, run id, task id, attempt id, size, and retention policy;
- add `agentteam db check` and `agentteam db rebuild` so the projection can be
  validated and regenerated from `frozen/`, `runs/`, reports, state files, and
  event logs;
- make `status`, `logs`, `taskpack list`, `report`, `update --status`, and
  `gc` eligible to read from the DB first, with file replay fallback;
- expose aggregate statistics for token usage, attempts, failures, runtime
  duration, evidence completeness, and integration outcomes through a small
  `stats` command or view;
- improve `gc` so it can distinguish authoritative artifacts, rebuildable
  caches, old releases, orphaned runs, and protected active or nonterminal
  state.

Acceptance:

- deleting `agentteam.db` and running rebuild recreates the same run/task/event
  summaries from authoritative files;
- `status` and `logs` return correct results when the DB is present and when it
  is absent;
- DB writes are best-effort projections and never replace append-only event
  writes;
- artifact metadata records content hashes and sizes for reports, patches,
  taskpacks, state snapshots, role contexts, repo contexts, and evidence
  summaries;
- `gc --dry-run` can explain what would be deleted and why using indexed
  artifact metadata;
- normal unit tests use temporary SQLite files and require no live model calls.

Short-term slices:

- M40a: schema, migration log, event/taskpack/run/evidence projection,
  `db check`, and `db rebuild`.
- M40b: read-through query path for `status`, `logs`, `taskpack list`, and
  report metadata with file replay fallback.
- M40c: artifact metadata hashes, token/stat aggregation, and DB-backed smart
  `gc --dry-run`.

## Longer-Term Route

These items should wait until M23-M30 have made the local runtime reliable:

- MCP tool and context compatibility as adapter capabilities, not as the native
  control plane, and initially around Codex runtime sessions.
- Richer repository analysis using language-aware tools such as compilers, LSP,
  build systems, and static analyzers, with compact summaries fed to repo and
  role context packages after the M32 MVP is validated.
- Moving from a rebuildable DB projection to a DB-primary artifact store, if the
  hybrid M40 path proves reliable and file-backed replay becomes the bottleneck.
- Policy-governed semantic feedback where implementation evidence can propose
  updates to design authority artifacts without letting ordinary workers edit
  those artifacts directly.
- API or executable backend adapters for DeepSeek, Claude Opus, Claude Code, or
  other models only after credentials, contracts, and result extraction paths
  are available.

## Explicit Non-Goals For The Current Route

- No full-repository JSON dump as planner context.
- No unrestricted planner writes to backlog, roadmap, design, or source code.
- No automatic edits to semantic authority artifacts by implementation workers.
- No live-model call requirement in normal unit tests or CI.
- No distributed multi-host orchestration before the local runtime is stable.
- No A2A dependency for the native control plane.

## Update Policy

Update this roadmap when one of these events occurs:

- a milestone changes what the next milestone should be;
- a validation result exposes a missing capability or invalid assumption;
- the user makes an explicit product or architecture decision;
- implementation evidence shows that a listed milestone should be split,
  reordered, or removed.

Do not update this roadmap for ordinary local implementation details that are
already captured in milestone plans, events, or test output.

The next recommended step is M39. It should establish the runtime SOP evidence
contract before the database projection work begins in M40.

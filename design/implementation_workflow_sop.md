# Implementation Workflow SOP

Status: current execution authority for turning an approved semantic design into
bounded code changes.

This document starts after `artifact_workflow_sop.md` has produced an acceptable
semantic design and validation plan. Its job is to make code modification
safe, incremental, and verifiable in a real repository with limited context
windows.

## Core Principle

Do not ask an agent to understand the whole repository.

Build a small, tool-generated navigation layer, use it to select a narrow set
of real files, then make one bounded change at a time. The repository index is
not the context package. The index helps choose context; the worker receives
only the task-local context it needs.

```text
semantic design
  -> implementation pack
  -> repo grounding
  -> localization
  -> task context pack
  -> bounded task card
  -> code change
  -> verification
  -> trace
  -> next task card or design CR
```

Implementation can discover that the approved semantic design is incomplete or
wrong. That feedback is valid, but it must not let a worker redefine the
architecture it was asked to implement. Workers report design gaps; the
orchestrator decides whether to open the design-change workflow.

## Non-Goals

The implementation workflow does not try to:

- build a universal code-understanding platform;
- summarize the entire repository into JSON;
- support every language deeply in the first version;
- infer business semantics from static indexes alone;
- produce a complete implementation plan for every future milestone at once;
- let parallel workers mutate the same authority workspace.

## Required Inputs

Before implementation starts, the orchestrator must have:

- semantic design artifacts from `output/current/`;
- acceptance contract and validation plan;
- implementation pack draft or task to create one;
- target repository root;
- allowed commands and sandbox policy;
- known build, test, or smoke-check verification objects if available;
- explicit user constraints about files, dependencies, runtime, and network.

If build/test commands are unknown, the first task is repository grounding, not
feature implementation.

## Required Output Structure

Use this structure under the current run output. The M0 profile uses only the
required subset; later maturity levels may add optional indexes.

```text
output/current/
├── INDEX.json
├── implementation/
│   ├── INDEX.json
│   ├── implementation_pack.json
│   ├── repo_index/
│   │   ├── repo_manifest.json
│   │   ├── file_inventory.jsonl
│   │   ├── unknowns.json
│   │   ├── project_detectors.json        (M1 optional)
│   │   ├── language_packs.json           (M1 optional)
│   │   ├── test_surface.json             (M1 optional)
│   │   ├── symbol_index.jsonl            (M2 optional)
│   │   ├── dependency_edges.jsonl         (M2 optional)
│   │   └── module_cards/                 (M2 optional)
│   ├── context_packs/
│   │   └── CTX-<N>-<short_name>.json
│   ├── task_cards/
│   │   └── TASK-<N>-<short_name>.json
│   ├── workspace_policy.json
│   ├── verification/
│   │   └── VERIFY-<N>-<short_name>.json
│   ├── agent_dispatches/
│   │   └── DISPATCH-<N>-<short_name>.json
│   ├── agent_results/
│   │   └── RESULT-<N>-<short_name>.json
│   ├── progress.json                    (M1 optional; M0 may use trace status)
│   └── traces/
│       └── IMPL-TRACE-<N>-<short_name>.json
```

`output/current/INDEX.json` must point to
`output/current/implementation/INDEX.json` after implementation starts.
Consumers must not infer latest implementation state from filenames alone.

Implementation authority artifacts:

- `implementation/INDEX.json`
- `implementation/implementation_pack.json`
- `implementation/workspace_policy.json`
- `implementation/context_packs/*.json`
- `implementation/task_cards/*.json`
- `implementation/verification/*.json`
- `implementation/agent_dispatches/*.json`
- `implementation/agent_results/*.json`
- `implementation/progress.json`
- `implementation/traces/*.json`

In M0, `progress.json` is optional. If it is omitted, the latest trace and task
card status are the progress record. In M1 and later, `progress.json` becomes a
required authority artifact.

Repo index files are derived artifacts and may be regenerated. If a task card
uses a repo index slice, it must record the slice hash, base revision, and stale
conditions that were used to build the context pack.

Minimum `implementation/INDEX.json` fields:

```json
{
  "implementation_index_id": "implementation_index",
  "base_revision": "git sha or snapshot id",
  "current_pack": {
    "path": "implementation_pack.json",
    "content_hash": "sha256:<hash>"
  },
  "workspace_policy": {
    "path": "workspace_policy.json",
    "content_hash": "sha256:<hash>"
  },
  "context_packs": [
    {
      "id": "CTX-001-example",
      "path": "context_packs/CTX-001-example.json",
      "version": 1,
      "status": "current",
      "content_hash": "sha256:<hash>"
    }
  ],
  "task_cards": [
    {
      "id": "TASK-001-example",
      "path": "task_cards/TASK-001-example.json",
      "version": 1,
      "status": "ready",
      "content_hash": "sha256:<hash>"
    }
  ],
  "verification_objects": [
    {
      "id": "VERIFY-001-example",
      "path": "verification/VERIFY-001-example.json",
      "version": 1,
      "status": "current",
      "content_hash": "sha256:<hash>"
    }
  ],
  "agent_dispatches": [
    {
      "id": "DISPATCH-001-worker",
      "path": "agent_dispatches/DISPATCH-001-worker.json",
      "version": 1,
      "status": "completed",
      "content_hash": "sha256:<hash>"
    }
  ],
  "agent_results": [
    {
      "id": "RESULT-001-worker",
      "path": "agent_results/RESULT-001-worker.json",
      "version": 1,
      "status": "completed",
      "content_hash": "sha256:<hash>"
    }
  ],
  "latest_progress": {
    "path": "progress.json or null for M0",
    "content_hash": "sha256:<hash> or null"
  },
  "traces": [
    {
      "id": "IMPL-TRACE-001-example",
      "path": "traces/IMPL-TRACE-001-example.json",
      "version": 1,
      "status": "completed",
      "content_hash": "sha256:<hash>"
    }
  ],
  "repo_index_slices": [
    {
      "path": "repo_index/file_inventory.jsonl",
      "content_hash": "sha256:<hash>",
      "derived": true
    }
  ]
}
```

## Maturity Profiles

The implementation workflow is intentionally staged. Do not build M2 machinery
to prove M0.

| Profile | Required scope |
|---|---|
| M0 | Single repository, single writer, current workspace, one implementation pack, minimal repo manifest, file inventory, unknowns, one context pack, one task card, one verification object, one worker dispatch/result, one trace. |
| M1 | Separate context packs, progress tracking, optional worktree policy, project detector, test surface, one primary language detector. |
| M2 | Symbol index, dependency edges, module cards, richer language packs, parallel read-only explorers, container/devcontainer policy. |
| M3 | Strong sandbox, parallel writing through disjoint worktrees, risk reports, LSP-backed deep indexes, cross-milestone scheduling. |

### M0 Pass Criteria

M0 passes only if:

- no parallel writing is used;
- the worker reads every file in its write scope before editing;
- the task card has one bounded objective and one allowed write scope;
- the verification object has a concrete command, cwd, expected exit code, and
  acceptance coverage reference;
- the trace records base revision, read file hashes, edited file hashes,
  command result, and next action;
- any need to expand scope stops the task and creates a revised task card.

### M0 Required Artifact Subset

M0 requires only:

- `implementation/INDEX.json`
- `implementation_pack.json`
- `repo_index/repo_manifest.json`
- `repo_index/file_inventory.jsonl`
- `repo_index/unknowns.json`
- one `context_packs/*.json`
- one `task_cards/*.json`
- one `verification/*.json`
- one worker `agent_dispatches/*.json`
- one worker `agent_results/*.json`
- `workspace_policy.json`
- one implementation trace

M0 does not require `project_detectors.json`, `language_packs.json`,
`test_surface.json`, `symbol_index.jsonl`, `dependency_edges.jsonl`,
`module_cards/`, container policy, parallel workers, or separate
`progress.json`.

### M0 Field Defaults

For M0, fields that support later parallelism may use strict defaults:

- `read_closure` equals `read_scope` unless a broader closure is explicitly
  known;
- `required_source_files` equals `write_scope` plus any files the task card
  names as direct inputs;
- `max_context_policy.max_files` is advisory only; if required files do not fit,
  shrink the task;
- `rollback_plan` may be "revert the single task patch";
- `integration_policy` is `single_workspace`;
- `workspace_mode` is `current_workspace`;
- progress is represented by task status and trace outcome.

## Implementation Pack Schema

`implementation_pack.json` is the authoritative handoff from semantic design to
code work. The schema lives here; architecture only describes the concept.

Minimum M0 fields:

```json
{
  "_meta": {
    "artifact_id": "implementation_pack",
    "artifact_type": "implementation_pack",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "semantic_artifacts": ["artifact ids or paths"],
  "acceptance_contract": "artifact id or path",
  "validation_plan": "artifact id or path",
  "source_layout": {
    "known_roots": ["paths"],
    "unknowns": ["layout questions"]
  },
  "environment": {
    "required_tools": ["tools or unknown"],
    "network_policy": "disabled | restricted | allowed | unknown"
  },
  "build_contract": {
    "commands": ["verification ids or discovery tasks"],
    "unknowns": ["build questions"]
  },
  "test_contract": {
    "commands": ["verification ids or discovery tasks"],
    "unknowns": ["test questions"]
  },
  "milestone_outline": ["M0", "M1", "M2"],
  "first_task_seed": {
    "objective": "smallest useful implementation slice",
    "candidate_read_scope": ["paths or unknown"],
    "candidate_write_scope": ["paths or unknown"]
  },
  "task_card_generation_policy": {
    "plan_all_tasks_up_front": false,
    "next_task_depends_on_latest_trace": true
  },
  "progress_state": "not_started | running | blocked | completed"
}
```

## Verification Object Schema

Verification entries must be objects, not command strings. A zero exit code is
not enough evidence by itself.

```json
{
  "_meta": {
    "artifact_id": "VERIFY-001-example",
    "artifact_type": "verification_object",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "verification_id": "VERIFY-001-example",
  "command": "test command",
  "cwd": "repository-relative path",
  "env": {"KEY": "VALUE"},
  "timeout_seconds": 300,
  "expected_exit": 0,
  "required_output": ["text or regex that proves the intended tests ran"],
  "covers_acceptance_ids": ["AC-001"],
  "allowed_writes": ["paths or temp dirs"],
  "allow_zero_tests": false
}
```

The orchestrator must reject unverifiable success: wrong cwd, missing expected
output, zero tests when `allow_zero_tests` is false, or writes outside
`allowed_writes`.

Verification objects are authority artifacts stored under
`implementation/verification/` and referenced through `implementation/INDEX.json`.
Context packs and task cards must reference verification object ids, not raw
command strings.

## Phase Flow

### Phase I0: Semantic Handoff Gate

Before reading code, check whether the design is ready for implementation.

Required checks:

- top-level `INDEX.json` identifies the current semantic artifacts;
- latest lint report has no blocking failures;
- semantic review is approved or has no unresolved high-severity blockers;
- adversarial review is `PROCEED` or all blockers have design CRs;
- validation plan contains the first empirical probe;
- trace can replay the semantic artifact/review sequence;
- acceptance criteria are testable or explicitly human-reviewable;
- implementation pack exists or a task exists to create it;
- first empirical probe is named;
- expected source layout is either known or marked as unknown;
- validation commands are known or marked as discovery tasks;
- any unresolved semantic blocker has a CR, not a hidden assumption.

If these are not true, return to the artifact workflow and create a design CR.

### Phase I1: Repository Grounding

Build a lightweight repository index using tools first. Do not use an agent to
read every file.

M0 grounding:

- file inventory: path, size, hash, extension, language guess, generated/vendor
  flags;
- repo manifest: repository root, base revision or snapshot id, dirty state,
  obvious manifest/build files found by filename;
- unknowns: missing commands, ambiguous entrypoints, unrecognized build systems;
- one verification object if a command is known, otherwise a discovery task card.

M1 grounding may add:

- project detection: package managers, workspace roots, and build systems;
- test surface: test directories, test naming patterns, target-specific commands;
- richer verification objects for build, lint, type-check, smoke, and tests.

The first version may use only `rg`, `git ls-files`, manifest parsing, and
manual configuration. Language-specific symbol indexes are not part of M0.

### Phase I2: Language Pack Expansion

When useful after M0, run language packs to improve navigation. A language pack
adapts existing tools to the common repo index model.

Language pack maturity:

- M1: `detect`, `inventory`, `build_contract`, and `test_surface` for one
  primary language.
- M2: `symbols` and `dependencies`.
- M3: `risk_report`, LSP integration, and cross-language edges.

Full language packs may implement:

| Capability | Purpose |
|---|---|
| `detect` | Find language roots, manifests, package managers, and build systems. |
| `inventory` | Classify source, tests, generated files, vendored code, and config. |
| `symbols` | Extract functions, classes, structs, interfaces, traits, exports, or commands. |
| `dependencies` | Extract imports, includes, package edges, or build-target edges. |
| `build_contract` | Discover build/type-check commands and output artifacts. |
| `test_surface` | Discover unit, integration, smoke, or end-to-end tests. |
| `risk_report` | Mark weak areas: macros, reflection, dynamic imports, plugin registration, codegen. |

Preferred tool sources:

| Ecosystem | Preferred sources |
|---|---|
| Rust | `cargo metadata`, `cargo check`, rust-analyzer. |
| Go | `go list`, `go test`, gopls. |
| TypeScript/JavaScript | `package.json`, `tsconfig.json`, `tsc --noEmit`, tsserver, eslint. |
| Python | `pyproject.toml`, `ast`, pyright or mypy, pytest. |
| C/C++ | `compile_commands.json`, CMake/Bazel/Make metadata, clangd, clang-tidy. |
| Java/Kotlin | Maven/Gradle metadata, javac, jdtls. |
| Shell/config | shellcheck, schema validators, dry-run commands, smoke tests. |

Unsupported languages fall back to weak mode: file inventory, manifest
detection, regex or tree-sitter extraction when available, local reading, and
stronger runtime verification.

### Phase I3: Localization

Localization chooses likely code touchpoints for one implementation slice.

Inputs:

- semantic goal or bug;
- acceptance contract;
- repo manifest;
- module cards;
- symbol/dependency index if available;
- text search results;
- failing test or runtime evidence if available.

Outputs:

- ranked candidate files and symbols;
- reason each candidate is relevant;
- missing files that must be read before editing;
- proposed `read_scope`;
- proposed `write_scope`;
- local verification object;
- confidence and unresolved unknowns.

Repo maps and symbol indexes only propose candidates. A worker must read the
real source files before deciding on a patch.

### Phase I4: Task Context Pack

Create a context pack for one small worker task. The pack should be small
enough to fit comfortably in one context window.

Context packs are authority artifacts because they define the material actually
given to a worker. They must be immutable after task launch. If context changes,
create a new context pack and update the task card reference.

Minimum fields:

```json
{
  "_meta": {
    "artifact_id": "CTX-001-example",
    "artifact_type": "context_pack",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "context_pack_id": "CTX-001-example",
  "content_hash": "sha256:<hash>",
  "base_revision": "git sha or snapshot id",
  "dirty_snapshot": "clean | dirty:<id>",
  "goal": "One bounded implementation objective",
  "semantic_references": ["artifact ids or paths"],
  "repo_index_references": [
    {
      "path": "repo_index/file_inventory.jsonl",
      "content_hash": "sha256:<hash>"
    }
  ],
  "read_scope": ["paths allowed or required for reading"],
  "write_scope": ["paths allowed for editing"],
  "read_closure": ["direct callers, callees, configs, tests, fixtures, or generated sources that must be considered"],
  "required_source_files": ["paths the worker must inspect before editing"],
  "verification": ["VERIFY-001-example"],
  "known_constraints": ["constraints"],
  "unknowns": ["questions the worker must resolve locally"],
  "stop_conditions": ["conditions requiring escalation"],
  "file_hashes": {
    "path": "sha256:<hash>"
  },
  "max_context_policy": {
    "max_files": 15,
    "prefer_snippets": false,
    "full_files_required_for_write_scope": true
  }
}
```

Do not include whole-repository summaries. Include only the source files,
snippets, command output, and artifact references needed for the task.

The context pack must include all files in `write_scope`, related tests, and
the nearest relevant manifest/config files. If that does not fit the context
budget, shrink the task instead of deleting necessary context.

For parallel writing, `read_closure` must be explicit, hashed, and treated as
complete for the task boundary. If closure completeness cannot be justified,
parallel writing is blocked and the task must run as the only writer. Every file
in `read_scope`, `read_closure`, and `write_scope` must appear in
`file_hashes` or be covered by a verification object that explains why a hash is
not available.

That verification-object exception is not allowed for parallel writing.
Parallel write tasks require explicit hashes for the complete read/write
closure.

### Phase I5: Task Card Creation

A task card is the unit of implementation work. It is not a broad milestone
plan.

Minimum fields:

```json
{
  "_meta": {
    "artifact_id": "TASK-001-example",
    "artifact_type": "task_card",
    "version": 1,
    "status": "ready",
    "content_hash": "sha256:<hash>"
  },
  "task_id": "TASK-001-example",
  "status": "ready | running | blocked | completed | failed",
  "context_pack_id": "CTX-001-example",
  "context_pack_hash": "sha256:<hash>",
  "objective": "Concrete change to make",
  "allowed_actions": ["read", "edit", "run_tests"],
  "read_scope": ["paths"],
  "read_closure": ["paths"],
  "write_scope": ["paths"],
  "verification": ["VERIFY-001-example"],
  "success_criteria": ["observable criteria"],
  "rollback_plan": ["how to revert or isolate changes"],
  "escalation_conditions": [
    "need to edit outside write_scope",
    "verification object unavailable",
    "semantic design contradicts repository reality",
    "dependency or generated-code boundary discovered"
  ],
  "integration_policy": "single_workspace | worktree_patch | patch_integration_agent"
}
```

Workers must stop instead of expanding scope silently.
Task card scope and context pack scope must match. If they drift, the task card
is blocked until both are regenerated and re-hashed.

### Phase I6: Workspace And Sandbox Policy

In a Codex-like environment there are two different controls:

- permission sandbox: limits filesystem, network, and dangerous commands;
- workspace sandbox: prevents code edits from colliding or polluting authority
  state.

AgentTeam should support these levels:

| Level | Use case | Policy |
|---|---|---|
| 0: current workspace | Small sequential edits. | One writer, explicit write scope, diff review, local verification. |
| 1: git branch or worktree | Milestone work or worker isolation. | One worker per worktree; return patch, trace, and test output. |
| 2: container/devcontainer | Dependency-heavy or environment-sensitive projects. | Run build/test in isolated image with recorded commands and volumes. |
| 3: strong sandbox | Untrusted code or destructive tests. | Network/process/filesystem isolation, explicit approval gates. |

Parallel agents are read-only by default. Parallel writing requires disjoint
worktrees or disjoint, explicit, hashed read/write closures plus serial
integration. If either closure is incomplete, unknown, or missing hashes,
parallel writing fails closed.

Minimum `workspace_policy.json` fields:

```json
{
  "_meta": {
    "artifact_id": "WORKSPACE-001-example",
    "artifact_type": "workspace_policy",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "workspace_policy_id": "WORKSPACE-001-example",
  "base_revision": "git sha or snapshot id",
  "workspace_mode": "current_workspace | git_worktree | container | strong_sandbox",
  "worktree_path": "path or null",
  "writable_roots": ["paths"],
  "network_mode": "disabled | restricted | allowed",
  "command_allowlist": ["commands or prefixes"],
  "env": {"KEY": "VALUE"},
  "allowed_output_dirs": ["paths"],
  "shared_cache_policy": "none | read_only | read_write",
  "pre_change_snapshot": "snapshot id",
  "post_change_snapshot_required": true
}
```

Pre/post snapshots must block scope drift: generated files, lockfiles, caches,
or repo-external writes are allowed only when the workspace policy declares
them.

### Phase I6.5: Subagent Dispatch

The orchestrator dispatches implementation subagents only after a task card,
context pack, verification object, and workspace policy exist. M0 uses one
worker dispatch. M1 and later may add read-only explorer, context-pack builder,
patch reviewer, or patch integration dispatches.

Implementation subagent roles:

| Role | Default write scope | Output |
|---|---|---|
| `repo_explorer` | none | localization findings, candidate files, unknowns |
| `context_pack_builder` | `implementation/context_packs/` and `implementation/task_cards/` only | draft context packs and task cards |
| `worker_agent` | task-card `write_scope` only | code patch, verification results, unknowns |
| `patch_reviewer` | none | review verdict on patch, trace, and verification evidence |
| `patch_integration_agent` | integration workspace only | serially integrated patch or rejection record |
| `design_cr_agent` | none or non-authoritative CR draft path only | classified design gap and CR draft |

Minimum dispatch record:

```json
{
  "_meta": {
    "artifact_id": "DISPATCH-001-worker",
    "artifact_type": "agent_dispatch",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "agent_role": "worker_agent",
  "task_card_id": "TASK-001-example",
  "context_pack_id": "CTX-001-example",
  "context_pack_hash": "sha256:<hash>",
  "workspace_policy_id": "WORKSPACE-001-example",
  "input_artifacts": ["implementation_pack", "TASK-001-example", "CTX-001-example", "VERIFY-001-example"],
  "read_scope": ["paths"],
  "write_scope": ["paths"],
  "allowed_tools": ["read", "edit", "verification commands"],
  "delegation_allowed": false,
  "expected_output_schema": "implementation_agent_result",
  "stop_conditions": ["needs wider scope", "verification unavailable", "design conflict"],
  "budget": {"timeout_minutes": 30}
}
```

Minimum result record:

```json
{
  "_meta": {
    "artifact_id": "RESULT-001-worker",
    "artifact_type": "agent_result",
    "version": 1,
    "status": "current",
    "content_hash": "sha256:<hash>"
  },
  "dispatch_id": "DISPATCH-001-worker",
  "status": "completed | blocked | failed | cancelled",
  "changed_files": ["paths"],
  "verification_results": [
    {
      "verification_id": "VERIFY-001-example",
      "status": "passed | failed | skipped",
      "evidence": "trace or command-output path"
    }
  ],
  "new_unknowns": [],
  "assumptions": [],
  "design_findings": [
    {
      "type": "missing_detail | contradiction | ambiguity | better_design_option",
      "severity": "blocking | non_blocking",
      "evidence": ["file path, command output path, or accepted artifact id"],
      "affected_artifacts": ["semantic artifact id or implementation artifact id"],
      "requested_action": "create_design_cr | update_task_card | clarify_context | reject_worker_result"
    }
  ],
  "trace_refs": [],
  "recommended_next_action": "integrate_patch | revise_task | create_design_cr | stop"
}
```

A subagent result is not accepted unless it matches the dispatch scope and
schema. Every worker result must be referenced by the implementation trace.
`design_findings` are evidence and routing requests. They are not permission for
the worker to modify semantic artifacts or start design-authority agents.

#### Implementation-to-Design Escalation Gate

This gate keeps implementation feedback automated without giving workers
authority over the semantic contract.

Authority split:

- worker agents have reporting authority only;
- the orchestrator has escalation and dispatch authority;
- `design_cr_agent` has proposal authority only;
- the Integration Agent has serial authority to update current design
  artifacts through the artifact workflow.

Default worker dispatches set `delegation_allowed` to `false`. A worker may not
launch a design CR agent, semantic reviewer, or Integration Agent. If the host
platform technically allows nested agent calls, any nested output is treated as
untrusted evidence until the orchestrator converts it into a normal
dispatch/result pair.

Escalation flow:

1. Worker stops before encoding an invented design assumption into code.
2. Worker returns an `agent_result` with `design_findings`, concrete evidence,
   affected artifacts, and `recommended_next_action`.
3. Orchestrator validates the finding for schema, evidence, severity, affected
   artifacts, and current artifact hashes.
4. Orchestrator routes non-blocking implementation detail gaps to task-card,
   context-pack, or implementation-pack revision.
5. Orchestrator routes blocking semantic contradictions or ambiguities to a
   recorded `design_cr_agent` dispatch.
6. `design_cr_agent` produces only a CR draft or classification result.
7. The artifact workflow accepts, rejects, or integrates the CR through its
   normal lint, review, trace, and Integration Agent gates.
8. After integration, affected implementation packs, context packs, task cards,
   verification objects, and pending worker dispatches are invalidated or
   rebased before implementation resumes.

Escalation policy:

- Missing repository details are not automatically semantic design defects.
  Prefer implementation-pack or task-card revision when the semantic contract
  remains intact.
- A worker result that changes design files directly is scope-invalid and must
  be rejected.
- A worker result that requests escalation without evidence is blocked and must
  be revised before any design CR agent is dispatched.
- A blocking design finding pauses dependent task cards until the design CR is
  rejected, integrated, or downgraded to an implementation-only revision.
- Every escalation request and routing decision must be recorded in an
  implementation trace and linked to any resulting artifact CR.

#### Platform Adapter Gate

Native agent tools such as `spawn_agent`, batch fanout, or CSV fanout are only
transport mechanisms. They do not replace AgentTeam dispatch records, result
records, workspace policy, verification objects, or final gates.

Before invoking a platform-native subagent tool, the orchestrator must:

- write and index the `agent_dispatch` artifact;
- confirm the workspace policy can enforce the requested write mode;
- confirm nested delegation is disabled unless the dispatch explicitly allows
  it;
- use read-only mode unless a disjoint workspace or M0 single-writer policy is
  active;
- include the expected output schema in the prompt or adapter payload;
- record the base revision, context pack hash, task card hash, and verification
  object ids.

After the platform-native subagent returns, the orchestrator must:

- parse the raw message into an `agent_result` artifact;
- validate the result against the expected schema;
- reject results that do not declare status, changed files, verification
  evidence, and next action as required by the dispatch;
- compare changed files and generated outputs against `write_scope` and
  `workspace_policy`;
- treat missing, schema-invalid, scope-invalid, or timed-out results as failed,
  even if the host tool reports the job as completed;
- block integration until required verification objects pass.

Batch fanout is read-only by default. A batch job with any failed, missing,
schema-invalid, or scope-invalid item is not a successful implementation job.
Batch writing is allowed only through isolated worktrees or an equivalent
workspace policy plus serial patch integration.

### Phase I7: Code Change And Verification

Worker sequence:

1. Read required files from the context pack.
2. Confirm the change hypothesis.
3. Stop if required edits exceed `write_scope`.
4. Apply the smallest coherent patch.
5. Run the required verification objects.
6. Record changed files, command output summary, verification result, failures,
   and assumptions.
7. Return result to the orchestrator.

A worker `done` message is not proof. Verification evidence is required.

### Phase I8: Integration

The orchestrator or Patch Integration Agent reviews the returned patch.

Integration checks:

- patch changes only allowed files;
- no unrelated formatting or metadata churn unless required;
- every required verification object passed: expected exit matched, required
  output was present, zero-test policy was satisfied, acceptance coverage was
  declared, and writes stayed within `allowed_writes`;
- trace includes read files, edited files, verification object IDs, commands, and output summaries;
- worker dispatch and result records exist and match task card scope, context
  pack hash, and workspace policy;
- context pack hash, base revision, and read/write file hashes still match;
- repo index stale conditions are evaluated and fail closed on missing hashes;
- next task card is updated from actual results.

If multiple workers return patches, integrate serially. Rebase or reject stale
patches rather than merging conflicting assumptions. If an integrated patch
touches another pending worker's read closure, write scope, shared API, build
surface, or test surface, that pending task must be cancelled or rebased and
reverified.

Failed required verification blocks integration. The only exception is a
verification failure formally classified as unrelated, with evidence, a failure
route, and no acceptance coverage dependency. Such a failure cannot be counted
as satisfying the task success criteria.

### Phase I9: Progression

Implementation advances by short milestones:

```text
Probe 0: discover repository touchpoints, no code edit
M0: minimal vertical slice, smallest code edit, smallest verification
M1: expand the core path
M2: cover boundary cases and error handling
M3: regression, cleanup, documentation, and handoff
```

Do not plan every task card up front. Plan the next card from the latest code,
tests, trace, and unknowns.

## Repo Index Policy

The repo index is a navigation layer.

Allowed in the index:

- file paths, hashes, sizes, language guesses;
- manifest and build metadata;
- symbols, ranges, imports, dependency edges for M2 and later;
- test commands and target names;
- generated/vendor flags;
- module cards with short summaries for M2 and later;
- provenance, confidence, and stale conditions.

Not allowed as authoritative index facts:

- full source code content;
- broad LLM claims about business logic without evidence;
- inferred ownership without source;
- hidden assumptions about runtime behavior;
- stale facts without hash or commit checks.

Every nontrivial fact should record:

```json
{
  "source": {
    "tool": "tool or agent name",
    "command": "command if applicable",
    "repo_commit": "commit or unknown",
    "file_hash": "sha256:...",
    "generated_at": "ISO-8601"
  },
  "confidence": "high | medium | low",
  "stale_if": ["file_hash_changed", "manifest_changed", "tool_version_changed"]
}
```

For M0, provenance may be recorded at file, manifest, and index-slice level
instead of every fact. For any authority artifact, missing base revision or file
hash fails closed: the task must refresh grounding before worker launch or
integration.

Trust order:

```text
compiled/tested behavior
  > source file read by worker
  > compiler/LSP/build-system fact
  > parser/static scan fact
  > LLM summary
```

LLM summaries and low-confidence index facts may suggest files to read. They
must not be cited as the reason for a code edit. Patch rationale must reference
real source files, command output, or accepted semantic artifacts.

## Failure Routing

Classify failures before retrying.

Every failure route must declare:

- `invalidated_artifacts`
- `affected_task_ids`
- `pending_worker_action`: `continue | cancel | rebase | wait`
- `owner`
- `retry_budget`
- `reentry_phase`

| Failure | Route |
|---|---|
| Patch bug inside write scope | Return to same worker with verification output; invalidate only the failed trace attempt. |
| Need to edit outside write scope | Block task; create revised context pack and task card; cancel dependent pending workers. |
| Missing build/test command | Create repository grounding task; block implementation tasks that rely on that verification. |
| Subagent result violates dispatch scope or schema | Reject result; invalidate result record; revise dispatch or task card before retrying. |
| Platform adapter reports success with failed or missing subagent items | Treat the job as failed; invalidate affected results; do not integrate until every required item has a valid result or is explicitly routed as cancelled/failed. |
| Platform adapter cannot enforce write isolation | Downgrade to read-only dispatch or isolated worktree/container policy; block parallel writing. |
| Required verification failed | Block integration; classify failure as patch bug, environment issue, unrelated failure, or design conflict; invalidate the failed trace attempt and affected task status. |
| Repo index stale or wrong | Regenerate affected index slice; invalidate context packs and task cards derived from it. |
| Missing base revision or file hash | Fail closed; refresh grounding and regenerate affected context packs before worker launch. |
| Worker requests design escalation with evidence | Run the Implementation-to-Design Escalation Gate; pause affected task cards until the request is routed. |
| Worker requests design escalation without evidence | Reject or revise the result; do not dispatch a design CR agent. |
| Worker edits design artifacts or launches authority-writing agents outside dispatch | Reject the result as scope-invalid; invalidate the result record and reissue a constrained dispatch if the task remains valid. |
| Existing code contradicts semantic design | Create design CR in artifact workflow; cancel implementation tasks depending on the contradicted assumption. |
| Implementation pack lacks source/build/test detail | Revise implementation pack; regenerate task-card seeds derived from old pack. |
| Runtime probe invalidates approach | Return to semantic review or design CR; invalidate downstream task cards. |
| Design CR changes a base semantic artifact | Regenerate affected implementation packs, context packs, task cards, and verification objects before worker relaunch. |
| Parallel patch conflict | Rebase or integrate serially; cancel or reverify pending workers whose read/write closure was touched. |

## Stop Conditions

Stop implementation and return to planning when:

- no verification object can be defined for the change;
- worker needs write access outside declared scope;
- worker reports a blocking design finding that has not been routed;
- repository reality contradicts acceptance criteria;
- generated code or external dependency must be changed without policy;
- failing tests are unrelated and cannot be classified;
- the next change requires a larger architectural decision;
- sandbox permissions are insufficient for required commands.

## Final Deliverable

An implementation milestone is acceptable only if:

- top-level `INDEX.json` points to the current implementation index;
- implementation `INDEX.json` records current pack, workspace policy, context
  packs, task cards, verification objects, agent dispatches, agent results,
  progress, traces, versions, and hashes;
- every subagent result used by the milestone has a matching dispatch record and
  trace reference;
- platform-native subagent outputs were converted into validated `agent_result`
  artifacts; raw completion messages were not accepted directly;
- task cards and context packs identify exact read/write scopes;
- task cards record context pack hashes and base revisions;
- worker patches stay within declared write scope;
- every required verification object passed, unless an unrelated failure was
  formally classified with evidence and routed separately;
- traces record read files, edited files, file hashes, command summaries, and outcomes;
- repo index stale conditions were checked for touched files;
- missing hashes or unknown base revisions did not pass integration;
- progress records completed, blocked, and next tasks, or M0 trace/task status
  records the same state;
- unresolved design conflicts are represented as CRs;
- every implementation-originated design finding is routed, closed, or linked
  to a pending CR before the milestone is accepted;
- the next milestone is derived from current evidence, not from stale planning.

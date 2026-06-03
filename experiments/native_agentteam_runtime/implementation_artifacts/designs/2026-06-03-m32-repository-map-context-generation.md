# M32 Repository Map Context Generation Design

## Goal

Generate bounded repository context for implementation agents before dispatch,
so workers can start from relevant files, symbols, tests, and risk notes without
loading a whole repository into the model context.

M32 extends the current M31 role runtime/profile/prompt/context system. The
scheduler remains deterministic authority; the repository map is advisory
context, not permission to write outside task scope.

## Architecture

M32 adds three layers.

### L0 Repository Inventory

The runtime builds a file inventory from the target repository using local
source-of-truth commands such as `git ls-files`, falling back to `rg --files`
when needed. Each file entry records:

- relative path;
- size bytes;
- language guess from extension;
- optional content digest;
- broad category such as source, test, docs, config, generated, or unknown.

The inventory enforces size and ignore limits. Large files and ignored paths are
represented as metadata only.

### L1 Structure Summary

The first implementation should support Python through the standard-library
`ast` module and keep other languages on a conservative fallback path. For each
supported source file, the summary records:

- module path;
- top-level classes and functions;
- imports;
- likely test files by naming convention;
- warnings when parsing fails.

For unsupported languages, M32 records path/category metadata only. Tree-sitter,
ctags, compiler queries, and LSP integrations are later upgrades, not MVP
requirements.

### L2 Task Context Selection

Before dispatch, the scheduler selects a compact task context from the repo map
using:

- task `objective`;
- `read_scope`;
- `write_scope`;
- `required_role`;
- explicit role context package config from M31c.

The output is a bounded JSON context package. It includes likely relevant files,
symbol summaries, candidate tests, selected artifact summaries, and warnings.
The mailbox payload carries only a context path, not the full context body.

## Data Flow

1. The scheduler receives or creates a ready task.
2. The scheduler loads or refreshes the repository map for `project_root`.
3. The context selector builds a bounded `repo_context.v1` package for the task.
4. The role context package can reference this repo context by path.
5. The dispatch payload includes `repo_context_path` or folds the path into the
   existing `role_context_path`.
6. `CodexRuntimeAdapter` prompts the worker to read the context file before
   selecting files to inspect.

## Storage

Repo map artifacts should live under the runtime output directory:

```text
state/repo_map/
  inventory.json
  symbols.json
  manifest.json
repo_contexts/
  <attempt-id>-<role>.json
```

`manifest.json` records the repository root, git commit when available, scan
time, scan limits, and warning counts. Runtime JSONL events remain the source of
truth for dispatch behavior.

## Cache And Invalidation

The first cache key should be conservative:

- `project_root`;
- current git commit when available;
- inventory options;
- symbol extraction version.

If the repository is dirty or not a git worktree, M32 may still generate a map,
but it must mark the manifest as `dirty_or_unversioned`. Later milestones can
add incremental invalidation by file hash or mtime.

## Error Handling

Repo map generation should degrade rather than block ordinary dispatch:

- missing repository root: omit repo context and record a warning;
- unsupported language: keep inventory entry, skip structure summary;
- parse failure: record warning and continue;
- oversized file: record metadata only;
- context budget exceeded: truncate low-ranked entries and record omitted
  counts.

Only malformed scheduler-owned config should fail fast.

## Testing

M32 should use deterministic local fixtures. No live Codex calls are required.

Required tests:

- inventory includes tracked files and excludes ignored/generated files;
- Python AST summary extracts imports, classes, functions, and parse warnings;
- context selector respects read/write scopes and context budgets;
- dispatch payload includes the context path when `project_root` is available;
- missing or unsupported files produce warnings instead of exceptions;
- artifact lint, compileall, full unit tests, and placeholder scan pass.

## Milestone Slices

### M32a Repository Inventory

Build `inventory.json` and `manifest.json` from `project_root`. Add explicit
scan limits and warnings.

### M32b Python Structure Summary

Build `symbols.json` for Python files with AST extraction. Unsupported languages
remain inventory-only.

### M32c Task Context Selection

Generate bounded `repo_context.v1` packages from task metadata and repo map
summaries.

### M32d Scheduler And Codex Wiring

Attach repo context paths to dispatch payloads and prompt Codex workers to read
them.

### M32e Cache Reuse

Reuse valid repo maps by git commit and scan options. Mark dirty or unversioned
repos clearly.

## Non-Goals

M32 does not implement full semantic code understanding, whole-repository JSON
dumps, automatic architecture edits, production LSP integration, or multi-host
indexing. It provides a bounded navigation map that workers must verify by
reading relevant source and running tests.

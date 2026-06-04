# M32 Repository Map Context Generation Implementation Notes

## Implemented Surface

M32 adds `agentteam_runtime.repo_map` as the repository-context boundary for
the native runtime.

`build_repository_map(project_root, output_dir, max_file_bytes=65536)` writes:

```text
state/repo_map/
  inventory.json
  symbols.json
  manifest.json
```

`build_repo_context(project_root, output_dir, task, agent_role, max_files=8,
max_file_bytes=65536, context_id=None)` writes:

```text
repo_contexts/<context-id-or-task-id>-<agent-role>.json
```

Both functions return the generated payloads and their artifact paths so tests
and schedulers can inspect them without reparsing mailbox output.

## Inventory

The inventory builder uses `git ls-files` first and falls back to `rg --files`
when Git metadata is unavailable. Each file entry records relative path, size,
extension-derived language, broad category, and SHA-256 digest when the file is
within `max_file_bytes`.

The inventory intentionally excludes runtime/vendor noise such as
`__pycache__`, `.git`, and `node_modules`. Untracked files are not included in
the inventory, but they make the worktree state dirty and prevent cache reuse.

## Symbol Summaries

Python files get a lightweight AST summary with imports, top-level functions,
classes, and class methods. The summary does not embed function bodies or full
source text. Unsupported languages remain inventory-only until a later
language-aware extractor is added.

Parse failures are warnings, not dispatch blockers.

## Task Context

`repo_context.v1` selects a bounded set of files by ranking:

- exact or prefix match against `write_scope`;
- exact or prefix match against `read_scope`;
- objective token matches against paths and Python symbol names.

Selected entries include inventory metadata and available symbol summaries.
The context records `omitted_file_count` when the repository map contains more
files than the selection budget allows.

## Scheduler And Prompt Wiring

`run_simulation` and `TwoPhaseFileScheduler` attach these dispatch payload
fields whenever `project_root` is available:

- `repo_context_path`;
- `repo_context_schema_version`.

`CodexRuntimeAdapter` renders a dedicated `Repo context package:` prompt
section that points to the context file. The prompt carries the path, not the
context body.

Repo context is advisory. It does not expand read scope, write scope, lease
authority, validation rules, or merge policy.

## Cache Semantics

The cache is reused only when all of these are true:

- the current worktree is clean;
- the current Git HEAD matches the manifest;
- `project_root` matches the manifest;
- inventory options match;
- symbol extraction version matches;
- `inventory.json`, `symbols.json`, and `manifest.json` all exist and parse.

Dirty or unversioned worktrees rebuild the map, set
`working_tree_state` to `dirty_or_unversioned`, and record a
`working_tree_dirty` warning.

## Current Limits

M32 is a bounded navigation map, not full semantic understanding. It does not
run compilers, LSPs, Tree-sitter, ctags, tests, or live model calls. Workers
must still read relevant files and verify changes through the normal runtime
validation path.

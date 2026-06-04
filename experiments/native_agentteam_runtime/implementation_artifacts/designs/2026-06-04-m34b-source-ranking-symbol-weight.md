# M34b Source Ranking Symbol Weight Implementation Notes

## Goal

Prefer files whose Python symbols match the task objective over files that only
match the same objective token in their path.

## Problem

Before M34b, objective matching was a single weak signal. If two files were both
inside `read_scope` and both matched the objective text somewhere in their
metadata, path ordering could choose a less relevant file first.

Example:

```text
pkg/a_build_worker_notes.py
pkg/module.py  # defines build_worker()
```

For objective `Update build_worker behavior`, the path-only match could outrank
the file defining `build_worker()` because both received the same objective
score and path sorting broke the tie.

## Implemented Behavior

`build_repo_context` now scores objective matches with separate weights:

- Python import/function/class/method matches receive the stronger score;
- path-only matches receive a weaker score;
- the externally visible `selection_reasons` value remains `objective` for
  compatibility.

This keeps the context contract stable while improving ranking quality.

## Current Limits

The symbol weighting only uses Python AST summaries. Unsupported languages still
fall back to inventory and path metadata until language-aware extractors are
added.

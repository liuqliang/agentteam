# M35b JavaScript and TypeScript Candidate Test Selection Implementation Notes

## Goal

Use JS/TS symbol summaries to identify candidate tests that import selected
source files.

## Implemented Behavior

Candidate test selection now builds two kinds of selected import targets:

- Python module names, preserving the existing Python behavior;
- JavaScript and TypeScript source module paths without file extensions.

For JS/TS tests, relative import targets are resolved from the test file
directory and compared to selected source module paths. A test importing
`../src/service` can therefore receive the `imports_selected_module` ranking
reason when `src/service.ts` is selected.

## Boundary

This remains a conservative path-level mapping. It does not evaluate package
aliases, `tsconfig` paths, CommonJS `require`, barrel re-exports, or bundler
resolution rules. Those should be added only when repository evidence shows the
current mapping misses important tests.

## Validation

A regression test creates `src/service.ts` and `tests/service.test.ts`, then
verifies that the repo context candidate test entry includes
`imports_selected_module`, `path_match`, and the existing objective match.

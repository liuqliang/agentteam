# M34c Candidate Tests Observability Implementation Notes

## Goal

Expose repo context candidate tests in the `repo-contexts` observability view so
operators can inspect recommended validation targets without opening the raw
repo context JSON file.

## Boundary

M34c does not change repository context generation, scheduler dispatch, attempt
validation, or integration policy. It only summarizes the existing
`candidate_tests` field already written by `build_repo_context`.

## Implemented Behavior

Each readable repo context entry in the observability view now includes:

- `candidate_test_count`;
- `candidate_tests` entries with path, language, and selection reasons.

This mirrors the existing selected-file summary style and keeps the raw context
file as the authoritative source for full repository context details.

## Validation

A focused regression test builds a repository with `pkg/module.py` and
`tests/test_module.py`, runs a project-root simulation, and verifies that the
`repo-contexts` view reports `tests/test_module.py` as a candidate test with its
ranking reasons.

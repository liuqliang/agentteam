# M33c Repo Context Effectiveness Metrics Implementation Notes

## Goal

Expose a lightweight post-run signal for whether changed files were present in
the repo context selected for an attempt.

This is an observability metric only. It does not accept, reject, retry, or
merge work.

## Metric Source

The `repo-contexts` observability view already reads:

- `repo_contexts/*.json` for selected files;
- state-index attempts for `attempt_id` and `repo_context_path`;
- replayed attempt state for `diff_audit`.

M33c compares:

```text
repo_context.selected_files[*].path
diff_audit.actual_changed_files
```

## Exposed Fields

Each repo-context summary now includes:

- `actual_changed_file_count`;
- `changed_selected_file_count`;
- `changed_selected_files`;
- `changed_unselected_files`;
- `selected_file_hit_rate`.

`selected_file_hit_rate` is:

```text
changed_selected_file_count / actual_changed_file_count
```

When an attempt has no actual changed files, the hit rate is `null`.

## Interpretation

A high hit rate means changed files were represented in the context package.
A low hit rate means the worker changed files that were not selected by the
repo context. That may be valid, but it is useful evidence for tuning context
selection and task decomposition.

The metric does not prove the model read the context. M33b remains the smoke
path for checking that the context path is usable by a worker process.

# M34a Candidate Test Selection Implementation Notes

## Goal

Make repo context packages point workers toward likely verification files
without consuming the bounded `selected_files` implementation-file budget.

## Implemented Behavior

`repo_context.v1` now includes:

```json
{
  "candidate_tests": [
    {
      "path": "tests/test_module.py",
      "language": "python",
      "selection_reasons": [
        "imports_selected_module",
        "path_match",
        "objective"
      ]
    }
  ]
}
```

Candidate tests are derived after source file selection. They are advisory
verification hints and do not expand task scope or validation policy.

## Ranking Signals

The first implementation uses deterministic local metadata only:

- `imports_selected_module`: a Python test imports the selected source module or
  a symbol below that module;
- `path_match`: test path tokens overlap selected source path tokens;
- `objective`: objective tokens match the test path or Python symbol summary.

`imports_selected_module` is the strongest signal. Path and objective matches
are weaker tie-breakers.

## Current Limits

Only Python imports are understood structurally. Unsupported languages and test
framework-specific discovery remain future work behind conservative fallbacks.
The worker must still decide which tests to run and report verification
evidence through the normal result contract.

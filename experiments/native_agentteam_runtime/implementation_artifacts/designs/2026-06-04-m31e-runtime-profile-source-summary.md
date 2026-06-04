# M31e Runtime Profile Source Summary Implementation Notes

## Goal

Make the default runtime observability summary show which execution source paths
were used across completed runtime sessions.

## Implemented Behavior

`build_runtime_observability(output_dir)` now includes
`runtime_profile_source_counts`, grouped from replayed runtime session state.

This complements the `sessions` drilldown view:

- summary shows aggregate source distribution;
- sessions view still exposes per-session source details.

## Boundary

M31e does not change adapter selection, session lifecycle, or profile
precedence. It only adds an aggregate read-only view over existing session
state.

## Validation

A regression test runs two scheduler tasks with an explicit fake runtime
adapter and verifies that the summary reports
`{"explicit_runtime_adapter": 2}`.

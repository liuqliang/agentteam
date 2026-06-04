# M31f Role Context Repo Map References Implementation Notes

## Goal

Let a role context package point to repository map artifacts while preserving
the separation between role context and task-scoped repo context.

## Implemented Behavior

`role_context_packages.<role>.include_repo_map_references` is an explicit
boolean option. When it is `true` and a `project_root` is available, the role
context JSON includes `repo_map_reference` with:

- `manifest_path`;
- `inventory_path`;
- `symbols_path`;
- `boundary: navigation_reference_only`;
- a read policy that tells the worker to use `repo_context_path` for
  task-specific implementation file selection.

The role context still does not embed source bodies, selected files, candidate
tests, or repo context content.

## Boundary

This is the approved plan-B boundary. Role context can reference repo map files
for coarse navigation, but repo context remains the task-specific authority for
selected files and tests.

## Validation

A regression test enables repo map references for `repo_map_agent`, runs a
project-root simulation, verifies that referenced repo map files exist, and
checks that source-body markers do not appear in the role context package.

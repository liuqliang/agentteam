# AgentTeam Operator Reliability Pack Design

**Goal:** Make the current M0 framework easier to operate during real project runs by fixing authoring visibility, run path confusion, verbose low-level output, and manual taskpack creation.

**Scope:** This design does not add true long-lived multi-agent planning or new model providers. It tightens the existing Codex-backed taskpack/runtime wrapper so operators can see what is happening, stop stalled authoring, run explicit taskpacks, and read concise completion summaries.

## Behavior

- Codex taskpack authoring records `author_state.json` under the author context directory with status, PID, elapsed time, prompt/result paths, and final outcome.
- `agentteam status` reports active authoring work when no run exists yet or when authoring is still running.
- `agentteam stop --authoring` stops the latest running author process recorded in the project work root.
- `agentteam run` accepts either a runs root or a concrete run directory named after the taskpack without creating nested `<taskpack>/<taskpack>` output.
- `agentteam run` defaults to concise human output with report and run paths. Full runtime JSON remains available through `--json`.
- `agentteam taskpack new` creates an explicit operator-authored taskpack from project profile defaults and concrete scopes, bypassing Codex authoring while still using normal validation and optional freeze.

## Error Handling

- Authoring timeout writes a final state before raising validation failure.
- Stop-authoring only targets PIDs recorded in author state files and reports stale state without pretending it killed anything.
- Run path normalization is deterministic: when the supplied run root basename equals the frozen taskpack id, the parent directory is treated as the runs root and the supplied path becomes the run directory.
- Compact run output still forwards runtime child argument failures so invalid flags are visible.

## Testing

- Unit tests cover author state lifecycle, timeout state, run-root normalization, nested run-dir resolution, stop-authoring, compact low-level run output, and quick taskpack creation.
- Existing M0 tests remain the regression suite for runtime and taskpack behavior.

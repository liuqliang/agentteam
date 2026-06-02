# M12b Codex Runtime CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `CodexRuntimeAdapter` as a first-class CLI runtime choice so scheduler-loop experiments can run real backlog files through Codex without relying on `--codex-command` as an implicit selector.

**Architecture:** Keep the scheduler and runtime adapter contracts unchanged. Add a CLI-only `--runtime fake|shell|codex` selector. Preserve existing behavior: no runtime flag uses the fake adapter unless `--shell-command` or `--codex-command` is supplied; the command flags remain overrides for tests and experiments.

**Tech Stack:** Python 3.12 standard library, `argparse`, `unittest`, `codex exec`.

---

### Task 1: CLI Runtime Selector

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI test**

Add a test that runs:

```bash
python3 -m agentteam_runtime.cli \
  --agent-pool <fixtures/sample_agent_pool.json> \
  --backlog <tmp/backlog.json> \
  --output-dir <tmp/run> \
  --project-root <tmp/repo> \
  --runtime codex \
  --codex-command python3 <fake_codex.py>
```

Assert that the generated file exists, validation is accepted, and replayed
runtime session state records `CodexRuntimeAdapter`.

- [x] **Step 2: Verify red**

Observed red:

```text
agentteam_runtime.cli: error: unrecognized arguments: --runtime
```

- [x] **Step 3: Implement CLI selector**

Add `--runtime fake|shell|codex` and centralize adapter construction:

- infer `shell` when only `--shell-command` is supplied;
- infer `codex` when only `--codex-command` is supplied;
- default to `fake` when no explicit runtime or command override is supplied;
- require `--project-root` for the Codex path because the current
  `CodexRuntimeAdapter` needs a git worktree;
- reject contradictory combinations.

- [x] **Step 4: Verify green**

Observed green:

```text
test_cli_can_select_codex_runtime_with_command_override ... ok
```

- [x] **Step 5: Guard missing project root**

Add a regression test for:

```bash
python3 -m agentteam_runtime.cli ... --runtime codex
```

without `--project-root`. Observed red before the guard:

```text
AssertionError: 0 != 2
```

Then reject the command at argparse time:

```text
--project-root is required when --runtime codex is set
```

Observed green:

```text
test_cli_rejects_codex_runtime_without_project_root ... ok
```

### Task 2: CLI Live Smoke Entry Point

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/live_codex_cli_smoke.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_live_codex_smoke.py`

- [x] **Step 1: Write failing skip test**

Add a test that runs:

```bash
python3 -m agentteam_runtime.live_codex_cli_smoke --output-dir <tmp>
```

without `AGENTTEAM_RUN_LIVE_CODEX=1`. Assert skip status and no output
directory creation.

- [x] **Step 2: Write failing fake-Codex CLI test**

Add a fake Codex command test that sets `AGENTTEAM_RUN_LIVE_CODEX=1`, passes
`--codex-command python3 <fake>`, and asserts:

```python
self.assertEqual(summary["status"], "completed")
self.assertEqual(summary["scheduler_status"], "idle")
self.assertEqual(summary["processed_task_ids"], ["TASK-LIVE-CODEX-CLI-SMOKE"])
self.assertEqual(summary["state_index"]["tasks"][0]["task_status"], "done")
self.assertEqual(summary["state_index"]["runtime_sessions"][0]["runtime_adapter"], "CodexRuntimeAdapter")
self.assertEqual(summary["state_index"]["runtime_sessions"][0]["session_status"], "stopped")
```

- [x] **Step 3: Verify red**

Observed red:

```text
python3 -m agentteam_runtime.live_codex_cli_smoke ... returned non-zero exit status 1
```

- [x] **Step 4: Implement CLI smoke module**

Implement a module parallel to `live_codex_scheduler_smoke.py`, but call the
official CLI as a subprocess:

```bash
python3 -m agentteam_runtime.cli ... --run-until-idle --runtime codex
python3 -m agentteam_runtime.cli --output-dir <run> --show-state-index
```

- [x] **Step 5: Verify green**

Observed green:

```text
test_live_codex_cli_smoke_skips_without_env_gate ... ok
test_live_codex_cli_smoke_runs_with_fake_codex_command ... ok
```

### Task 3: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m12b-codex-runtime-cli.md`

- [x] **Step 1: Document M12b**

Document the preferred CLI shape:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog /path/to/backlog.json \
  --output-dir /tmp/agentteam-m0-run \
  --project-root /path/to/git/repo \
  --runtime codex
```

- [x] **Step 2: Run full verification**

Run unit tests, artifact lint, skip-gated live smoke commands, CLI regression,
JSON validation, compile check, and `git diff --check`.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 57 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_scheduler_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_cli_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

- [x] **Step 3: Try real Codex CLI runtime selector**

Run a bounded one-task real Codex CLI experiment through:

```bash
python3 -m agentteam_runtime.cli ... --runtime codex
```

Expected: the scheduler accepts the result, records a stopped
`CodexRuntimeAdapter` runtime session, and leaves the scheduler-loop state query
readable through `--show-state-index`.

Observed real Codex CLI smoke on 2026-06-02 with `codex-cli 0.132.0`:

```text
status: completed
scheduler_status: idle
processed_task_ids: ["TASK-LIVE-CODEX-CLI-SMOKE"]
changed_files: ["generated/live_codex_cli_smoke.json"]
expected_file_exists: true
state_index.tasks[0].task_status: done
state_index.runtime_sessions[0].runtime_adapter: CodexRuntimeAdapter
state_index.runtime_sessions[0].session_status: stopped
```

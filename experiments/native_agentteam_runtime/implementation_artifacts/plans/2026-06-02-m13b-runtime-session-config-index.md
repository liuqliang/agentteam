# M13b Runtime Session Config Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the effective runtime model, sandbox, and timeout for each runtime session so differentiated agent profiles are observable through events and the SQLite state index.

**Architecture:** Keep JSONL as the authority. Extend `runtime_session_started` payload with adapter metadata derived from the resolved runtime adapter. Replay carries that metadata into the `runtime_sessions` snapshot. SQLite state index stores the replayed metadata in additional columns and treats older indexes missing those columns as stale.

**Tech Stack:** Python 3.12 standard library, JSONL replay, SQLite, `unittest`.

---

### Task 1: Runtime Session Metadata

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing state-index test**

Run a scheduler loop with:

```python
CodexRuntimeAdapter(
    command=[sys.executable, str(fake_codex)],
    model="gpt-runtime-config",
    sandbox="read-only",
    timeout_seconds=30,
)
```

Assert that `read_scheduler_state_index(output_dir)["runtime_sessions"][0]`
contains:

```json
{
  "runtime_adapter": "CodexRuntimeAdapter",
  "runtime_model": "gpt-runtime-config",
  "runtime_sandbox": "read-only",
  "runtime_timeout_seconds": 30
}
```

- [x] **Step 2: Verify red**

Observed red:

```text
KeyError: 'runtime_model'
```

- [x] **Step 3: Emit and replay metadata**

Add runtime adapter metadata to `runtime_session_started`:

- `runtime_adapter`
- `runtime_model`
- `runtime_sandbox`
- `runtime_timeout_seconds`

Replay copies those fields into the runtime session snapshot.

- [x] **Step 4: Extend SQLite index**

Add columns:

- `runtime_model`
- `runtime_sandbox`
- `runtime_timeout_seconds`

Update state-index reads and make stale-index detection rebuild indexes that
have the old `runtime_sessions` schema.

- [x] **Step 5: Verify green**

Observed green:

```text
test_scheduler_state_index_records_runtime_session_config ... ok
```

### Task 2: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m13b-runtime-session-config-index.md`

- [x] **Step 1: Document M13b**

Document the additional `runtime_sessions` columns and the fact that older
SQLite indexes missing those columns are treated as stale.

- [x] **Step 2: Run full verification**

Run unit tests, artifact lint, live-smoke skip paths, CLI regression, JSON
validation, compile check, `git diff --check`, and a real Codex CLI smoke.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 60 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_scheduler_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_cli_smoke ... {"status": "skipped"}
AGENTTEAM_RUN_LIVE_CODEX=1 python3 -m agentteam_runtime.live_codex_cli_smoke ... {"status": "completed"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
```

- [x] **Step 3: Commit and push**

Commit and push the M13b milestone after verification passes.

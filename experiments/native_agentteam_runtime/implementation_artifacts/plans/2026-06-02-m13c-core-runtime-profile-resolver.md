# M13c Core Runtime Profile Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move agent `runtime_profile` interpretation from the CLI into the runtime core so future scheduler daemons, API servers, and test harnesses can reuse one resolver.

**Architecture:** Keep `runtime_adapter_factory` as an escape hatch, but make the default scheduler path resolve adapters in core:

1. explicit `runtime_adapter_factory`;
2. explicit Python `runtime_adapter`;
3. selected agent `runtime_profile`;
4. runtime fallback defaults passed by the caller;
5. `FakeRuntimeAdapter`.

The CLI now builds only a `runtime_profile_defaults` dict from global flags and passes it to `run_simulation(...)` or `run_scheduler_loop(...)`. It no longer interprets `agent.runtime_profile` directly.

**Tech Stack:** Python 3.12 standard library, `argparse`, `unittest`, Codex runtime adapter.

---

### Task 1: Core Resolver Behavior

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing core test**

Run `run_scheduler_loop(...)` directly with an agent pool whose selected agent has:

```json
{
  "runtime_profile": {
    "adapter": "codex",
    "command": ["python3", "<fake_codex.py>"],
    "model": "core-profile-model",
    "sandbox": "read-only",
    "timeout_seconds": 30
  }
}
```

Do not pass a CLI factory or explicit runtime adapter. Assert that the generated
file exists, the runtime session records `CodexRuntimeAdapter`, and the state
index reports the profile model/sandbox/timeout.

- [x] **Step 2: Verify red**

Observed red:

```text
FileNotFoundError: generated/core_profile.json
```

- [x] **Step 3: Implement core profile resolver**

Add `runtime_profile_defaults` to `run_simulation(...)`, `FileScheduler`, and
`run_scheduler_loop(...)`. Resolve selected-agent profiles in `m0_runtime.py`.

- [x] **Step 4: Verify green**

Observed green:

```text
test_scheduler_core_uses_agent_runtime_profile_without_cli_factory ... ok
```

### Task 2: CLI Simplification

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m13c-core-runtime-profile-resolver.md`

- [x] **Step 1: Pass fallback defaults**

Replace CLI-side agent profile parsing with `_build_runtime_profile_defaults(...)`.
The CLI still validates global runtime flags and converts them to a profile-like
fallback dict.

- [x] **Step 2: Verify focused CLI paths**

Observed green:

```text
test_cli_can_run_codex_runtime_adapter_command ... ok
test_cli_can_select_codex_runtime_with_command_override ... ok
test_cli_passes_codex_runtime_options_to_command ... ok
test_cli_rejects_codex_runtime_without_project_root ... ok
test_cli_uses_agent_runtime_profile_for_codex_options ... ok
test_scheduler_state_index_records_runtime_session_config ... ok
```

- [x] **Step 3: Run full verification**

Run unit tests, artifact lint, live-smoke skip paths, CLI regression, JSON
validation, compile check, `git diff --check`, and a real Codex CLI smoke.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 61 tests ... OK
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

- [x] **Step 4: Commit and push**

Commit and push the M13c milestone after verification passes.

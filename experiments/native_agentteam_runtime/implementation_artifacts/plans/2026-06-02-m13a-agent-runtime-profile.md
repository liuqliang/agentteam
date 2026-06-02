# M13a Agent Runtime Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `agent_pool` declare role/agent-specific runtime configuration so the scheduler can choose different Codex model, sandbox, and timeout settings per agent identity instead of relying only on global CLI flags.

**Architecture:** Keep task/backlog semantics unchanged. Add optional `agent.runtime_profile`. The CLI constructs a runtime adapter factory and passes it into the scheduler. At dispatch time, `run_simulation(...)` resolves the selected agent first, then asks the factory for that agent's adapter. Agent profile values override CLI fallback values; if no profile exists, the existing CLI runtime selector remains the fallback.

**Tech Stack:** Python 3.12 standard library, `argparse`, `unittest`, JSON schemas, `codex exec`.

---

### Task 1: Agent Profile Runtime Selection

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing profile test**

Add a test where the selected `repo_map_agent` has:

```json
{
  "runtime_profile": {
    "adapter": "codex",
    "model": "agent-profile-model",
    "sandbox": "read-only",
    "timeout_seconds": 30
  }
}
```

Run the CLI with a fake Codex command override. Assert that validation is
accepted, the runtime session records `CodexRuntimeAdapter`, and the fake Codex
command receives the profile model and sandbox.

- [x] **Step 2: Verify red**

Observed red:

```text
AssertionError: None != 'agent-profile-model'
```

- [x] **Step 3: Add runtime adapter factory**

Update `run_simulation(...)`, `FileScheduler`, and `run_scheduler_loop(...)` to
accept `runtime_adapter_factory(agent, task)`. Existing direct
`runtime_adapter=` calls remain supported.

- [x] **Step 4: Add CLI profile resolver**

Build a CLI factory that:

- reads `agent.runtime_profile` after the scheduler selects an agent;
- supports `adapter: fake|shell|codex`;
- lets profile `model`, `sandbox`, and `timeout_seconds` override CLI fallback
  values;
- uses `--codex-command` as a test/experiment command override;
- requires `--project-root` for Codex profiles.

- [x] **Step 5: Verify green**

Observed green:

```text
test_cli_uses_agent_runtime_profile_for_codex_options ... ok
```

### Task 2: Schema, Documentation, And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/schemas/agent_state.schema.json`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m13a-agent-runtime-profile.md`

- [x] **Step 1: Add schema field**

Add optional `runtime_profile` to `agent_state.schema.json`:

- `adapter`: `fake|shell|codex`
- `model`
- `sandbox`
- `timeout_seconds`
- `command`

- [x] **Step 2: Document M13a**

Document the agent-pool shape and precedence:

1. selected agent `runtime_profile`
2. CLI runtime options as fallback
3. fake adapter when neither exists

- [x] **Step 3: Run full verification**

Run unit tests, artifact lint, live-smoke skip paths, CLI regression, JSON
validation, compile check, and `git diff --check`.

Observed on 2026-06-02:

```text
python3 -m unittest discover ... Ran 59 tests ... OK
python3 -m agentteam_runtime.artifact_lint ... {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
python3 -m agentteam_runtime.live_codex_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_scheduler_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.live_codex_cli_smoke ... {"status": "skipped"}
python3 -m agentteam_runtime.cli ... exit 0
find ... jq empty ... exit 0
jq -c . sample_events.jsonl ... exit 0
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime ... exit 0
git diff --check ... exit 0
AGENTTEAM_RUN_LIVE_CODEX=1 python3 -m agentteam_runtime.live_codex_cli_smoke ... {"status": "completed"}
```

- [x] **Step 4: Commit and push**

Commit and push the M13a milestone after verification passes.

# M5 Integration Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run an explicit verification command in the integration worktree after
an accepted patch has been applied, without committing or merging.

**Architecture:** Keep verification opt-in. Add `integration_verification_command`
to `run_simulation`. When integration is applied and the command is present, run
it in the integration worktree, capture exit code/stdout/stderr, emit an
`integration_verified` event, and return verification metadata. CLI support is
deferred because current runtime command flags use `argparse.REMAINDER`.

**Tech Stack:** Python 3.12 standard library, `subprocess`, `unittest`, local
git worktrees.

---

### Task 1: Verification Command Runner

**Files:**
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing verification tests**

Add one passing test that runs:

```python
integration_verification_command=[
    sys.executable,
    "-c",
    "import pathlib; assert pathlib.Path('generated/integration_result.json').exists()",
]
```

Assert `integration_verification_status == "passed"` and replay stores the same
status.

Add one failing test that runs:

```python
integration_verification_command=[sys.executable, "-c", "import sys; sys.exit(7)"]
```

Assert `integration_verification_status == "failed"` and exit code `7`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py -v
```

Expected: fail because `integration_verification_command` is not accepted.

- [x] **Step 3: Implement runner**

Add `run_integration_verification(command, integration_worktree_path)` and wire
it after `patch_integrated`. Return:

```json
{
  "integration_verification_status": "passed",
  "integration_verification_exit_code": 0,
  "integration_verification_stdout": "",
  "integration_verification_stderr": ""
}
```

For non-zero exit, status is `"failed"` and the run result remains accepted but
not merge-ready.

- [x] **Step 4: Verify green**

Run focused tests. Expected: pass.

### Task 2: Documentation and Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m5-integration-verification.md`

- [x] **Step 1: Document M5 behavior**

Document that verification is opt-in, runs only after integration apply, and
does not trigger commit/merge.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m5
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m5-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- focused verification tests first failed because
  `integration_verification_command` was not accepted, then passed after
  implementation;
- unit test discovery ran 28 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression returned `integration_verification_status:
  "not_requested"`;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

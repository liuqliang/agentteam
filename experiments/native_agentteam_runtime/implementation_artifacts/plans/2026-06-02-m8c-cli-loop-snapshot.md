# M8c CLI Loop Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Include a replayed snapshot in `--run-until-idle` CLI output now that
scheduler loops write a canonical root event log.

**Architecture:** Keep `run_scheduler_loop(...)` unchanged. In `cli.py`, after
the scheduler loop returns, replay `result["events_path"]` and print
`{**result, "snapshot": snapshot}`. This mirrors the existing single-task CLI
output shape.

**Tech Stack:** Python 3.12 standard library, `argparse`, `unittest`.

---

### Task 1: CLI Loop Snapshot

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI snapshot test**

Extend the existing `--run-until-idle` CLI test to assert:

```python
self.assertEqual(summary["snapshot"]["tasks"]["TASK-001"]["task_status"], "done")
self.assertEqual(summary["snapshot"]["tasks"]["TASK-002"]["task_status"], "done")
self.assertEqual(
    set(summary["snapshot"]["leases"].keys()),
    {"TASK-001-LEASE-001", "TASK-002-LEASE-001"},
)
```

- [x] **Step 2: Verify red**

Run the focused CLI loop test. Expected: fail because loop CLI output does not
include `snapshot`.

- [x] **Step 3: Implement CLI snapshot**

In the `args.run_until_idle` branch:

```python
snapshot = replay_events(result["events_path"])
print(json.dumps({**result, "snapshot": snapshot}, sort_keys=True))
return
```

- [x] **Step 4: Verify green**

Run the focused CLI loop test and default CLI test. Expected: pass.

### Task 2: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m8c-cli-loop-snapshot.md`

- [x] **Step 1: Document M8c**

Document that both default CLI and loop CLI now include a `snapshot` field.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m8c
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m8c-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- focused CLI loop test first failed because loop output did not include
  `snapshot`;
- focused CLI tests passed after replaying `result["events_path"]` in the
  `--run-until-idle` branch;
- unit test discovery ran 39 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression kept the single-task summary plus replay snapshot;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

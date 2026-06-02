# M7c CLI Scheduler Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the file scheduler loop through the existing CLI without
changing default single-task CLI behavior.

**Architecture:** Add explicit CLI flags `--run-until-idle` and `--max-steps`.
When `--run-until-idle` is absent, keep the current `run_simulation(...)` path
and replayed snapshot output. When present, call `run_scheduler_loop(...)` and
print its scheduler summary.

**Tech Stack:** Python 3.12 standard library, `argparse`, `unittest`.

---

### Task 1: CLI Loop Flag

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing CLI loop test**

Add a CLI test with a two-task ready backlog:

```bash
python3 -m agentteam_runtime.cli \
  --agent-pool <fixture-agent-pool> \
  --backlog <two-task-backlog> \
  --output-dir <tmp-run> \
  --run-until-idle
```

Assert stdout JSON includes:

```python
self.assertEqual(summary["scheduler_status"], "idle")
self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
self.assertEqual(summary["step_count"], 2)
```

Read `summary["state_path"]` and assert both tasks are persisted as `done`.

- [x] **Step 2: Verify red**

Run the focused CLI test. Expected: fail because `--run-until-idle` is not
recognized.

- [x] **Step 3: Implement CLI switch**

Import `run_scheduler_loop` in `cli.py`. Add:

```python
parser.add_argument("--run-until-idle", action="store_true")
parser.add_argument("--max-steps", type=int, default=100)
```

If `args.run_until_idle` is true, call:

```python
result = run_scheduler_loop(..., max_steps=args.max_steps)
print(json.dumps(result, sort_keys=True))
return
```

Do not attach a replayed snapshot because M7a/M7b still write per-step event
logs rather than one unified scheduler event log.

- [x] **Step 4: Verify green**

Run the focused CLI test and existing default CLI test. Expected: pass.

### Task 2: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-02-m7c-cli-scheduler-loop.md`

- [x] **Step 1: Document M7c**

Document `--run-until-idle` and clarify that default CLI remains single-task.

- [x] **Step 2: Run full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.live_codex_smoke --output-dir /tmp/agentteam-live-codex-skip-m7c
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m7c-regression-run
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
```

Expected: all commands exit 0.

Observed on 2026-06-02:

- CLI loop test first failed with argparse exit status 2 because
  `--run-until-idle` was not recognized;
- focused CLI tests passed after adding `--run-until-idle` and `--max-steps`;
- unit test discovery ran 37 tests with `OK`;
- live Codex smoke without the env gate returned
  `{"reason": "set AGENTTEAM_RUN_LIVE_CODEX=1", "status": "skipped"}`;
- default CLI regression kept the single-task summary plus replay snapshot;
- JSON/JQ checks, `compileall`, and `git diff --check` exited 0.

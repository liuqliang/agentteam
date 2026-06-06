# M37 Operator Control Plane And Versioned Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add truthful operator visibility, safe stopping, sparse notifications, and side-by-side framework updates for AgentTeam.

**Architecture:** Add a small operator control layer on top of the existing file-backed runtime. Keep status/watch read-only, keep stop scoped to registered run processes, and make update install immutable releases for future runs instead of editing the active runtime in place.

**Tech Stack:** Python stdlib `argparse`, `json`, `pathlib`, `shutil`, `subprocess`, `time`, `os`, Linux `/proc` process inspection fallback, existing `unittest` subprocess CLI tests.

---

### Task 1: Run Liveness Summary

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_control.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] Write a failing test that builds a run directory with `scheduler_status=running`, no inflight attempts, and a worker registry whose worker is stopped. Assert `agentteam status --json` reports `liveness_status=running-stale` and human status includes `stale`.
- [ ] Write a failing test that builds a run directory with a live helper process recorded in `worker_process_registry.json`. Assert `agentteam status --json` reports `liveness_status=running-alive`.
- [ ] Implement `operator_control.build_run_liveness_summary(run_dir, profile=None)` that reads scheduler state, worker registry, events replay, registered PIDs, and heartbeat timestamps.
- [ ] Update `_build_run_status_summary` to include `liveness_status`, `runtime_release`, and a bounded `processes` summary.
- [ ] Update `taskpack list` so `run_status` uses liveness-aware status instead of raw scheduler status.
- [ ] Run focused tests, full taskpack tests, and `git diff --check`.
- [ ] Commit with `fix: report truthful run liveness`.

### Task 2: Watch Command

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_control.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] Write a failing test for `agentteam watch --run-dir <run> --interval 0 --max-lines 1` that asserts one compact progress line is printed and the command exits without mutating state.
- [ ] Add parser options `watch`, `--project-root`, `--taskpack`, `--run-dir`, `--interval`, `--max-lines`, and `--json-lines`.
- [ ] Implement event cursor helpers that read only new `events.jsonl` records between ticks.
- [ ] Print periodic human lines to stdout by default. Print event-specific lines immediately for dispatch, completion, blocked integration, manual gate, stopped, failed, and completed states.
- [ ] Stop watching automatically when liveness is terminal, unless future options add follow behavior.
- [ ] Run focused tests, full runtime tests, and `compileall`.
- [ ] Commit with `feat: add agentteam watch`.

### Task 3: Stop And Stale Cleanup

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_control.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] Write a failing test that starts a fake long-running worker process fixture, records its PID in a run registry, calls `agentteam stop --run-dir <run>`, and asserts the stop file is written and registry state becomes stopped.
- [ ] Write a failing test for `agentteam stop --stale --project-root <repo>` that cleans a stale `running` state without trying to kill any process.
- [ ] Implement `stop_run(run_dir, grace_seconds=5, force=False)` that writes registered stop files, waits for live registered PIDs, terminates only registered PIDs and descendants owned by the current user, and updates registry/state.
- [ ] Implement `cleanup_stale_runs(profile)` that only mutates runs whose liveness summary is `running-stale`.
- [ ] Add `agentteam stop` parser and JSON/human output.
- [ ] Ensure stop never matches processes by name such as `codex`; it must use registry PIDs and descendant discovery only.
- [ ] Run focused tests and verify no unrelated local Codex processes are touched.
- [ ] Commit with `feat: add scoped run stop`.

### Task 4: Run-Level Notifications

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] Write failing tests with a fake notification sink for `run_started`, `run_completed`, `run_failed`, `integration_blocked`, `manual_gate_required`, and `run_stale_detected`.
- [ ] Generalize the current Feishu manual-gate sink into an event-policy sink that formats bounded text for allowed event types.
- [ ] Keep default policy sparse: run start, run terminal state, failure, timeout, manual gate, integration blocked, stale detected, update activated, rollback activated.
- [ ] Ensure task-completed notifications are disabled by default.
- [ ] Ensure notification failures append telemetry but do not block scheduler progress.
- [ ] Run notification tests and full runtime tests.
- [ ] Commit with `feat: notify run-level operator events`.

### Task 5: Release Manager And Update Command

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/release_manager.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/profile.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] Write failing tests for creating a release from a clean fixture checkout into `<work-root>/releases/<release-id>` with a `manifest.json`.
- [ ] Write failing tests for `agentteam update --status` showing active release, known releases, active runs by release, and unmanaged active runs.
- [ ] Write failing tests for `agentteam update --from <checkout>` activating a new release without changing an existing run's recorded release id.
- [ ] Implement immutable release install: copy the launcher and runtime package into a release directory, write `manifest.json`, and refuse dirty source checkouts by default.
- [ ] Implement active release pointer as JSON, not an in-place runtime overwrite.
- [ ] Add `agentteam update --status`, `--from`, `--activate`, and `--rollback`.
- [ ] Record `runtime_release_id` and `runtime_release_root` for new `start` and `continue` launches.
- [ ] Report active unmanaged development-worktree runs as warnings.
- [ ] Run focused tests, full taskpack tests, and `compileall`.
- [ ] Commit with `feat: add versioned agentteam update`.

### Task 6: Stable Local Launcher

**Files:**
- Modify: `agentteam`
- Modify: `scripts/install-local.sh`
- Modify: `experiments/native_agentteam_runtime/README.md`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] Write a failing test that installs a fixture active release pointer and asserts the repository-root launcher dispatches through the active release runtime path.
- [ ] Update the local launcher so installed copies can read the active release pointer and prepend the active release runtime to `PYTHONPATH`.
- [ ] Update `scripts/install-local.sh` so `~/.local/bin/agentteam` is a stable launcher, not a symlink into the mutable development worktree.
- [ ] Keep development execution from the repository root working for tests.
- [ ] Document the new usage: `agentteam update --from <checkout>`, `agentteam update --status`, and rollback.
- [ ] Run full runtime tests and manually smoke `agentteam update --status`.
- [ ] Commit with `feat: install stable release launcher`.

### Task 7: Documentation And Roadmap Sync

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Modify: `experiments/native_agentteam_runtime/README.md`

- [ ] Update runtime behavior docs with liveness states, watch output, stop safety rules, and release update semantics.
- [ ] Update the roadmap status for M37 after implementation evidence is available.
- [ ] Add operator examples for `status`, `taskpack list`, `watch`, `stop`, and `update`.
- [ ] Run `git diff --check`.
- [ ] Commit with `docs: document operator control plane`.

### Verification

Run after each task:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack
git diff --check
```

Run before declaring M37 complete:

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime
agentteam taskpack list --project-root /home/liuql/projects/verisilicon
agentteam update --status
```


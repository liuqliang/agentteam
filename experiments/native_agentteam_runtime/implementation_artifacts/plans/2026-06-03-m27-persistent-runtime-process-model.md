# M27 Persistent Runtime Process Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume worker-pool visibility from an existing file-backed process registry.

**Architecture:** Add PID-attached mode to `FileMailboxWorkerProcessSupervisor` and a `resume_from_registry()` method to `FileMailboxWorkerPoolSupervisor`. Keep storage file-backed and avoid scheduler changes.

**Tech Stack:** Python 3.12 standard library, existing mailbox worker process supervisor, `unittest`.

---

### Task 1: Resume From Worker Registry

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/worker_pool.py`

- [x] **Step 1: Write failing resume test**

Start a worker pool, create a second `FileMailboxWorkerPoolSupervisor` for the
same output directory, call `resume_from_registry()`, assert resumed health is
running, then stop the resumed pool and assert the original process exits.

- [x] **Step 2: Verify red**

Run the focused resume test. Expected: `resume_from_registry` does not exist.

- [x] **Step 3: Implement attached PID mode**

Add `attach_existing_process(pid)` to `FileMailboxWorkerProcessSupervisor`.
When attached, `health()` checks PID liveness and `stop()` writes the stop file
and waits until the PID exits.

- [x] **Step 4: Implement worker-pool resume**

Add `FileMailboxWorkerPoolSupervisor.resume_from_registry()`. It reads
`worker_process_registry.json`, creates worker supervisors for known agent ids,
attaches live PIDs, writes a resumed registry summary, and returns it.

- [x] **Step 5: Verify green**

Run the focused resume test. Expected: pass.

### Task 2: Docs, Verification, Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Modify: runtime, test, and artifact files from Task 1.

- [x] **Step 1: Update docs and roadmap**

Add M27 notes to `m0_file_runtime.md`; mark M27 implemented in
`native_runtime_roadmap.md` and set the next recommended milestone to M28.

- [x] **Step 2: Run full verification**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime
git diff --check
```

- [x] **Step 3: Run placeholder scan**

```bash
rg -n 'TB[D]|TO[D]O|implement later|fill in details|Similar to|appropriate placeholder' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m27-persistent-runtime-process-model.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m27-persistent-runtime-process-model.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md
```

- [x] **Step 4: Commit and push**

```bash
git add experiments/native_agentteam_runtime
git commit -m "Add M27 persistent runtime process resume"
git push origin native-runtime-m0
```

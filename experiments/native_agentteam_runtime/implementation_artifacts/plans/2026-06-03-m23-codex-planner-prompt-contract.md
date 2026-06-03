# M23 Codex Planner Prompt Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Codex planner tasks return structured `task_proposal` results through the existing runtime result contract.

**Architecture:** Keep scheduler authority unchanged. Add a planner-specific prompt branch to `CodexRuntimeAdapter`, allow read-only planner execution through a fallback workspace, and reject fallback runs that dirty that checkout. Propagate the fallback workspace from CLI runtime profile defaults into mailbox worker processes.

**Tech Stack:** Python 3.12 standard library, existing `unittest` suite, file mailbox worker pool, two-phase scheduler.

---

### Task 1: Planner Prompt Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`

- [x] **Step 1: Write the failing prompt test**

Add a test that calls `CodexRuntimeAdapter._build_prompt(...)` with a
decomposition message and asserts that the prompt contains the planner proposal
contract:

```python
def test_codex_runtime_adapter_builds_planner_prompt_contract(self):
    message = {
        "message_id": "MSG-0001",
        "from_agent": "agent-scheduler",
        "to_agent": "agent-planner",
        "message_type": "dispatch_task",
        "correlation_id": "DECOMPOSE-M23-001:ATTEMPT-001",
        "created_at": "2026-06-03T00:00:00Z",
        "lease_expires_at": "2026-06-03T00:15:00Z",
        "payload": {
            "task_id": "DECOMPOSE-M23-001",
            "attempt_id": "DECOMPOSE-M23-001-ATTEMPT-001",
            "lease_id": "DECOMPOSE-M23-001-LEASE-001",
            "task_kind": "decompose_backlog",
            "milestone_id": "M23",
            "planner_context_path": "/tmp/planner_contexts/DECOMPOSE-M23-001.json",
            "objective": "Generate bounded backlog tasks.",
            "read_scope": ["."],
            "write_scope": [],
        },
    }

    prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

    self.assertIn("AgentTeam planner", prompt)
    self.assertIn("task_proposal", prompt)
    self.assertIn("DECOMPOSE-M23-001.json", prompt)
    self.assertIn('"changed_files": []', prompt)
    self.assertIn('"required_role"', prompt)
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_codex_runtime_adapter_builds_planner_prompt_contract \
  -v
```

Expected red: the prompt does not contain the planner-specific contract.

- [x] **Step 3: Implement the prompt branch**

In `CodexRuntimeAdapter._build_prompt`, branch on
`message["payload"].get("task_kind") == "decompose_backlog"` and return a
planner prompt. Keep the existing implementation prompt unchanged for all other
tasks.

- [x] **Step 4: Verify green**

Run the same focused unittest command. Expected: one passing test.

### Task 2: Fallback Workspace For Planner Tasks

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/worker_pool.py`

- [x] **Step 1: Write the failing fallback Codex planner test**

Add a fake Codex helper that reads the planner prompt, writes a
`task_proposal`, and does not modify the fallback repo. Add a test that calls
`CodexRuntimeAdapter(..., fallback_worktree_path=repo).run(message)` with no
`worktree_path`.

Expected result:

```python
self.assertEqual(result["result_status"], "completed")
self.assertEqual(result["changed_files"], [])
self.assertEqual(
    result["output"]["task_proposal"]["tasks"][0]["task_id"],
    "TASK-M23-CODEX-001",
)
```

- [x] **Step 2: Verify red**

Run the focused test. Expected red: `CodexRuntimeAdapter.__init__` does not
accept `fallback_worktree_path` or the run fails with `missing_worktree_path`.

- [x] **Step 3: Implement fallback path and dirty-check**

Add `fallback_worktree_path=None` to `CodexRuntimeAdapter`. In `run`, use
`worktree_path or fallback_worktree_path` as the Codex `-C` directory. When the
fallback path is used, snapshot `_git_changed_files(...)` before and after
execution and reject with `error=fallback_worktree_modified` if the checkout
changes.

- [x] **Step 4: Propagate fallback path from profiles and worker processes**

Pass `fallback_worktree_path` through:

- `_runtime_adapter_from_profile(...)`;
- `_build_runtime_profile_defaults(...)`, using `args.project_root`;
- `FileMailboxWorkerProcessSupervisor`;
- `FileMailboxWorkerPoolSupervisor`;
- `agentteam_runtime.mailbox_worker` CLI.

- [x] **Step 5: Verify focused green**

Run the fallback planner test. Expected: pass.

### Task 3: Two-Phase Fake Codex Planner CLI Path

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write the failing CLI integration test**

Add a test that runs:

```bash
python3 -m agentteam_runtime.cli \
  --agent-pool <agent_pool.json> \
  --backlog <empty_backlog.json> \
  --output-dir <run> \
  --project-root <repo> \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --auto-decompose-backlog \
  --decomposition-milestone-id M23 \
  --decomposition-planner-role task_planner \
  --decomposition-default-worker-role repo_map_agent \
  --runtime codex \
  --codex-command <fake_codex_planner.py> \
  --max-steps 10
```

The fake Codex command should return a planner proposal for planner prompts and
write `generated/codex_generated_worker.json` for generated worker prompts.

- [x] **Step 2: Verify red or existing partial failure**

The no-worktree Codex planner failure was verified by the focused fallback tests
before fallback implementation. The CLI integration test was added after that
implementation and exercises the same path through the worker pool.

- [x] **Step 3: Verify green after fallback propagation**

Run the focused test again. Expected: the scheduler reaches `idle`, processes
`DECOMPOSE-M23-001` and `TASK-M23-CODEX-001`, and the state index marks both
tasks done.

- [x] **Step 4: Update runtime implementation docs**

Add M23 notes to `implementation_artifacts/m0_file_runtime.md`, including the
planner prompt contract, fallback workspace behavior, and fake Codex CLI path.

### Task 4: Full Verification And Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m23-codex-planner-prompt-contract.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m23-codex-planner-prompt-contract.md`
- Modify: runtime and test files from Tasks 1 to 3.

- [x] **Step 1: Run full runtime tests**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
```

- [x] **Step 2: Run artifact lint**

```bash
env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
```

- [x] **Step 3: Run syntax and repository checks**

```bash
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime
git diff --check
rg -n 'TB[D]|TO[D]O|implement later|fill in details|Similar to|appropriate placeholder' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m23-codex-planner-prompt-contract.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m23-codex-planner-prompt-contract.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md
```

- [x] **Step 4: Commit and push**

```bash
git add experiments/native_agentteam_runtime
git commit -m "Add M23 Codex planner prompt contract"
git push origin native-runtime-m0
```

# M21 Planner Proposal Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in automatic backlog decomposition by dispatching a planner agent and accepting only validated task proposals.

**Architecture:** Keep the scheduler deterministic. Add a focused task-proposal validator, let `TwoPhaseFileScheduler` synthesize one planner task when idle, and apply accepted planner proposals to scheduler state before normal dispatch continues.

**Tech Stack:** Python 3.12 standard library, existing two-phase scheduler, existing mailbox worker pool, `unittest`.

---

### Task 1: Task Proposal Validator

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing validator tests**

Add imports:

```python
from agentteam_runtime import normalize_task_proposal
```

Add tests:

```python
def test_task_proposal_normalizes_valid_generated_tasks(self):
    proposal = {
        "milestone_id": "M21",
        "tasks": [
            {
                "task_id": "TASK-M21-001",
                "objective": "Add a bounded generated task.",
                "read_scope": ["experiments/native_agentteam_runtime/"],
                "write_scope": ["experiments/native_agentteam_runtime/generated/"],
                "required_role": "repo_map_agent",
                "risk_target": "L1",
                "depends_on": [],
                "blockers": [],
            }
        ],
    }

    normalized = normalize_task_proposal(proposal, existing_task_ids={"DECOMPOSE-M21-001"})

    self.assertEqual(normalized["proposal_status"], "accepted")
    self.assertEqual(normalized["generated_task_ids"], ["TASK-M21-001"])
    self.assertEqual(normalized["tasks"][0]["backlog_status"], "ready")
    self.assertEqual(normalized["tasks"][0]["milestone_id"], "M21")
```

```python
def test_task_proposal_rejects_duplicate_existing_task_id(self):
    proposal = {
        "milestone_id": "M21",
        "tasks": [
            {
                "task_id": "TASK-M21-001",
                "objective": "Duplicate task id.",
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "required_role": "repo_map_agent",
                "risk_target": "L0",
                "depends_on": [],
                "blockers": [],
            }
        ],
    }

    with self.assertRaisesRegex(ValueError, "duplicate task_id"):
        normalize_task_proposal(proposal, existing_task_ids={"TASK-M21-001"})
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_task_proposal_normalizes_valid_generated_tasks \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_task_proposal_rejects_duplicate_existing_task_id \
  -v
```

Expected red: `normalize_task_proposal` is not exported.

Observed red:

```text
ImportError: cannot import name 'normalize_task_proposal' from 'agentteam_runtime'
```

- [x] **Step 3: Implement validator**

Create `task_proposal.py` with `normalize_task_proposal(proposal, existing_task_ids=None)`.

Rules:

- `proposal` must be a dict;
- `tasks` must be a non-empty list;
- each generated task requires string `task_id`, `objective`, `required_role`, and `risk_target`;
- `read_scope`, `write_scope`, `depends_on`, and `blockers` must be lists of strings;
- `backlog_status` defaults to `ready` and must be `ready` or `blocked`;
- `milestone_id` defaults from the proposal-level `milestone_id`;
- generated tasks may not use `task_kind=decompose_backlog`;
- dependencies must reference an existing task id or another generated task id;
- duplicate ids are rejected.

Export `normalize_task_proposal` from `__init__.py`.

- [x] **Step 4: Verify green**

Run the focused validator tests again. Expected: pass.

Observed green:

```text
Ran 2 tests in 0.000s

OK
```

### Task 2: Scheduler Planner Dispatch

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing auto-dispatch test**

Add a test that creates an empty backlog and an agent pool with a `task_planner`
agent:

```python
def test_two_phase_scheduler_dispatches_planner_task_when_auto_decompose_is_idle(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
        agent_pool_path = tmp_path / "agent_pool.json"
        _write_agent_pool_with_agent_roles(
            agent_pool_path,
            [("agent-planner", "task_planner"), ("agent-repo-map", "repo_map_agent")],
        )
        scheduler = TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=FixedClock(),
            auto_decompose=True,
            decomposition_milestone_id="M21",
        )

        dispatch = scheduler.dispatch_ready()

        self.assertEqual(dispatch["dispatch_status"], "dispatched")
        self.assertEqual(dispatch["dispatched_task_ids"], ["DECOMPOSE-M21-001"])
        self.assertEqual(scheduler.state["backlog"]["items"][0]["task_kind"], "decompose_backlog")
        self.assertEqual(scheduler.state["backlog"]["items"][0]["required_role"], "task_planner")
```

- [x] **Step 2: Verify red**

Run the focused test. Expected red: `TwoPhaseFileScheduler.__init__()` does not
accept `auto_decompose`.

Observed red:

```text
TypeError: TwoPhaseFileScheduler.__init__() got an unexpected keyword argument 'auto_decompose'
```

- [x] **Step 3: Implement planner task enqueue**

Add constructor settings:

```python
auto_decompose=False
decomposition_milestone_id="M21"
decomposition_planner_role="task_planner"
decomposition_default_worker_role="repo_map_agent"
```

Before `dispatch_ready()` computes capacity, call an internal
`_ensure_decomposition_task()` that appends `DECOMPOSE-<milestone>-001` only
when:

- auto-decomposition is enabled;
- no inflight attempts exist;
- `_ready_tasks()` is empty;
- no existing task has `task_kind=decompose_backlog`.

- [x] **Step 4: Verify green**

Run the focused scheduler auto-dispatch test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.052s

OK
```

### Task 3: Apply Planner Proposal Results

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing proposal-apply test**

Dispatch the planner task, append a runtime result containing
`output.task_proposal`, collect results, and assert that generated tasks are now
in scheduler state:

```python
def test_two_phase_scheduler_applies_planner_task_proposal_to_backlog(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "run"
        backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
        agent_pool_path = tmp_path / "agent_pool.json"
        _write_agent_pool_with_agent_roles(
            agent_pool_path,
            [("agent-planner", "task_planner"), ("agent-repo-map", "repo_map_agent")],
        )
        scheduler = TwoPhaseFileScheduler(
            agent_pool_path,
            backlog_path,
            output_dir,
            clock=FixedClock(),
            auto_decompose=True,
            decomposition_milestone_id="M21",
        )

        scheduler.dispatch_ready()
        inflight = scheduler.state["inflight_attempts"][0]
        _append_runtime_result_with_output(
            inflight["outbox_path"],
            inflight["message_id"],
            "DECOMPOSE-M21-001",
            inflight["attempt_id"],
            inflight["lease_id"],
            "completed",
            [],
            {
                "task_proposal": {
                    "milestone_id": "M21",
                    "tasks": [
                        {
                            "task_id": "TASK-M21-001",
                            "objective": "Run generated worker task.",
                            "read_scope": ["."],
                            "write_scope": ["generated/"],
                            "required_role": "repo_map_agent",
                            "risk_target": "L0",
                            "depends_on": [],
                            "blockers": [],
                        }
                    ],
                }
            },
        )

        collected = scheduler.collect_ready_results()

        self.assertEqual(collected["results"][0]["decomposition_status"], "applied")
        self.assertEqual(collected["results"][0]["generated_task_ids"], ["TASK-M21-001"])
        self.assertEqual(
            [item["task_id"] for item in scheduler.state["backlog"]["items"]],
            ["DECOMPOSE-M21-001", "TASK-M21-001"],
        )
```

- [x] **Step 2: Verify red**

Run the focused test. Expected red: result lacks `decomposition_status`.

Observed red:

```text
KeyError: 'decomposition_status'
```

- [x] **Step 3: Implement proposal apply**

In `_collect_result()`, after an accepted `decompose_backlog` task, call
`normalize_task_proposal()` with current backlog ids. Append normalized tasks to
`self.state["backlog"]["items"]`, attach `decomposition_status` and
`generated_task_ids` to the result, and add one `backlog_updated` event with
`update_type=decomposition_applied`.

If validation raises `ValueError`, mark the result with
`decomposition_status=rejected`, `failure_category=invalid_task_proposal`, and
block the planner task.

- [x] **Step 4: Verify green**

Run the focused proposal-apply test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.053s

OK
```

### Task 4: Fake Planner Runtime And CLI

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write failing CLI test**

Create a temp agent pool with `task_planner` and `repo_map_agent`, an empty
backlog, and run:

```bash
python3 -m agentteam_runtime.cli \
  --agent-pool <agent_pool> \
  --backlog <empty_backlog> \
  --output-dir <output_dir> \
  --daemon-run-until-idle \
  --daemon-two-phase-worker-pool \
  --auto-decompose-backlog \
  --decomposition-milestone-id M21 \
  --decomposition-planner-role task_planner \
  --decomposition-default-worker-role repo_map_agent
```

Assert:

```python
self.assertEqual(summary["daemon_status"], "idle")
self.assertIn("DECOMPOSE-M21-001", summary["processed_task_ids"])
self.assertIn("TASK-M21-GENERATED-001", summary["processed_task_ids"])
```

- [x] **Step 2: Verify red**

Run the focused CLI test. Expected red: CLI does not recognize
`--auto-decompose-backlog`.

Observed red:

```text
cli.py: error: unrecognized arguments: --auto-decompose-backlog --decomposition-milestone-id M21 --decomposition-planner-role task_planner --decomposition-default-worker-role repo_map_agent
```

- [x] **Step 3: Implement fake planner and CLI flags**

In `FakeRuntimeAdapter.run()`, when `payload.task_kind == "decompose_backlog"`,
return a completed result with:

```json
{
  "task_proposal": {
    "milestone_id": "M21",
    "tasks": [
      {
        "task_id": "TASK-M21-GENERATED-001",
        "objective": "Run generated worker task for M21.",
        "read_scope": ["."],
        "write_scope": ["generated/"],
        "required_role": "repo_map_agent",
        "risk_target": "L0",
        "depends_on": [],
        "blockers": []
      }
    ]
  }
}
```

In CLI, add the four decomposition flags and pass them to
`TwoPhaseFileScheduler` through `_run_supervised_two_phase_scheduler()`.

- [x] **Step 4: Verify green**

Run the focused CLI test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.212s

OK
```

### Task 5: Full Verification And Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m21-planner-proposal-decomposition.md`

- [x] **Step 1: Full verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -p 'test*.py' -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
find experiments/native_agentteam_runtime -name '*.json' -exec jq empty {} +
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
python3 -m compileall -q experiments/native_agentteam_runtime/m0_runtime
git diff --check
rg -n 'TB[D]|TO[D]O|implement[ ]later|fill[ ]in[ ]details|Similar[ ]to|approp[r]iate' \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m21-planner-proposal-decomposition.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m21-planner-proposal-decomposition.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md
```

- [x] **Step 2: Record observed verification**

Update this plan with exact pass or failure lines from the verification commands.

Observed verification:

```text
unittest discover: Ran 91 tests in 4.286s
unittest discover: OK
artifact_lint: {"status": "passed", "checked_json_files": 21, "checked_jsonl_files": 1}
find *.json jq empty: exit 0
jq -c sample_events.jsonl: exit 0
compileall: exit 0
git diff --check: exit 0
placeholder scan: exit 1 with no matches, expected
```

- [ ] **Step 3: Commit and push**

Commit:

```bash
git add \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m21-planner-proposal-decomposition.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m21-planner-proposal-decomposition.md
git commit -m "Add M21 planner proposal decomposition"
git push origin native-runtime-m0
```

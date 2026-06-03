# M22 Planner Context Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and enforce a bounded planner context package for automatic backlog decomposition.

**Architecture:** Add a focused planner-context module, attach context files to synthetic planner tasks, and extend proposal validation with role and write-scope constraints derived from that context.

**Tech Stack:** Python 3.12 standard library, existing two-phase scheduler, existing mailbox worker pool, `unittest`.

---

### Task 1: Planner Context Builder

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/planner_context.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing planner context test**

Add import:

```python
from agentteam_runtime import build_planner_context
```

Add test:

```python
def test_build_planner_context_summarizes_state_roles_and_scopes(self):
    agent_pool = {
        "agents": [
            {"agent_id": "agent-planner", "role": "task_planner"},
            {"agent_id": "agent-repo-map", "role": "repo_map_agent"},
        ]
    }
    state = {
        "backlog": {
            "items": [
                {"task_id": "TASK-DONE", "backlog_status": "done"},
                {"task_id": "TASK-BLOCKED", "backlog_status": "blocked"},
            ]
        },
        "steps": [{"task_id": "TASK-DONE", "step_status": "processed"}],
        "inflight_attempts": [],
    }

    context = build_planner_context(
        agent_pool,
        state,
        milestone_id="M22",
        default_worker_role="repo_map_agent",
        allowed_read_scopes=["."],
        allowed_write_scopes=["generated/"],
    )

    self.assertEqual(context["context_schema_version"], "planner_context.v1")
    self.assertEqual(context["milestone_id"], "M22")
    self.assertEqual(context["default_worker_role"], "repo_map_agent")
    self.assertEqual(context["allowed_write_scopes"], ["generated/"])
    self.assertEqual(context["available_agent_roles"], ["repo_map_agent", "task_planner"])
    self.assertEqual(context["backlog_summary"]["done"], 1)
    self.assertEqual(context["backlog_summary"]["blocked"], 1)
    self.assertEqual(context["completed_task_ids"], ["TASK-DONE"])
    self.assertIn("proposal_contract", context)
```

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_build_planner_context_summarizes_state_roles_and_scopes \
  -v
```

Expected red: `build_planner_context` is not exported.

Observed red:

```text
ImportError: cannot import name 'build_planner_context' from 'agentteam_runtime'
```

- [x] **Step 3: Implement context builder**

Create `planner_context.py` with:

```python
def build_planner_context(agent_pool, state, milestone_id, default_worker_role, allowed_read_scopes=None, allowed_write_scopes=None):
    ...
```

Rules:

- default `allowed_read_scopes` to `["."]`;
- default `allowed_write_scopes` to `["generated/"]`;
- return sorted unique available roles;
- summarize backlog statuses using keys `total`, `ready`, `blocked`, `done`, and `other`;
- use processed steps as `completed_task_ids`;
- include `proposal_contract` with required fields and forbidden planner task kind.

Export `build_planner_context` from `__init__.py`.

- [x] **Step 4: Verify green**

Run the focused context builder test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.000s

OK
```

### Task 2: Context-Aware Proposal Validation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing validator constraint tests**

Add tests:

```python
def test_task_proposal_rejects_unknown_required_role(self):
    proposal = {
        "milestone_id": "M22",
        "tasks": [
            {
                "task_id": "TASK-M22-001",
                "objective": "Use an unknown role.",
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "required_role": "unknown_role",
                "risk_target": "L0",
                "depends_on": [],
                "blockers": [],
            }
        ],
    }

    with self.assertRaisesRegex(ValueError, "unknown required_role"):
        normalize_task_proposal(proposal, allowed_roles={"repo_map_agent"})
```

```python
def test_task_proposal_rejects_write_scope_outside_allowed_prefix(self):
    proposal = {
        "milestone_id": "M22",
        "tasks": [
            {
                "task_id": "TASK-M22-001",
                "objective": "Write outside generated scope.",
                "read_scope": ["."],
                "write_scope": ["src/"],
                "required_role": "repo_map_agent",
                "risk_target": "L0",
                "depends_on": [],
                "blockers": [],
            }
        ],
    }

    with self.assertRaisesRegex(ValueError, "write_scope outside allowed scope"):
        normalize_task_proposal(
            proposal,
            allowed_roles={"repo_map_agent"},
            allowed_write_scopes=["generated/"],
        )
```

- [x] **Step 2: Verify red**

Run the two focused validator tests. Expected red:
`normalize_task_proposal()` does not accept `allowed_roles`.

Observed red:

```text
TypeError: normalize_task_proposal() got an unexpected keyword argument 'allowed_roles'
```

- [x] **Step 3: Implement constraints**

Extend `normalize_task_proposal()` with:

```python
allowed_roles=None
allowed_write_scopes=None
```

Reject a task when:

- `required_role` is not in `allowed_roles`, if provided;
- any `write_scope` entry does not start with one allowed prefix, if provided.

Keep existing callers compatible by defaulting both arguments to `None`.

- [x] **Step 4: Verify green**

Run the two focused validator tests again. Expected: pass.

Observed green:

```text
Ran 3 tests in 0.000s

OK
```

### Task 3: Scheduler Writes Context Package

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing scheduler context test**

Extend the auto-decompose dispatch test to assert:

```python
planner_task = scheduler.state["backlog"]["items"][0]
context_path = Path(planner_task["planner_context_path"])
context = json.loads(context_path.read_text(encoding="utf-8"))
message = _read_first_jsonl(
    output_dir / "steps" / "STEP-0001-DECOMPOSE-M22-001" / "mailboxes" / "agent-planner" / "inbox.jsonl"
)

self.assertTrue(context_path.exists())
self.assertEqual(context["milestone_id"], "M22")
self.assertEqual(context["allowed_write_scopes"], ["generated/"])
self.assertEqual(message["payload"]["planner_context_path"], str(context_path))
```

Use `decomposition_milestone_id="M22"`.

- [x] **Step 2: Verify red**

Run the focused auto-decompose dispatch test. Expected red:
`planner_context_path` is missing.

Observed red:

```text
KeyError: 'planner_context_path'
```

- [x] **Step 3: Implement context file creation**

In `TwoPhaseFileScheduler`:

- add constructor arguments `decomposition_allowed_read_scopes=None` and
  `decomposition_allowed_write_scopes=None`;
- store defaults `["."]` and `["generated/"]`;
- in `_ensure_decomposition_task()`, call `build_planner_context()`;
- write JSON to
  `<output-dir>/planner_contexts/DECOMPOSE-<milestone>-001.json`;
- put `planner_context_path`, `allowed_write_scopes`, and
  `allowed_read_scopes` on the synthetic task;
- include `planner_context_path` in dispatch payload through the existing task
  fields.

- [x] **Step 4: Verify green**

Run the focused auto-decompose dispatch test again. Expected: pass.

Observed green:

```text
Ran 1 test in 0.051s

OK
```

### Task 4: Apply Proposals With Context Constraints And Fake Planner Reads Context

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Write failing context-enforced scheduler test**

Add a test that sends a planner proposal using `write_scope=["src/"]` while the
context allows only `generated/`, then assert:

```python
self.assertEqual(collected["results"][0]["decomposition_status"], "rejected")
self.assertEqual(collected["results"][0]["failure_category"], "invalid_task_proposal")
self.assertEqual(len(scheduler.state["backlog"]["items"]), 1)
```

- [x] **Step 2: Verify red**

Run the focused test. Expected red: invalid proposal is applied because scheduler
does not pass context constraints into `normalize_task_proposal()`.

Observed red:

```text
AssertionError: 'applied' != 'rejected'
```

- [x] **Step 3: Implement context-constrained apply and fake planner context read**

In `_apply_decomposition_result()`, read the planner context from
`planner_context_path` and pass:

```python
allowed_roles=context["available_agent_roles"]
allowed_write_scopes=context["allowed_write_scopes"]
```

to `normalize_task_proposal()`.

In `FakeRuntimeAdapter.run()`, when `task_kind == "decompose_backlog"`, read
`planner_context_path` if present and use:

- `milestone_id`;
- `default_worker_role`;
- first `allowed_write_scopes` entry.

Update `m0_file_runtime.md` with the M22 behavior and limits.

- [x] **Step 4: Verify green**

Run the context-enforced scheduler test and the existing CLI auto-decompose test.
Expected: pass.

Observed green:

```text
context-enforced scheduler test: Ran 1 test in 0.027s, OK
CLI auto-decompose test: Ran 1 test in 0.210s, OK
```

### Task 5: Full Verification And Commit

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m22-planner-context-package.md`

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
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m22-planner-context-package.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m22-planner-context-package.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md
```

- [x] **Step 2: Record observed verification**

Update this plan with exact pass or failure lines from the verification commands.

Observed verification:

```text
unittest discover: Ran 95 tests in 4.116s
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
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/planner_context.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/task_proposal.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m22-planner-context-package.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m22-planner-context-package.md
git commit -m "Add M22 planner context package"
git push origin native-runtime-m0
```

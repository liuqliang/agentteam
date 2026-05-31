# M0 File Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standard-library Python M0 runtime simulation that proves deterministic scheduling, mailbox dispatch, fake runtime result validation, event replay, and authority-gated state updates.

**Architecture:** Keep M0 as a small file-backed package under the native runtime experiment. `m0_runtime.py` owns deterministic runtime behavior, tests drive public behavior, and a tiny CLI runs the simulation against fixture files. No Codex, Claude Code, SQLite, MCP, or A2A integration is included in M0.

**Tech Stack:** Python 3.12 standard library, `unittest`, JSON/JSONL files.

---

### Task 1: Runtime Test Harness And Core Simulation

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Create: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**

Create `test_m0_runtime.py` with tests that import `run_simulation` and `replay_events`, copy the existing fixtures into a temporary output directory, run one simulation, and assert:

```python
result = run_simulation(agent_pool_path, backlog_path, output_dir, clock=FixedClock())
assert result["task_id"] == "TASK-001"
assert result["attempt_id"] == "ATTEMPT-001"
assert result["worktree_id"] == "WT-ATTEMPT-001"
assert result["validation_status"] == "accepted"
```

Also assert that replay reconstructs the done task only after a validation event:

```python
snapshot = replay_events(events_path)
assert snapshot["tasks"]["TASK-001"]["task_status"] == "done"
assert snapshot["attempts"]["ATTEMPT-001"]["validation_status"] == "accepted"
```

- [x] **Step 2: Verify red**

Run:

```bash
python3 -m unittest discover experiments/native_agentteam_runtime/m0_runtime/tests -v
```

Expected: fail with `ModuleNotFoundError` or missing `run_simulation`.

- [x] **Step 3: Implement minimal runtime**

Create `m0_runtime.py` with these public functions:

```python
def run_simulation(agent_pool_path, backlog_path, output_dir, clock=None):
    ...

def replay_events(events_path):
    ...
```

M0 behavior:

- load fixture JSON;
- select the first ready task whose required role has an idle agent;
- create deterministic `ATTEMPT-001`, `LEASE-001`, `MSG-0001`, and `WT-ATTEMPT-001`;
- write one mailbox dispatch JSONL record;
- write events for task selection, lease acquisition, worktree creation, dispatch, fake runtime output, validation acceptance, and backlog completion;
- return a compact result dictionary.

- [x] **Step 4: Verify green**

Run:

```bash
python3 -m unittest discover experiments/native_agentteam_runtime/m0_runtime/tests -v
```

Expected: all M0 runtime tests pass.

### Task 2: Schema Alignment And CLI

**Files:**
- Modify: `experiments/native_agentteam_runtime/schemas/event.schema.json`
- Modify: `experiments/native_agentteam_runtime/schemas/mailbox_message.schema.json`
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`

- [x] **Step 1: Write failing tests**

Add tests that assert emitted event types and mailbox message types are allowed
by the local schema enum values.

- [x] **Step 2: Verify red**

Run:

```bash
python3 -m unittest discover experiments/native_agentteam_runtime/m0_runtime/tests -v
```

Expected: fail because the current schemas do not allow all semantic M0 event
or message names.

- [x] **Step 3: Align schema enum values and add CLI**

Extend existing schema enum values without removing old fixture-compatible
values. Add a CLI entry point that runs:

```bash
python3 -m agentteam_runtime.cli \
  --agent-pool experiments/native_agentteam_runtime/fixtures/sample_agent_pool.json \
  --backlog experiments/native_agentteam_runtime/fixtures/sample_backlog.json \
  --output-dir /tmp/agentteam-m0-run
```

- [x] **Step 4: Verify green**

Run the unit tests and CLI command. Expected: tests pass and CLI prints a JSON
summary with `validation_status` set to `accepted`.

### Task 3: Documentation And Verification Record

**Files:**
- Create: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`

- [x] **Step 1: Document the implemented M0 boundary**

Describe what the M0 runtime proves, what it intentionally fakes, and what must
be true before M1 real runtime backend integration.

- [x] **Step 2: Run final checks**

Run:

```bash
python3 -m unittest discover experiments/native_agentteam_runtime/m0_runtime/tests -v
find experiments/native_agentteam_runtime -name '*.json' -print0 | xargs -0 jq empty
jq -c . experiments/native_agentteam_runtime/fixtures/sample_events.jsonl
git diff --check
```

Expected: all commands pass.

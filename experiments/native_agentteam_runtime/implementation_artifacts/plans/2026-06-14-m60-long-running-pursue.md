# M60 Long-Running Pursue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a conservative long-running goal loop that can execute `start -> next -> next` style rounds within explicit budgets and stop at operator gates.

**Architecture:** Add `agentteam pursue` as a thin operator command over existing taskpack authoring and runtime execution. It does not create a new scheduler, does not merge source changes, and does not bypass manual gate, permission, blocker, or integration review stops. Each round remains an ordinary taskpack/run with existing reports and artifacts.

**Tech Stack:** Python standard library, existing AgentTeam CLI helpers, existing fake runtime for deterministic tests, unittest.

---

### Task 1: Add Pursue CLI Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing parser/help tests**
  Add tests that `agentteam help` lists `pursue` and that `agentteam pursue --help` exposes `--max-rounds`, `--stop-on-review-gate`, and `--allow-review-gate-follow-up`.

- [x] **Step 2: Run focused tests and observe failure**
  Run `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_agentteam_cli_help_lists_pursue`.

- [x] **Step 3: Implement parser and help entry**
  Add `_add_pursue_parser`, register it in `_build_parser`, and add `_HELP_COMMANDS` entry.

### Task 2: Implement Bounded Round Loop

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing fake-runtime CLI test**
  Initialize a temp repo/profile with fake author/runtime and run `agentteam pursue --goal ... --taskpack-id pursue-loop --max-rounds 1 --json`. Assert JSON contains `pursue_status`, `rounds_completed`, `runs[0].taskpack_id`, and `stop_reason`.

- [x] **Step 2: Run focused test and observe failure**
  Run the focused pursue CLI test.

- [x] **Step 3: Implement `_handle_pursue`**
  Load profile, build submit args from profile, run `_handle_submit` for round 1, then loop while budget remains and stop conditions allow.

### Task 3: Stop Conditions And Follow-Up Goal

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing unit tests for stop decisions**
  Add focused tests for manual gate, permission request, blocked report, integration review gate, and max rounds.

- [x] **Step 2: Run focused tests and observe failure**
  Run the stop-decision tests.

- [x] **Step 3: Implement stop helpers**
  Add `_pursue_stop_reason`, `_pursue_next_goal`, and `_pursue_round_record`. Use existing `completion_summary.follow_up_recommendation` and `next_steps`; do not infer unstated progress.

### Task 4: Text Output, Docs, Roadmap

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing text-output test**
  Assert non-JSON pursue output includes status, completed rounds, stop reason, latest taskpack id, and latest report path.

- [x] **Step 2: Implement text renderer and docs**
  Add `_write_pursue_result_text`, command reference section, and M60 roadmap entry.

### Final Verification

- [x] Run `git diff --check`.
- [x] Run `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime`.
- [x] Commit and push the implementation branch.

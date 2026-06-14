# M50-M54 Runtime Usability Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Close the next usability loop after M49: validate retention safety, improve Chinese operator summaries, harden optimization task decomposition, suggest follow-up work, and make permission blocks easier to act on.

**Architecture:** Keep authoritative artifacts file-backed. SQLite remains a rebuildable projection used for validation and summaries, not runtime authority. Existing commands stay stable; changes add structured fields, validation checks, and concise operator-facing text rather than new control-plane concepts.

**Tech Stack:** Python standard library, SQLite projection helpers, existing AgentTeam CLI, existing unittest suites.

---

### Task 1: M50 Projection-Backed Retention Validation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/projection_db.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing tests**
  Add tests that build a temporary work_root with rebuildable artifacts, rebuild the projection DB, and assert `agentteam gc --artifacts` style retention plans include `validation_status`, `validated_candidate_count`, and hash/size validation metadata. Add a stale/missing DB case that remains unavailable with `next_action: run agentteam db rebuild`.

- [x] **Step 2: Run focused test to verify failure**
  Run:
  `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_projected_artifact_retention_plan_validates_candidate_hashes`

- [x] **Step 3: Implement validation**
  Add a projection reader helper that rechecks candidate artifact existence, size, and sha256 against the DB rows. Return validation counts and invalid candidate records. Keep `deletion_enabled: false`.

- [x] **Step 4: Run focused and related tests**
  Run the M50 focused tests plus existing DB/stats/gc tests in `test_taskpack.py`.

### Task 2: M51 Stronger Chinese Operator Report

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_brief.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing tests**
  Add a report fixture with changed files, verification, measured result, integration recommendation, and next steps. Assert the completion summary exposes `operator_digest` with Chinese labels for what changed, files, verification, result, merge recommendation, and next action. Assert concise terminal lines and Feishu text include the digest.

- [x] **Step 2: Run focused test to verify failure**
  Run:
  `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_completion_summary_includes_chinese_operator_digest`

- [x] **Step 3: Implement digest**
  Build the digest deterministically from existing structured fields only. Do not translate raw logs or infer unstated performance gains.

- [x] **Step 4: Run report and notification tests**
  Run the focused report/notification tests.

### Task 3: M52 Optimization Task Decomposition Quality Gate

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing tests**
  Add validation tests rejecting optimization taskpacks whose code-facing task lacks `baseline_or_current_behavior`, `optimization_candidate_matrix`, or `metric_delta_or_no_safe_change_evidence`; add a test rejecting doc-only optimization unless it contains an explicit no-safe-code-change rationale deliverable.

- [x] **Step 2: Run focused test to verify failure**
  Run:
  `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_validate_taskpack_rejects_optimization_without_candidate_matrix`

- [x] **Step 3: Implement stricter canonicalization and validation**
  Preserve current defaults, but add specific validation messages and author prompt guidance that optimization goals must produce baseline/profile evidence and candidate matrices before narrow implementation.

- [x] **Step 4: Run taskpack tests**
  Run taskpack-focused validation tests.

### Task 4: M53 Follow-Up Recommendation Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing tests**
  Add a completion report test asserting idle completed runs expose `follow_up_recommendation` with an action such as `integrate`, `next`, or `review_blocker`, and a ready-to-copy `agentteam next --from-taskpack ... --goal ...` command when next steps exist.

- [x] **Step 2: Run focused test to verify failure**
  Run:
  `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_completion_summary_includes_follow_up_recommendation`

- [x] **Step 3: Implement recommendation builder**
  Derive recommendations only from run status, blocked count, integration baseline, merge recommendation, and next steps. Do not auto-start follow-up work.

- [x] **Step 4: Run report CLI tests**
  Run focused report and next-command tests.

### Task 5: M54 Permission Block Operator Hints

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [x] **Step 1: Write failing tests**
  Add tests for status/explain-status output when permission requests are waiting. Assert the output includes request id, capability, reason, and exact approve/deny commands. Add a notification payload test for permission requests.

- [x] **Step 2: Run focused test to verify failure**
  Run:
  `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_status_includes_permission_request_hints`

- [x] **Step 3: Implement hints**
  Reuse existing permission request snapshot fields and notification text. Do not add new approval semantics.

- [x] **Step 4: Run related permission tests**
  Run focused permission tests plus the two-module suite before commit.

### Final Verification

- [x] Run `git diff --check`.
- [x] Run `env PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime`.
- [x] Update `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`.
- [x] Update `docs/agentteam-command-reference.md`.
- [x] Commit and push the implementation branch.

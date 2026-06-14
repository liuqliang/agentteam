# M61 Operator Report Aggregation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make completed AgentTeam runs produce concise Chinese operator reports and Feishu messages that summarize every completed task, not only the first task.

**Architecture:** Keep the scheduler and worker authority model unchanged. Extend the existing completion summary/report/notification boundary so structured task reports are aggregated into bounded operator-facing lines, then render the same aggregate through `agentteam report`, concise terminal output, and Feishu `run_completed` notifications.

**Tech Stack:** Python standard library, `unittest`, AgentTeam native runtime modules under `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime`.

---

### Task 1: Multi-Task Completion Summary Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py`

- [x] **Step 1: Write the failing aggregate-summary test**

Add a test that builds a completion summary from two completed task reports and asserts:

```python
summary["operator_digest"]
```

contains one bounded Chinese line for both task changes, both changed files, both verification entries, both measured results, and both next steps.

- [x] **Step 2: Run the focused test and verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_completion_summary_aggregates_multiple_tasks_in_operator_digest
```

Expected: fail because the digest currently uses only the first text item for each field.

- [x] **Step 3: Implement bounded aggregate digest lines**

Add a helper in `completion_summary.py` that joins up to three unique items per field and reports omitted counts as `等 N 项`. Use it only for operator digest text so existing structured lists remain unchanged.

- [x] **Step 4: Run the focused test and verify it passes**

Run the same focused command. Expected: pass.

### Task 2: Concise Terminal Report Aggregation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`

- [x] **Step 1: Write the failing concise-report test**

Add a test for `concise_report_lines(...)` with two task reports and a multi-item `completion_summary`. Assert the output includes bounded aggregate lines such as:

```text
changed: TASK-A=...; TASK-B=...
changed_files: src/a.py; src/b.py
verification: unit-a: passed; unit-b: passed
next: 继续验证 A。; 继续验证 B。
```

- [x] **Step 2: Run the focused test and verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_concise_report_lines_aggregate_multiple_tasks
```

Expected: fail because concise output currently uses the first summary item and first task item.

- [x] **Step 3: Implement bounded concise aggregation**

Update `concise_report_lines(...)` to render aggregate summary fields before per-task snippets. Preserve existing review gate, pursue recap, and token lines.

- [x] **Step 4: Run the focused test and verify it passes**

Run the same focused command. Expected: pass.

### Task 3: Feishu Run-Completed Message Quality

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py`

- [x] **Step 1: Write the failing Feishu aggregation test**

Add a `run_completed` notification test with two task reports. Assert the outbound text includes the Chinese aggregate digest and both task identifiers while staying bounded.

- [x] **Step 2: Run the focused test and verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_feishu_run_completed_summarizes_multiple_tasks
```

Expected: fail if Feishu text does not include the aggregate digest for all completed tasks.

- [x] **Step 3: Reuse completion summary aggregate digest in Feishu**

Update `_operator_report_text(...)` to place the Chinese digest near the top and keep per-task detail bounded to the first few task reports.

- [x] **Step 4: Run the focused test and verify it passes**

Run the same focused command. Expected: pass.

### Task 4: Documentation And Roadmap

**Files:**
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Document the M61 report behavior**

State that `agentteam report`, concise completion output, and Feishu `run_completed` notifications aggregate all structured task reports into bounded Chinese operator-facing summaries.

- [x] **Step 2: Mark M61 implemented and set the next recommended step**

Add M61 after M60 and set the next route toward follow-up queue / long-goal continuation ergonomics.

### Task 5: Full Verification

**Files:**
- No source edits.

- [x] **Step 1: Run diff whitespace check**

```bash
git diff --check
```

- [x] **Step 2: Run native runtime unit tests**

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
```

- [x] **Step 3: Commit and push**

```bash
git add docs/agentteam-command-reference.md \
  experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-14-m61-operator-report-aggregation.md \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/completion_summary.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/notifications.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py
git commit -m "Improve multi-task operator reports"
git push
```

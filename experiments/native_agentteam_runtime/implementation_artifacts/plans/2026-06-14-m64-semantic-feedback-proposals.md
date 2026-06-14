# M64 Semantic Feedback Proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Let implementation evidence create bounded semantic feedback proposal artifacts without letting ordinary workers mutate authority design documents.

**Architecture:** Add a `semantic_feedback.py` helper that writes immutable proposal JSON files under `<work_root>/semantic_feedback/`. Add `agentteam feedback propose` to create a proposal from a source run/report and `agentteam feedback list` to inspect pending proposals. Proposals are review artifacts only; they do not edit roadmap, design, source, or taskpack files.

**Tech Stack:** Python standard library, AgentTeam CLI, `unittest`.

---

### Task 1: Proposal Helper Contract

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/semantic_feedback.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing helper test**

Add a test that imports `write_semantic_feedback_proposal`, writes a proposal with source report metadata and target artifacts, then asserts the JSON contains `proposal_status: pending_review`, an explicit authority boundary, source report path, target artifacts, summary, and rationale.

- [x] **Step 2: Run focused test and verify failure**

Expected: import failure because `semantic_feedback.py` does not exist.

- [x] **Step 3: Implement helper**

Implement bounded proposal writing plus `list_semantic_feedback_proposals(work_root)`.

- [x] **Step 4: Run focused test and verify pass**

Run the focused helper test. Expected: pass.

### Task 2: CLI Feedback Commands

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing CLI tests**

Add tests for:

- `agentteam feedback propose --taskpack first-pass --proposal-id design-gap-1 --target-artifact design/system.md --summary ... --rationale ... --json`
- `agentteam feedback list --json`

Assert proposal files are written under `<work_root>/semantic_feedback/`, list output shows pending proposals, and source reports remain unchanged.

- [x] **Step 2: Run focused tests and verify failure**

Expected: parser reports unknown command `feedback`.

- [x] **Step 3: Add parser and handlers**

Add `_add_feedback_parser`, `_handle_feedback`, `_write_feedback_text`, and source run resolution using existing run selection helpers.

- [x] **Step 4: Run focused tests and verify pass**

Run focused feedback tests. Expected: pass.

### Task 3: Documentation And Roadmap

**Files:**
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Document semantic feedback proposals**

Explain that proposals are review artifacts and do not mutate design authority documents.

- [x] **Step 2: Mark M64 implemented**

Add M64 after M63 and set the next route toward model adapter or language-aware repo grounding.

### Task 4: Full Verification

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
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-14-m64-semantic-feedback-proposals.md \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/semantic_feedback.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "Add semantic feedback proposals"
git push
```

# M63 Guarded Artifact Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Allow operators to delete only validated rebuildable artifacts while preserving all authoritative AgentTeam run evidence.

**Architecture:** Extend `agentteam gc --artifacts` with an explicit `--delete-artifacts` flag that requires `--force` and a fresh projection DB. Reuse the existing retention plan validation so deletion is limited to listed rebuildable candidates whose size and sha256 still match the projection. After deletion, report that `agentteam db rebuild` is the next action.

**Tech Stack:** Python standard library, AgentTeam CLI, projection DB retention metadata, `unittest`.

---

### Task 1: Guarded CLI Deletion Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`

- [x] **Step 1: Write failing deletion test**

Add a test that creates authoritative run artifacts plus rebuildable `repo_contexts` and `role_contexts`, rebuilds the projection DB, then runs:

```bash
agentteam gc --project-root <repo> --artifacts --delete-artifacts --force --json
```

Assert rebuildable context files are deleted while `events.jsonl`, `reports/final_report.json`, and frozen taskpack files still exist.

- [x] **Step 2: Run focused test and verify failure**

Expected: parser rejects unknown `--delete-artifacts`.

- [x] **Step 3: Implement guarded deletion**

Add `--delete-artifacts`, require `--artifacts --force`, block deletion unless the retention plan is ready and `validation_status == "passed"`, delete only listed candidates whose `retention_policy == "rebuildable"` and validation status is `passed`.

- [x] **Step 4: Run focused test and verify pass**

Run the focused test. Expected: pass.

### Task 2: Safety Failure Contract

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`

- [x] **Step 1: Write failing stale-candidate test**

After projection rebuild, mutate a rebuildable candidate file and run deletion with `--delete-artifacts --force`. Assert the command fails and the mutated file remains.

- [x] **Step 2: Run focused test and verify failure**

Expected: parser rejects unknown flag before implementation.

- [x] **Step 3: Implement validation failure path**

Raise `AgentTeamCliError` with `artifact_deletion_status: blocked` when candidate validation fails.

- [x] **Step 4: Run focused test and verify pass**

Run the focused test. Expected: pass.

### Task 3: Documentation And Roadmap

**Files:**
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Document guarded artifact cleanup**

Explain `--delete-artifacts`, its required flags, fresh projection dependency, validation guard, and protected authoritative artifact classes.

- [x] **Step 2: Mark M63 implemented**

Add M63 after M62 and set the next route toward semantic feedback proposal artifacts.

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
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-14-m63-guarded-artifact-cleanup.md \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "Add guarded artifact cleanup"
git push
```

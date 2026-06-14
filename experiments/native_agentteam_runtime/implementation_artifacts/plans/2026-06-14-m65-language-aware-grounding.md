# M65 Language-Aware Repository Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only repository grounding command that identifies the target project's primary languages, build/test entrypoints, and candidate verification commands before taskpack authoring or follow-up planning.

**Architecture:** Introduce `repo_grounding.py` as a thin repository-level scanner. It should not run build tools, install dependencies, mutate the target repository, or replace per-attempt `repo_context.v1`. It provides a compact `repo_grounding.v1` summary that later milestones can feed into taskpack author prompts.

**Tech Stack:** Python standard library, AgentTeam CLI, `unittest`.

---

### Task 1: Grounding Helper Contract

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/repo_grounding.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`

- [x] **Step 1: Write failing helper test**

Add a test that builds a small mixed-language repository and asserts the grounding summary includes language counts, detected project tools, test entrypoints, and suggested verification commands.

- [x] **Step 2: Run focused test and verify failure**

Expected: import failure because the helper does not exist yet.

- [x] **Step 3: Implement helper**

Implement bounded tracked-file scanning plus static detection for Python, Node, Make, CMake, Cargo, Go, Java/Maven/Gradle, and common test file patterns.

- [x] **Step 4: Run focused test and verify pass**

Run the focused helper test. Expected: pass.

### Task 2: CLI Grounding Command

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [x] **Step 1: Write failing CLI test**

Add `agentteam grounding --project-root <repo> --json` coverage that checks compact JSON output without launching a worker.

- [x] **Step 2: Run focused CLI test and verify failure**

Expected: parser reports unknown command `grounding`.

- [x] **Step 3: Add parser and handler**

Add `grounding` to CLI help, render concise text by default, and return structured JSON when requested.

- [x] **Step 4: Run focused CLI test and verify pass**

Run the focused CLI test. Expected: pass.

### Task 3: Documentation And Roadmap

**Files:**
- Modify: `docs/agentteam-command-reference.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Document `agentteam grounding`**

Explain that the command is read-only and reports candidate commands without executing them.

- [x] **Step 2: Mark M65 implemented**

Add M65 to the roadmap and leave M66 as the next non-M68 implementation route.

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
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-14-m65-language-aware-grounding.md \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/repo_grounding.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "Add language-aware repo grounding"
git push
```

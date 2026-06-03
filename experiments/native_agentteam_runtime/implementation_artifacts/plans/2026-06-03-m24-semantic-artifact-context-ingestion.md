# M24 Semantic Artifact Context Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded selected-artifact summaries to planner context packages.

**Architecture:** Extend `planner_context.py` with deterministic artifact summary helpers, pass selected artifact paths through `TwoPhaseFileScheduler`, and expose repeatable CLI flags for context artifact selection. Keep the feature explicit and bounded; no automatic repository scanning.

**Tech Stack:** Python 3.12 standard library, existing `unittest` suite, file-backed two-phase scheduler.

---

### Task 1: Planner Context Artifact Summaries

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/planner_context.py`

- [x] **Step 1: Write failing artifact summary tests**

Add tests that call `build_planner_context(...)` with a long markdown artifact
and a missing path. Assert that:

- `artifact_context.schema_version == "artifact_context.v1"`;
- the source includes path, `sha256`, `size_bytes`, `modified_at`, headings, and
  a bounded excerpt;
- a unique tail marker past the excerpt budget is not embedded;
- the missing path appears in `artifact_context.warnings`.

- [x] **Step 2: Verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_build_planner_context_includes_bounded_artifact_summaries \
  experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_build_planner_context_warns_for_missing_artifact \
  -v
```

Expected red: `artifact_context` is not present.

- [x] **Step 3: Implement deterministic artifact context**

Add optional parameters to `build_planner_context(...)`:

```python
context_artifact_paths=None
context_artifact_excerpt_chars=1200
```

Add helpers that read only explicit paths, compute digest and metadata, extract
markdown headings, and bound excerpts by `context_artifact_excerpt_chars`.

- [x] **Step 4: Verify green**

Run the same focused test command. Expected: both tests pass.

### Task 2: Scheduler Context File Integration

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`

- [x] **Step 1: Write failing scheduler context test**

Add a test that constructs `TwoPhaseFileScheduler(..., auto_decompose=True,
decomposition_context_artifact_paths=[artifact_path],
decomposition_context_excerpt_chars=80)`, dispatches the planner task, reads
`planner_context_path`, and asserts that `artifact_context.sources[0].path`
matches the artifact.

- [x] **Step 2: Verify red**

Run the focused scheduler test. Expected red:
`TwoPhaseFileScheduler.__init__` does not accept the new parameters.

- [x] **Step 3: Pass artifact settings into `build_planner_context(...)`**

Store `decomposition_context_artifact_paths` and
`decomposition_context_excerpt_chars` on the scheduler and pass them in
`_write_planner_context(...)`.

- [x] **Step 4: Verify green**

Run the focused scheduler test. Expected: pass.

### Task 3: CLI Flags And Runtime Docs

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/cli.py`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [x] **Step 1: Write failing CLI test**

Add a CLI test that runs the two-phase worker-pool fake planner path with:

```text
--planner-context-artifact <artifact.md>
--planner-context-excerpt-chars 80
```

Then read `<output-dir>/planner_contexts/DECOMPOSE-M24-001.json` and assert
that the artifact summary is present and bounded.

- [x] **Step 2: Verify red**

Run the focused CLI test. Expected red: CLI does not recognize the new flags.

- [x] **Step 3: Implement CLI flags**

Add repeatable `--planner-context-artifact` and integer
`--planner-context-excerpt-chars` arguments. Validate the excerpt limit is at
least 1, and pass both values to `TwoPhaseFileScheduler`.

- [x] **Step 4: Update docs and roadmap**

Add M24 notes to `m0_file_runtime.md`; mark M24 implemented in
`native_runtime_roadmap.md` and set the next recommended milestone to M25.

- [x] **Step 5: Verify focused green**

Run the focused CLI test. Expected: pass.

### Task 4: Full Verification And Commit

**Files:**
- Modify: runtime, test, and artifact files from Tasks 1 to 3.

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
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-03-m24-semantic-artifact-context-ingestion.md \
  experiments/native_agentteam_runtime/implementation_artifacts/plans/2026-06-03-m24-semantic-artifact-context-ingestion.md \
  experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md \
  experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md
```

- [x] **Step 4: Commit and push**

```bash
git add experiments/native_agentteam_runtime
git commit -m "Add M24 semantic artifact context ingestion"
git push origin native-runtime-m0
```

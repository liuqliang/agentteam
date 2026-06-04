# M32 Repository Map Context Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate bounded repository-map context files and attach their paths to implementation dispatch payloads.

**Architecture:** Add a focused `repo_map.py` module for inventory, Python symbol summaries, context selection, and cache metadata. Keep scheduler authority in `m0_runtime.py` and `two_phase_scheduler.py`; they only call the repo-map API during dispatch and pass `repo_context_path` to workers. Codex prompts point to context files instead of inlining repository content.

**Tech Stack:** Python standard library only: `ast`, `hashlib`, `json`, `subprocess`, `pathlib`; existing `unittest` test harness.

---

### Task 1: M32a Repository Inventory

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/repo_map.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] **Step 1: Write failing inventory test**

Add `test_repo_map_inventory_records_tracked_files_and_warnings`:

```python
with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "repo"
    output_dir = Path(tmp) / "run"
    _init_git_repo(repo)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "module.py").write_text("import os\n\ndef run():\n    return os.name\n", encoding="utf-8")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "pkg/module.py", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "add files"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    result = build_repository_map(repo, output_dir, max_file_bytes=1024)

    self.assertEqual(result["manifest"]["repo_map_schema_version"], "repo_map.v1")
    self.assertTrue((output_dir / "state" / "repo_map" / "inventory.json").exists())
    self.assertIn("pkg/module.py", {item["path"] for item in result["inventory"]["files"]})
```

- [ ] **Step 2: Run red test**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime.M0RuntimeTests.test_repo_map_inventory_records_tracked_files_and_warnings -v
```

Expected: import or name failure for `build_repository_map`.

- [ ] **Step 3: Implement inventory**

Create `repo_map.py` with `build_repository_map(project_root, output_dir, max_file_bytes=65536)`. It writes:

```text
state/repo_map/manifest.json
state/repo_map/inventory.json
```

Each inventory file entry includes `path`, `size_bytes`, `language`, `category`, and optional `sha256` when size is within limit.

- [ ] **Step 4: Export API**

Expose `build_repository_map` from `agentteam_runtime.__init__`.

- [ ] **Step 5: Verify**

Run the red test again. Expected: PASS.

### Task 2: M32b Python Structure Summary

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/repo_map.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] **Step 1: Write failing Python symbols test**

Add `test_repo_map_python_symbols_extract_imports_classes_and_functions`:

```python
source = "import os\nfrom pathlib import Path\n\nclass Runner:\n    def run(self):\n        return Path(os.getcwd())\n\ndef helper():\n    return Runner()\n"
```

Assert `symbols.json` has `imports == ["os", "pathlib.Path"]`, class `Runner`, function `helper`, and method `Runner.run`.

- [ ] **Step 2: Run red test**

Expected: symbols file missing or symbol fields absent.

- [ ] **Step 3: Implement AST extraction**

Use `ast.parse` for `.py` files. Record per-file:

```json
{
  "path": "pkg/module.py",
  "language": "python",
  "imports": ["os", "pathlib.Path"],
  "classes": [{"name": "Runner", "methods": ["run"]}],
  "functions": ["helper"],
  "parse_warnings": []
}
```

Parse failures should produce `parse_warnings` and continue.

- [ ] **Step 4: Verify**

Run focused symbols tests. Expected: PASS.

### Task 3: M32c Task Context Selection

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/repo_map.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] **Step 1: Write failing context-selection test**

Add `test_repo_context_selects_files_inside_task_scopes`:

```python
task = _backlog_task("TASK-001", write_scope=["pkg/"])
context = build_repo_context(repo, output_dir, task, agent_role="repo_map_agent", max_files=3)
```

Assert `repo_context_schema_version == "repo_context.v1"`, selected files include `pkg/module.py`, docs outside scope are either omitted or lower-ranked, and `omitted_file_count` is present.

- [ ] **Step 2: Run red test**

Expected: `build_repo_context` missing.

- [ ] **Step 3: Implement selector**

Implement `build_repo_context(project_root, output_dir, task, agent_role, max_files=8)`. It loads or builds repo map, ranks files by scope and objective token matches, writes:

```text
repo_contexts/<task-id>-<agent-role>.json
```

Return the context dict with `repo_context_path`.

- [ ] **Step 4: Verify**

Run focused context-selection test. Expected: PASS.

### Task 4: M32d Scheduler And Codex Wiring

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/m0_runtime.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] **Step 1: Write failing dispatch tests**

Add tests:

- `test_run_simulation_dispatch_includes_repo_context_path_when_project_root_is_available`
- `test_two_phase_scheduler_dispatch_includes_repo_context_path_when_project_root_is_available`
- `test_codex_runtime_adapter_includes_repo_context_path`

Assert mailbox payload includes `repo_context_path` and Codex prompt includes `Repo context package:`.

- [ ] **Step 2: Run red tests**

Expected: missing payload key and prompt section.

- [ ] **Step 3: Wire dispatch**

In `run_simulation` and `TwoPhaseFileScheduler._dispatch_task`, when `project_root` is available, call `build_repo_context(...)` and merge:

```python
{
    "repo_context_path": context["repo_context_path"],
    "repo_context_schema_version": "repo_context.v1",
}
```

into the mailbox payload.

- [ ] **Step 4: Wire Codex prompt**

Add a prompt section:

```text
Repo context package:
<repo_context_path>
Read repo_context_path before selecting implementation files.
```

- [ ] **Step 5: Verify**

Run focused dispatch/prompt tests. Expected: PASS.

### Task 5: M32e Cache Reuse And Dirty Metadata

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/repo_map.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_m0_runtime.py`

- [ ] **Step 1: Write failing cache metadata test**

Add `test_repo_map_manifest_reuses_clean_commit_cache_and_marks_dirty_repo`.

Build the repo map twice on a clean repo and assert `cache_status == "reused"` on the second build. Modify a tracked file without committing and assert `working_tree_state == "dirty_or_unversioned"`.

- [ ] **Step 2: Run red test**

Expected: missing `cache_status` or dirty state metadata.

- [ ] **Step 3: Implement cache metadata**

Record in `manifest.json`:

```json
{
  "git_commit": "<sha or null>",
  "working_tree_state": "clean|dirty_or_unversioned",
  "cache_status": "rebuilt|reused",
  "inventory_options": {"max_file_bytes": 65536},
  "symbol_extraction_version": "python_ast_v1"
}
```

Reuse existing inventory/symbols when commit and options match and the worktree is clean.

- [ ] **Step 4: Verify**

Run focused cache test. Expected: PASS.

### Task 6: Documentation And Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/m0_file_runtime.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`
- Add: `experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-04-m32-implementation-notes.md`

- [ ] **Step 1: Update docs**

Document M32a-M32e behavior, repo map files, context payload keys, cache status, and non-goals.

- [ ] **Step 2: Run verification**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -v
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m agentteam_runtime.artifact_lint --root experiments/native_agentteam_runtime
python3 -m compileall experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime experiments/native_agentteam_runtime/m0_runtime/tests
git diff --check
rg -n "TB[D]|TO[D]O|implement late[r]|fill in detail[s]|Similar t[o]|appropriate place[h]older" <changed-files>
```

Expected: all commands pass; placeholder scan exits 1 with no matches.

- [ ] **Step 3: Commit and push**

Commit message:

```text
Add M32 repository map context generation
```

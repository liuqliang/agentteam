# M49 AgentTeam-Target Policy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement M49 policy hardening so AgentTeam-as-target functional or semantic requirements stay ordinary taskpack work while adding no-merge/no-push and operator review constraints.

**Architecture:** Keep the existing CLI and scheduler unchanged. Add deterministic helper logic in taskpack authoring to detect AgentTeam target repositories, classify broad vs goal-directed requirements, and inject taskpack/report policy text without granting merge or push authority.

**Tech Stack:** Python standard library, existing AgentTeam taskpack authoring, unittest-based native runtime tests.

---

### Task 1: AgentTeam Target Detection And Author Prompt

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write the failing prompt test**

Add a test near existing taskpack author tests:

```python
def test_codex_taskpack_author_prompt_includes_agentteam_target_policy(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "agentteam"
        taskpack_dir = tmp_path / "drafts" / "m49-agentteam-target"
        author_context_dir = tmp_path / "drafts" / ".m49-agentteam-target-author"
        _init_repo(repo)
        runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
        runtime_pkg.mkdir(parents=True)
        (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")

        prompt = _build_codex_taskpack_prompt(
            project_root=repo,
            goal="Implement M49 policy hardening for AgentTeam-as-target tasks.",
            taskpack_id="m49-agentteam-target",
            taskpack_dir=taskpack_dir,
            author_context_dir=author_context_dir,
            repo_map={"paths": {"manifest_path": "manifest.json", "inventory_path": "inventory.json", "symbols_path": "symbols.json"}},
            verification_profile=None,
        )

        self.assertIn("AgentTeam-as-target", prompt)
        self.assertIn("functional or semantic requirement", prompt)
        self.assertIn("do not request git merge or git push", prompt)
        self.assertIn("open-ended improvement requests", prompt)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_codex_taskpack_author_prompt_includes_agentteam_target_policy
```

Expected: FAIL because `_build_codex_taskpack_prompt` does not include AgentTeam-as-target policy text.

- [ ] **Step 3: Implement the minimal prompt helper**

Add `_is_agentteam_target_project(project_root)` and `_agentteam_target_policy_prompt()` in `taskpack_author.py`. Append the policy block from `_build_codex_taskpack_prompt()` only when the target repo contains `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime`.

- [ ] **Step 4: Run focused test**

Run the same focused unittest. Expected: OK.

### Task 2: Canonicalize AgentTeam-Target Taskpacks

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing canonicalization test**

Add a test near existing `_canonicalize_codex_taskpack_files` tests:

```python
def test_canonicalize_codex_taskpack_preserves_agentteam_target_operator_gate(self):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "agentteam"
        drafts = tmp_path / "drafts"
        _init_repo(repo)
        runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
        runtime_pkg.mkdir(parents=True)
        (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")
        result = draft_taskpack_files(
            project_root=repo,
            goal="Implement M49 policy hardening for AgentTeam-as-target tasks.",
            draft_root=drafts,
            taskpack_id="m49-agentteam-target",
            read_scope=["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/"],
            write_scope=["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/"],
        )
        taskpack_dir = Path(result["taskpack_dir"])

        _canonicalize_codex_taskpack_files(taskpack_dir)

        loaded = load_taskpack(taskpack_dir)
        taskpack = loaded["taskpack"]
        task = loaded["backlog"]["items"][0]
        self.assertFalse(taskpack["policy"]["allow_merge"])
        self.assertTrue(taskpack["policy"]["operator_review_required"])
        self.assertIn("source_control_restrictions", taskpack["policy"])
        self.assertIn("AgentTeam-as-target", task["goal_alignment"])
        self.assertIn("agentteam_target_review_gate", task["required_deliverables"])
        self.assertIn("do not merge", task["objective"])
        self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_canonicalize_codex_taskpack_preserves_agentteam_target_operator_gate
```

Expected: FAIL because canonicalization does not yet add AgentTeam-target policy metadata or deliverables.

- [ ] **Step 3: Implement minimal canonicalization**

When `_canonicalize_codex_taskpack_files()` loads an AgentTeam target taskpack, set `policy.allow_merge = False`, set `policy.operator_review_required = True`, add `source_control_restrictions = ["no_merge", "no_push", "no_release_activation"]`, append AgentTeam-target review text to each task objective and goal_alignment if missing, and add `agentteam_target_review_gate` to required_deliverables.

- [ ] **Step 4: Run focused test**

Run the same focused unittest. Expected: OK.

### Task 3: Report And Documentation Visibility

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py`
- Modify: `docs/agentteam-command-reference.md`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing report test**

Add a test that builds an operator report containing `agentteam_target_review_gate` in a task report and asserts `concise_report_lines()` includes an operator review requirement.

- [ ] **Step 2: Implement report wording**

Render a concise line when a task report indicates AgentTeam-target review. Keep it advisory: it must not approve, merge, or change status.

- [ ] **Step 3: Document ordinary command usage**

Update `docs/agentteam-command-reference.md` to state that AgentTeam-as-target work uses ordinary `start`/`next` commands and does not use a dedicated self-improvement command.

- [ ] **Step 4: Run focused tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_agentteam_cli_report_renders_operator_summary_and_writes_report_file
```

Expected: OK.

### Task 4: Full Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/native_runtime_roadmap.md`

- [ ] **Step 1: Mark M49 implemented**

Update the roadmap M49 status and implemented bullets after code and tests pass.

- [ ] **Step 2: Run full native runtime tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime
```

Expected: all tests pass.

- [ ] **Step 3: Commit and push**

Commit with:

```bash
git commit -m "feat: harden AgentTeam-target task policy"
```

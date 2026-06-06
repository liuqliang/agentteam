# Local AgentTeam Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AgentTeam usable as a short local command that reads project-local `.agentteam/profile.json` configuration.

**Architecture:** Keep the existing module CLI as the execution engine. Add a thin repository launcher, a project profile module, and `init`/`start` CLI commands that translate profile fields into the existing `submit` flow.

**Tech Stack:** Python stdlib `argparse`, `json`, `pathlib`, shell launcher scripts, existing `unittest` suite.

---

### Task 1: Project Profile Contract

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/profile.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing tests**

Add tests that call `python3 -m agentteam_runtime.agentteam init` with a fixture repository and assert that `.agentteam/profile.json` is created with `profile_schema_version`, `project_key`, `work_root`, `author_runtime`, `default_runtime`, `notification_project`, and `feishu` fields. Assert webhook values and signing secret values are not written to the file.

- [ ] **Step 2: Run tests and verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_agentteam_cli_init_writes_project_profile_without_secrets
```

Expected: failure because the `init` subcommand does not exist.

- [ ] **Step 3: Implement profile helpers**

Create `profile.py` with `PROFILE_SCHEMA_VERSION`, `profile_path_for_project`, `default_project_key`, `default_work_root`, `build_project_profile`, `write_project_profile`, and `load_project_profile`.

- [ ] **Step 4: Run focused tests and verify green**

Run the same focused test. Expected: pass.

### Task 2: Short Start Command

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing tests**

Add a test that creates a fake-runtime profile, runs `agentteam start --project-root <repo> --goal <goal> --taskpack-id <id> --one-shot`, and asserts that the existing submit flow completes using the profile work root.

- [ ] **Step 2: Run tests and verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_agentteam_cli_start_uses_project_profile_to_submit_fake_taskpack
```

Expected: failure because the `start` subcommand does not exist.

- [ ] **Step 3: Implement CLI commands**

Add `init` and `start` parsers. `init` writes the profile. `start` loads or interactively creates the profile, prompts for a goal when missing, and calls the existing submit path through an internal namespace.

- [ ] **Step 4: Run focused tests and verify green**

Run the focused `start` test. Expected: pass.

### Task 3: Local Command Entry Point

**Files:**
- Create: `agentteam`
- Create: `scripts/install-local.sh`
- Modify: `experiments/native_agentteam_runtime/README.md`
- Test: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Write failing launcher test**

Add a test that runs the repository-root `agentteam --help` and asserts it exits successfully and prints the operator CLI help.

- [ ] **Step 2: Run tests and verify red**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_repo_root_agentteam_launcher_invokes_cli_help
```

Expected: failure because the launcher does not exist.

- [ ] **Step 3: Implement launcher and installer**

Add a Python launcher that prepends `experiments/native_agentteam_runtime/m0_runtime` to `sys.path` and calls `agentteam_runtime.agentteam.main`. Add a shell installer that symlinks the launcher to `$HOME/.local/bin/agentteam`.

- [ ] **Step 4: Run verification**

Run focused tests, full `test_taskpack`, full runtime tests, `compileall`, and `git diff --check`.

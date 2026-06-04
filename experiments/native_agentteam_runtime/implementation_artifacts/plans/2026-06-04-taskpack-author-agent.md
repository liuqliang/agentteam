# Taskpack Author Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a taskpack authoring layer so a user can submit `project_root` and `goal`, get a validated frozen taskpack, and launch it through the existing AgentTeam runtime without hand-writing JSON artifacts.

**Architecture:** Add a focused `taskpack` module for deterministic artifact loading, validation, freezing, and runtime argument translation. Add a separate `taskpack_author` module for draft generation, with a fake author for tests and a Codex author path for live use. Add a human-facing module CLI, `python -m agentteam_runtime.agentteam`, that wraps the existing scheduler CLI instead of replacing it.

**Tech Stack:** Python standard library only (`argparse`, `json`, `hashlib`, `shutil`, `subprocess`, `pathlib`, `tempfile`, `unittest`), existing AgentTeam runtime modules, existing Codex CLI integration.

---

## File Structure

- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
  - Owns taskpack file paths, JSON-compatible `taskpack.yaml` loading, validation, freezing, manifest digesting, and conversion to existing runtime CLI arguments.
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
  - Owns fake and Codex-backed draft generation. It writes drafts only to a draft directory and never edits the target repository.
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
  - Provides human-facing subcommands: `taskpack draft`, `taskpack validate`, `taskpack freeze`, and `run`.
- Create: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
  - Focused deterministic tests for draft generation, validation, freezing, and CLI argument translation.
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
  - Export the stable taskpack helpers used by tests and future modules.
- Modify: `experiments/native_agentteam_runtime/README.md`
  - Document the operator flow and explain that shell scripts are only development helpers.

The first version keeps `taskpack.yaml` as JSON-compatible YAML. JSON is valid YAML 1.2, so this preserves the filename and future compatibility without adding a PyYAML dependency to the M0 experiment.

## Task 1: Taskpack Draft Files And Loader

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Create: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`

- [ ] **Step 1: Write the failing draft test**

Add this test file:

```python
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime import (
    TaskpackValidationError,
    draft_taskpack_files,
    load_taskpack,
)


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class TaskpackTests(unittest.TestCase):
    def test_draft_taskpack_files_writes_expected_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_files(
                project_root=repo,
                goal="Improve fixture behavior without broad writes.",
                draft_root=drafts,
                taskpack_id="fixture-taskpack",
                write_scope=["src/"],
                verification_command=["python3", "-m", "unittest", "discover"],
            )

            taskpack_dir = Path(result["taskpack_dir"])
            self.assertEqual(taskpack_dir.name, "fixture-taskpack")
            self.assertTrue((taskpack_dir / "taskpack.yaml").exists())
            self.assertTrue((taskpack_dir / "agent_pool.json").exists())
            self.assertTrue((taskpack_dir / "backlog.json").exists())
            self.assertTrue((taskpack_dir / "verification.json").exists())
            self.assertTrue((taskpack_dir / "README.md").exists())

            loaded = load_taskpack(taskpack_dir)
            self.assertEqual(loaded["taskpack"]["taskpack_schema_version"], "taskpack.v1")
            self.assertEqual(loaded["taskpack"]["taskpack_id"], "fixture-taskpack")
            self.assertEqual(loaded["taskpack"]["status"], "draft")
            self.assertEqual(loaded["taskpack"]["project_root"], str(repo.resolve()))
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])
            self.assertEqual(loaded["backlog"]["items"][0]["write_scope"], ["src/"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_draft_taskpack_files_writes_expected_artifacts -v
```

Expected: FAIL with an import error for `draft_taskpack_files` or `agentteam_runtime.taskpack`.

- [ ] **Step 3: Implement draft file creation and loading**

Create `taskpack.py` with:

```python
import json
import re
from pathlib import Path


TASKPACK_SCHEMA_VERSION = "taskpack.v1"
DEFAULT_WORKER_ROLE = "implementation_worker"


class TaskpackValidationError(ValueError):
    pass


def draft_taskpack_files(
    project_root,
    goal,
    draft_root,
    taskpack_id=None,
    read_scope=None,
    write_scope=None,
    verification_command=None,
    allow_merge=False,
    codex_timeout_seconds=1800,
):
    project_root = Path(project_root).resolve()
    draft_root = Path(draft_root).resolve()
    taskpack_id = taskpack_id or _slugify(goal)
    taskpack_dir = draft_root / taskpack_id
    taskpack_dir.mkdir(parents=True, exist_ok=False)

    read_scope = list(read_scope or ["."])
    write_scope = list(write_scope or [".agentteam/generated/"])
    verification_command = list(verification_command or ["python3", "-m", "unittest", "discover"])

    task_id = f"TASK-{taskpack_id.upper().replace('-', '_')}-001"
    taskpack = {
        "taskpack_schema_version": TASKPACK_SCHEMA_VERSION,
        "taskpack_id": taskpack_id,
        "status": "draft",
        "project_root": str(project_root),
        "goal": goal,
        "runtime": {
            "default_backend": "codex",
            "codex": {
                "sandbox": "workspace-write",
                "timeout_seconds": codex_timeout_seconds,
            },
        },
        "policy": {
            "allow_merge": bool(allow_merge),
            "merge_requires_verified_integration": True,
        },
        "files": {
            "agent_pool": "agent_pool.json",
            "backlog": "backlog.json",
            "verification": "verification.json",
        },
    }
    agent_pool = {
        "scheduler_agent_id": "agent-scheduler",
        "role_runtime_profiles": {
            DEFAULT_WORKER_ROLE: {
                "adapter": "codex",
                "sandbox": "workspace-write",
                "timeout_seconds": codex_timeout_seconds,
            }
        },
        "agents": [
            {
                "agent_id": "agent-implementation-worker-1",
                "role": DEFAULT_WORKER_ROLE,
                "status": "idle",
                "inbox_path": "mailboxes/agent-implementation-worker-1/inbox.jsonl",
            }
        ],
    }
    backlog = {
        "backlog_id": f"BL-{taskpack_id}",
        "items": [
            {
                "task_id": task_id,
                "milestone_id": "TASKPACK-M0",
                "objective": goal,
                "backlog_status": "ready",
                "risk_target": "L1",
                "depends_on": [],
                "read_scope": read_scope,
                "write_scope": write_scope,
                "required_role": DEFAULT_WORKER_ROLE,
                "blockers": [],
            }
        ],
    }
    verification = {
        "verification_schema_version": "taskpack_verification.v1",
        "command": verification_command,
        "success_criteria": [
            "verification command exits with code 0",
            "runtime validation accepts changed files inside declared write_scope",
        ],
    }

    _write_json(taskpack_dir / "taskpack.yaml", taskpack)
    _write_json(taskpack_dir / "agent_pool.json", agent_pool)
    _write_json(taskpack_dir / "backlog.json", backlog)
    _write_json(taskpack_dir / "verification.json", verification)
    (taskpack_dir / "README.md").write_text(_render_readme(taskpack, backlog, verification), encoding="utf-8")
    return {"taskpack_dir": str(taskpack_dir), "taskpack_id": taskpack_id}


def load_taskpack(taskpack_dir):
    taskpack_dir = Path(taskpack_dir)
    taskpack = _read_json(taskpack_dir / "taskpack.yaml")
    files = taskpack.get("files", {})
    return {
        "taskpack_dir": str(taskpack_dir.resolve()),
        "taskpack": taskpack,
        "agent_pool": _read_json(taskpack_dir / files.get("agent_pool", "agent_pool.json")),
        "backlog": _read_json(taskpack_dir / files.get("backlog", "backlog.json")),
        "verification": _read_json(taskpack_dir / files.get("verification", "verification.json")),
    }


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "taskpack"


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _render_readme(taskpack, backlog, verification):
    task = backlog["items"][0]
    return "\n".join(
        [
            f"# {taskpack['taskpack_id']}",
            "",
            f"Goal: {taskpack['goal']}",
            "",
            f"Project root: `{taskpack['project_root']}`",
            "",
            f"Task: `{task['task_id']}`",
            "",
            f"Read scope: `{json.dumps(task['read_scope'], sort_keys=True)}`",
            "",
            f"Write scope: `{json.dumps(task['write_scope'], sort_keys=True)}`",
            "",
            f"Verification: `{json.dumps(verification['command'])}`",
            "",
        ]
    )
```

Modify `__init__.py` by adding exports:

```python
    "TaskpackValidationError": (".taskpack", "TaskpackValidationError"),
    "draft_taskpack_files": (".taskpack", "draft_taskpack_files"),
    "load_taskpack": (".taskpack", "load_taskpack"),
```

- [ ] **Step 4: Run the draft test to verify it passes**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_draft_taskpack_files_writes_expected_artifacts -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "feat: add taskpack draft artifacts"
```

## Task 2: Deterministic Validator And Freezer

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Add validator and freezer tests**

Append these methods to `TaskpackTests`:

```python
    def test_validate_taskpack_rejects_broad_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject broad writes.",
                draft_root=drafts,
                taskpack_id="bad-write-scope",
                write_scope=["."],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope must not include repository root", str(raised.exception))

    def test_validate_and_freeze_taskpack_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Freeze a safe taskpack.",
                draft_root=drafts,
                taskpack_id="safe-taskpack",
                write_scope=["src/"],
            )

            validation = validate_taskpack(result["taskpack_dir"])
            self.assertEqual(validation["status"], "accepted")

            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["taskpack_id"], "safe-taskpack")
            self.assertEqual(manifest["status"], "frozen")
            self.assertEqual(len(manifest["digest_sha256"]), 64)
            self.assertTrue((frozen_dir / "taskpack.yaml").exists())
```

Update the import list in `test_taskpack.py`:

```python
from agentteam_runtime import (
    TaskpackValidationError,
    draft_taskpack_files,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_validate_taskpack_rejects_broad_write_scope experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_validate_and_freeze_taskpack_writes_manifest -v
```

Expected: FAIL with missing `validate_taskpack` and `freeze_taskpack`.

- [ ] **Step 3: Implement validation and freezing**

Add to `taskpack.py`:

```python
import hashlib
import shutil
import subprocess
```

Add functions:

```python
def validate_taskpack(taskpack_dir):
    loaded = load_taskpack(taskpack_dir)
    errors = []
    taskpack = loaded["taskpack"]
    backlog = loaded["backlog"]
    verification = loaded["verification"]
    project_root = Path(taskpack.get("project_root", ""))

    if taskpack.get("taskpack_schema_version") != TASKPACK_SCHEMA_VERSION:
        errors.append("taskpack_schema_version must be taskpack.v1")
    if not project_root.exists():
        errors.append("project_root does not exist")
    elif not _is_git_repo(project_root):
        errors.append("project_root must be a git repository")
    if taskpack.get("status") not in {"draft", "frozen"}:
        errors.append("status must be draft or frozen")
    if not taskpack.get("goal"):
        errors.append("goal must be non-empty")

    items = backlog.get("items", [])
    if not items:
        errors.append("backlog must contain at least one task")
    seen_task_ids = set()
    for item in items:
        task_id = item.get("task_id")
        if not task_id:
            errors.append("task_id must be non-empty")
        if task_id in seen_task_ids:
            errors.append(f"duplicate task_id: {task_id}")
        seen_task_ids.add(task_id)
        write_scope = item.get("write_scope", [])
        if not isinstance(write_scope, list) or not write_scope:
            errors.append(f"{task_id} write_scope must be a non-empty list")
        for scope in write_scope:
            if scope in {".", "./", "*", "**", "/"}:
                errors.append("write_scope must not include repository root")
            if Path(scope).is_absolute():
                errors.append(f"{task_id} write_scope must be repository-relative: {scope}")
        for dependency in item.get("depends_on", []):
            if dependency == task_id:
                errors.append(f"{task_id} must not depend on itself")

    command = verification.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        errors.append("verification.command must be a non-empty string array")
    elif command[0] not in {"python3", "python", "/bin/bash", "bash", "make"}:
        errors.append(f"verification command is not allowed: {command[0]}")

    if errors:
        raise TaskpackValidationError("; ".join(errors))
    return {"status": "accepted", "taskpack_id": taskpack["taskpack_id"], "errors": []}


def freeze_taskpack(taskpack_dir, frozen_root):
    validation = validate_taskpack(taskpack_dir)
    loaded = load_taskpack(taskpack_dir)
    taskpack_id = loaded["taskpack"]["taskpack_id"]
    frozen_dir = Path(frozen_root).resolve() / taskpack_id
    if frozen_dir.exists():
        raise TaskpackValidationError(f"frozen taskpack already exists: {frozen_dir}")
    shutil.copytree(taskpack_dir, frozen_dir)

    frozen_taskpack = _read_json(frozen_dir / "taskpack.yaml")
    frozen_taskpack["status"] = "frozen"
    _write_json(frozen_dir / "taskpack.yaml", frozen_taskpack)

    digest = _digest_taskpack_files(frozen_dir)
    manifest = {
        "manifest_schema_version": "taskpack_manifest.v1",
        "taskpack_id": taskpack_id,
        "status": "frozen",
        "digest_sha256": digest,
        "source_taskpack_dir": str(Path(taskpack_dir).resolve()),
        "validation": validation,
    }
    _write_json(frozen_dir / "manifest.json", manifest)
    return {"frozen_taskpack_dir": str(frozen_dir), "manifest": manifest}


def _is_git_repo(path):
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _digest_taskpack_files(taskpack_dir):
    hasher = hashlib.sha256()
    for name in ["taskpack.yaml", "agent_pool.json", "backlog.json", "verification.json", "README.md"]:
        path = Path(taskpack_dir) / name
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()
```

Modify `__init__.py` by adding exports:

```python
    "freeze_taskpack": (".taskpack", "freeze_taskpack"),
    "validate_taskpack": (".taskpack", "validate_taskpack"),
```

- [ ] **Step 4: Run validator/freezer tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "feat: validate and freeze taskpacks"
```

## Task 3: Taskpack Runtime Argument Translation

**Files:**
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Add runtime translation test**

Append this method to `TaskpackTests`:

```python
    def test_build_taskpack_runtime_args_uses_frozen_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args.",
                draft_root=drafts,
                taskpack_id="runtime-args",
                write_scope=["src/"],
                verification_command=["python3", "-m", "unittest", "discover"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                daemon=True,
                max_inflight=2,
                commit_verified_integration=False,
            )

            self.assertEqual(args[0:2], ["--agent-pool", str(Path(frozen["frozen_taskpack_dir"]) / "agent_pool.json")])
            self.assertIn("--daemon-run-until-idle", args)
            self.assertIn("--daemon-two-phase-worker-pool", args)
            self.assertIn("--runtime", args)
            self.assertIn("codex", args)
            self.assertIn("--integrate-accepted-patch", args)
            self.assertNotIn("--commit-verified-integration", args)
            self.assertIn("--integration-verification-command-json", args)
            self.assertTrue((run_root / "runtime-args").exists())
```

Update imports:

```python
from agentteam_runtime import (
    TaskpackValidationError,
    build_taskpack_runtime_args,
    draft_taskpack_files,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_build_taskpack_runtime_args_uses_frozen_files -v
```

Expected: FAIL with missing `build_taskpack_runtime_args`.

- [ ] **Step 3: Implement runtime argument translation**

Add to `taskpack.py`:

```python
def build_taskpack_runtime_args(
    frozen_taskpack_dir,
    run_root,
    daemon=True,
    max_inflight=2,
    max_attempts=1,
    commit_verified_integration=False,
):
    loaded = load_taskpack(frozen_taskpack_dir)
    taskpack = loaded["taskpack"]
    if taskpack.get("status") != "frozen":
        raise TaskpackValidationError("taskpack must be frozen before runtime launch")
    taskpack_id = taskpack["taskpack_id"]
    run_dir = Path(run_root).resolve() / taskpack_id
    run_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "--agent-pool",
        str(Path(frozen_taskpack_dir) / taskpack["files"]["agent_pool"]),
        "--backlog",
        str(Path(frozen_taskpack_dir) / taskpack["files"]["backlog"]),
        "--output-dir",
        str(run_dir),
        "--project-root",
        taskpack["project_root"],
    ]
    if daemon:
        args.extend(["--daemon-run-until-idle", "--daemon-two-phase-worker-pool"])
        args.extend(["--max-inflight", str(max_inflight), "--max-attempts", str(max_attempts)])
    else:
        args.append("--run-until-idle")
    args.extend(["--runtime", taskpack["runtime"]["default_backend"]])
    args.append("--integrate-accepted-patch")
    command_json = json.dumps(loaded["verification"]["command"])
    args.extend(["--integration-verification-command-json", command_json])
    if commit_verified_integration:
        args.append("--commit-verified-integration")
    return args
```

Modify `__init__.py` by adding:

```python
    "build_taskpack_runtime_args": (".taskpack", "build_taskpack_runtime_args"),
```

- [ ] **Step 4: Run taskpack tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "feat: translate taskpacks to runtime args"
```

## Task 4: Fake And Codex Taskpack Author

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Add fake author test**

Append this method to `TaskpackTests`:

```python
    def test_fake_taskpack_author_drafts_safe_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Improve fixture behavior.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="fake-authored",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(loaded["taskpack"]["taskpack_id"], "fake-authored")
            self.assertEqual(loaded["backlog"]["items"][0]["required_role"], "implementation_worker")
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")
```

Update imports:

```python
from agentteam_runtime import (
    TaskpackValidationError,
    build_taskpack_runtime_args,
    draft_taskpack_files,
    draft_taskpack_from_goal,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_fake_taskpack_author_drafts_safe_taskpack -v
```

Expected: FAIL with missing `draft_taskpack_from_goal`.

- [ ] **Step 3: Implement fake author and Codex prompt scaffolding**

Create `taskpack_author.py`:

```python
import json
import subprocess
import tempfile
from pathlib import Path

from .repo_map import build_repository_map
from .taskpack import TaskpackValidationError, draft_taskpack_files


def draft_taskpack_from_goal(
    project_root,
    goal,
    draft_root,
    author_runtime="fake",
    taskpack_id=None,
    codex_command=None,
    codex_timeout_seconds=600,
):
    if author_runtime == "fake":
        return draft_taskpack_files(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            read_scope=["."],
            write_scope=[".agentteam/generated/"],
            verification_command=["python3", "-m", "unittest", "discover"],
        )
    if author_runtime == "codex":
        return _draft_with_codex(
            project_root=project_root,
            goal=goal,
            draft_root=draft_root,
            taskpack_id=taskpack_id,
            codex_command=codex_command,
            codex_timeout_seconds=codex_timeout_seconds,
        )
    raise TaskpackValidationError(f"unsupported author_runtime: {author_runtime}")


def _draft_with_codex(project_root, goal, draft_root, taskpack_id, codex_command, codex_timeout_seconds):
    project_root = Path(project_root).resolve()
    draft_root = Path(draft_root).resolve()
    taskpack_id = taskpack_id or "codex-authored-taskpack"
    taskpack_dir = draft_root / taskpack_id
    taskpack_dir.mkdir(parents=True, exist_ok=False)
    context_dir = taskpack_dir / "author_context"
    context_dir.mkdir()
    repo_map = build_repository_map(project_root, context_dir)
    context = {
        "project_root": str(project_root),
        "goal": goal,
        "taskpack_id": taskpack_id,
        "repo_map_manifest": repo_map["manifest"],
        "repo_inventory_sample": repo_map["inventory"]["files"][:200],
        "required_files": ["taskpack.yaml", "agent_pool.json", "backlog.json", "verification.json", "README.md"],
        "hard_rules": [
            "write only inside the taskpack directory",
            "do not edit the target repository",
            "produce JSON-compatible YAML in taskpack.yaml",
            "use bounded repository-relative write_scope values",
            "use Codex as the runtime backend",
        ],
    }
    (context_dir / "author_context.json").write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")
    result_path = taskpack_dir / "author_result.json"
    command = list(codex_command or ["codex", "exec"])
    command.extend(["-C", str(taskpack_dir), "-s", "workspace-write", "--output-last-message", str(result_path), "-"])
    completed = subprocess.run(
        command,
        cwd=taskpack_dir,
        input=_author_prompt(context),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=codex_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise TaskpackValidationError(f"codex author failed: {completed.stderr}")
    for name in context["required_files"]:
        if not (taskpack_dir / name).exists():
            raise TaskpackValidationError(f"codex author did not write required file: {name}")
    return {"taskpack_dir": str(taskpack_dir), "taskpack_id": taskpack_id, "author_result_path": str(result_path)}


def _author_prompt(context):
    return (
        "You are the AgentTeam Taskpack Author Agent. "
        "Read author_context/author_context.json and create the required taskpack files. "
        "Do not modify the target repository. "
        "The taskpack must be valid JSON in every .json file and JSON-compatible YAML in taskpack.yaml. "
        "Use bounded read_scope/write_scope and include measurable verification. "
        "After writing files, summarize assumptions in author_result.json through --output-last-message.\n\n"
        + json.dumps(context, indent=2, sort_keys=True)
    )
```

Modify `__init__.py`:

```python
    "draft_taskpack_from_goal": (".taskpack_author", "draft_taskpack_from_goal"),
```

- [ ] **Step 4: Run fake author test**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_fake_taskpack_author_drafts_safe_taskpack -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/__init__.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "feat: add taskpack author agent scaffold"
```

## Task 5: Human-Facing AgentTeam CLI

**Files:**
- Create: `experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py`
- Modify: `experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py`

- [ ] **Step 1: Add CLI smoke tests for draft and validate**

Append this method to `TaskpackTests`:

```python
    def test_agentteam_cli_draft_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            draft_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "draft",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Draft through CLI.",
                    "--draft-root",
                    str(drafts),
                    "--taskpack-id",
                    "cli-draft",
                    "--author-runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(draft_completed.returncode, 0, draft_completed.stderr)
            draft_summary = json.loads(draft_completed.stdout)
            self.assertEqual(draft_summary["taskpack_id"], "cli-draft")

            validate_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "validate",
                    str(drafts / "cli-draft"),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(validate_completed.returncode, 0, validate_completed.stderr)
            self.assertEqual(json.loads(validate_completed.stdout)["status"], "accepted")
```

Add helper near `_init_repo`:

```python
def _test_env():
    import os

    env = os.environ.copy()
    runtime_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = runtime_root if not current else f"{runtime_root}:{current}"
    return env
```

- [ ] **Step 2: Run CLI test to verify it fails**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_agentteam_cli_draft_and_validate -v
```

Expected: FAIL with missing module `agentteam_runtime.agentteam`.

- [ ] **Step 3: Implement CLI module**

Create `agentteam.py`:

```python
import argparse
import json
import subprocess
import sys

from .taskpack import build_taskpack_runtime_args, freeze_taskpack, validate_taskpack
from .taskpack_author import draft_taskpack_from_goal


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentTeam operator CLI.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    taskpack_parser = subcommands.add_parser("taskpack")
    taskpack_subcommands = taskpack_parser.add_subparsers(dest="taskpack_command", required=True)

    draft = taskpack_subcommands.add_parser("draft")
    draft.add_argument("--project-root", required=True)
    draft.add_argument("--goal", required=True)
    draft.add_argument("--draft-root", required=True)
    draft.add_argument("--taskpack-id")
    draft.add_argument("--author-runtime", choices=["fake", "codex"], default="fake")
    draft.add_argument("--codex-timeout-seconds", type=int, default=600)
    draft.add_argument("--codex-command", nargs=argparse.REMAINDER)

    validate = taskpack_subcommands.add_parser("validate")
    validate.add_argument("taskpack_dir")

    freeze = taskpack_subcommands.add_parser("freeze")
    freeze.add_argument("taskpack_dir")
    freeze.add_argument("--frozen-root", required=True)

    run = subcommands.add_parser("run")
    run.add_argument("frozen_taskpack_dir")
    run.add_argument("--run-root", required=True)
    run.add_argument("--one-shot", action="store_true")
    run.add_argument("--max-inflight", type=int, default=2)
    run.add_argument("--max-attempts", type=int, default=1)
    run.add_argument("--commit-verified-integration", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "taskpack" and args.taskpack_command == "draft":
            result = draft_taskpack_from_goal(
                project_root=args.project_root,
                goal=args.goal,
                draft_root=args.draft_root,
                author_runtime=args.author_runtime,
                taskpack_id=args.taskpack_id,
                codex_command=args.codex_command,
                codex_timeout_seconds=args.codex_timeout_seconds,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "taskpack" and args.taskpack_command == "validate":
            print(json.dumps(validate_taskpack(args.taskpack_dir), sort_keys=True))
            return 0
        if args.command == "taskpack" and args.taskpack_command == "freeze":
            print(json.dumps(freeze_taskpack(args.taskpack_dir, args.frozen_root), sort_keys=True))
            return 0
        if args.command == "run":
            runtime_args = build_taskpack_runtime_args(
                args.frozen_taskpack_dir,
                run_root=args.run_root,
                daemon=not args.one_shot,
                max_inflight=args.max_inflight,
                max_attempts=args.max_attempts,
                commit_verified_integration=args.commit_verified_integration,
            )
            command = [sys.executable, "-m", "agentteam_runtime.cli", *runtime_args]
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            if completed.stdout:
                print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
            return completed.returncode
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled args: {args}")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack.TaskpackTests.test_agentteam_cli_draft_and_validate -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py
git commit -m "feat: add agentteam taskpack cli"
```

## Task 6: Documentation And Final Verification

**Files:**
- Modify: `experiments/native_agentteam_runtime/README.md`
- Modify: `experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-04-taskpack-author-agent.md` only if implementation reveals a necessary clarification.

- [ ] **Step 1: Document the operator flow**

Append this section to `README.md`:

```markdown
## Taskpack Authoring Flow

The runtime can draft, validate, freeze, and run taskpacks without requiring the
operator to hand-write the low-level scheduler JSON.

Development entry point:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam taskpack draft \
  --project-root /path/to/repo \
  --goal "optimize the target behavior under an explicit metric" \
  --draft-root /tmp/agentteam-taskpacks/drafts \
  --taskpack-id example-taskpack \
  --author-runtime fake
```

Validate and freeze:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam taskpack validate /tmp/agentteam-taskpacks/drafts/example-taskpack

PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam taskpack freeze \
  /tmp/agentteam-taskpacks/drafts/example-taskpack \
  --frozen-root /tmp/agentteam-taskpacks/frozen
```

Run through the existing scheduler, mailbox worker pool, Codex adapter, and
integration verifier:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m agentteam_runtime.agentteam run \
  /tmp/agentteam-taskpacks/frozen/example-taskpack \
  --run-root /tmp/agentteam-runs
```

The `taskpack draft --author-runtime codex` mode is live-agent authoring. It
writes only the taskpack draft directory, then the deterministic validator must
accept the taskpack before runtime execution.
```
```

- [ ] **Step 2: Run focused taskpack tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack -v
```

Expected: PASS.

- [ ] **Step 3: Run full M0 tests**

Run:

```bash
PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime \
python3 -m unittest discover -s experiments/native_agentteam_runtime/m0_runtime/tests -v
```

Expected: PASS.

- [ ] **Step 4: Run compile check**

Run:

```bash
python3 -m compileall experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime
```

Expected: command exits with code 0.

- [ ] **Step 5: Run unresolved-marker scan**

Run:

```bash
rg -n "T[B]D|TO[D]O|FI[X]ME|place[ -]?holder|implement[ ]later" \
  experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime \
  experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py \
  experiments/native_agentteam_runtime/README.md
```

Expected: no matches relevant to the new taskpack implementation.

- [ ] **Step 6: Commit documentation**

```bash
git add experiments/native_agentteam_runtime/README.md \
  experiments/native_agentteam_runtime/implementation_artifacts/designs/2026-06-04-taskpack-author-agent.md
git commit -m "docs: document taskpack authoring flow"
```

- [ ] **Step 7: Final status check**

Run:

```bash
git status --short --branch
```

Expected: clean worktree on `native-runtime-m0`, ahead by the implementation commits.

## Self-Review

Spec coverage:

- Agent-generated taskpack: Task 4 implements fake and Codex authoring paths.
- Validator-owned executability: Task 2 implements deterministic validation and rejection.
- Frozen runnable artifact: Task 2 implements freezing and digest manifest.
- Runtime launch through existing scheduler path: Task 3 translates frozen taskpacks to `agentteam_runtime.cli` args; Task 5 executes that path.
- Human-facing entry point: Task 5 adds `python -m agentteam_runtime.agentteam`; README explains it as the development entry before a future installed `agentteam` console command.
- Verisilicon applicability: the generic taskpack accepts explicit goal, project root, write scope, and verification command; a Verisilicon-specific profile can be added after this core layer without changing the validator/freezer boundary.

Unresolved-marker scan:

- This plan intentionally contains no unresolved markers or unresolved file names.

Type consistency:

- Public helper names are consistent across tasks: `draft_taskpack_files`, `load_taskpack`, `validate_taskpack`, `freeze_taskpack`, `build_taskpack_runtime_args`, and `draft_taskpack_from_goal`.

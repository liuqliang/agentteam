import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime import (
    TaskpackValidationError,
    draft_taskpack_files,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
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

    def test_draft_taskpack_files_rejects_unsafe_taskpack_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            cases = [
                ("../escape", tmp_path / "escape"),
                (str(tmp_path / "absolute"), tmp_path / "absolute"),
            ]
            for taskpack_id, escaped_path in cases:
                with self.subTest(taskpack_id=taskpack_id):
                    with self.assertRaises(TaskpackValidationError):
                        draft_taskpack_files(
                            project_root=repo,
                            goal="Reject unsafe taskpack IDs.",
                            draft_root=drafts,
                            taskpack_id=taskpack_id,
                        )
                    self.assertFalse(escaped_path.exists())

    def test_draft_taskpack_files_rejects_invalid_string_sequences(self):
        cases = [
            {"read_scope": "."},
            {"write_scope": "src/"},
            {"verification_command": "python3 -m unittest"},
            {"write_scope": ["src/", 123]},
        ]

        for index, kwargs in enumerate(cases):
            with self.subTest(kwargs=kwargs):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)

                    with self.assertRaises(TaskpackValidationError):
                        draft_taskpack_files(
                            project_root=repo,
                            goal="Reject invalid sequence inputs.",
                            draft_root=drafts,
                            taskpack_id=f"sequence-{index}",
                            **kwargs,
                        )

    def test_load_taskpack_rejects_companion_paths_outside_taskpack_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            cases = [
                ("../agent_pool.json", lambda taskpack_dir: taskpack_dir.parent / "agent_pool.json"),
                (str(tmp_path / "outside.json"), lambda taskpack_dir: tmp_path / "outside.json"),
            ]
            for index, (unsafe_path, target_path_for) in enumerate(cases):
                with self.subTest(unsafe_path=unsafe_path):
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject unsafe companion file paths.",
                        draft_root=drafts,
                        taskpack_id=f"loader-{index}",
                    )
                    taskpack_dir = Path(result["taskpack_dir"])
                    target_path = target_path_for(taskpack_dir)
                    target_path.write_text("{}", encoding="utf-8")
                    taskpack_path = taskpack_dir / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    taskpack["files"]["agent_pool"] = unsafe_path
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError):
                        load_taskpack(taskpack_dir)

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

    def test_validate_taskpack_rejects_parent_relative_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject parent-relative write scope.",
                draft_root=drafts,
                taskpack_id="parent-write-scope",
                write_scope=["../outside"],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope", str(raised.exception))

    def test_validate_taskpack_rejects_missing_taskpack_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing taskpack id.",
                draft_root=drafts,
                taskpack_id="missing-taskpack-id",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            del taskpack["taskpack_id"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("taskpack_id", str(raised.exception))

    def test_validate_taskpack_rejects_unsafe_taskpack_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject unsafe taskpack id.",
                draft_root=drafts,
                taskpack_id="unsafe-taskpack-id",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["taskpack_id"] = "../escaped"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("taskpack_id", str(raised.exception))

    def test_freeze_taskpack_rejects_unsafe_taskpack_id_without_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            escaped = tmp_path / "escaped"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject unsafe freeze target.",
                draft_root=drafts,
                taskpack_id="unsafe-freeze-id",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["taskpack_id"] = "../escaped"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                freeze_taskpack(result["taskpack_dir"], frozen_root)

            self.assertIn("taskpack_id", str(raised.exception))
            self.assertFalse(escaped.exists())

    def test_validate_taskpack_rejects_missing_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing project root.",
                draft_root=drafts,
                taskpack_id="missing-project-root",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            del taskpack["project_root"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("project_root", str(raised.exception))

    def test_validate_taskpack_rejects_missing_verification_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing verification file.",
                draft_root=drafts,
                taskpack_id="missing-verification",
                write_scope=["src/"],
            )
            (Path(result["taskpack_dir"]) / "verification.json").unlink()

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("verification", str(raised.exception))

    def test_validate_taskpack_rejects_non_list_backlog_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed backlog items.",
                draft_root=drafts,
                taskpack_id="malformed-backlog-items",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"] = {"task_id": "TASK"}
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("backlog.items", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_task_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed task id.",
                draft_root=drafts,
                taskpack_id="malformed-task-id",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["task_id"] = ["TASK"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("task_id", str(raised.exception))

    def test_validate_taskpack_rejects_invalid_write_scope_without_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject invalid write scope.",
                draft_root=drafts,
                taskpack_id="invalid-write-scope",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["write_scope"] = None
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope must be a non-empty list", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_write_scope_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed write scope entries.",
                draft_root=drafts,
                taskpack_id="malformed-write-scope",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["write_scope"] = [123]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope", str(raised.exception))

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

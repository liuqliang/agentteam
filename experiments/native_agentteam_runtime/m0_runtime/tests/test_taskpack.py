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

    def test_load_taskpack_rejects_non_object_taskpack_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed taskpack document.",
                draft_root=drafts,
                taskpack_id="malformed-taskpack-yaml",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack_path.write_text("[]", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                load_taskpack(result["taskpack_dir"])

            self.assertIn("taskpack", str(raised.exception))

    def test_validate_taskpack_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject invalid JSON.",
                draft_root=drafts,
                taskpack_id="invalid-json",
                write_scope=["src/"],
            )
            verification_path = Path(result["taskpack_dir"]) / "verification.json"
            verification_path.write_text("{", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertTrue("verification.json" in message or "invalid json" in message)

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

    def test_validate_taskpack_rejects_normalized_root_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject normalized root write scope.",
                draft_root=drafts,
                taskpack_id="normalized-root-write-scope",
                write_scope=["./."],
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("write_scope", str(raised.exception))

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

    def test_validate_taskpack_rejects_root_wide_glob_write_scope(self):
        cases = ["./*", "**/*", "./**", "./**/*"]
        for index, write_scope in enumerate(cases):
            with self.subTest(write_scope=write_scope):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject broad glob write scope.",
                        draft_root=drafts,
                        taskpack_id=f"root-glob-write-scope-{index}",
                        write_scope=[write_scope],
                    )

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn("write_scope", str(raised.exception))

    def test_validate_taskpack_rejects_root_prefix_wildcard_write_scope(self):
        cases = ["*.py", "*/*.py", "*/**/*"]
        for index, write_scope in enumerate(cases):
            with self.subTest(write_scope=write_scope):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject root-prefix wildcard write scope.",
                        draft_root=drafts,
                        taskpack_id=f"root-prefix-wildcard-{index}",
                        write_scope=[write_scope],
                    )

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn("write_scope", str(raised.exception))

    def test_validate_taskpack_accepts_scoped_glob_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Accept scoped glob write scope.",
                draft_root=drafts,
                taskpack_id="scoped-glob-write-scope",
                write_scope=["src/**/*.py"],
            )

            validation = validate_taskpack(result["taskpack_dir"])

            self.assertEqual(validation["status"], "accepted")

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

    def test_validate_taskpack_rejects_file_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject file project root.",
                draft_root=drafts,
                taskpack_id="file-project-root",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["project_root"] = str(repo / "README.md")
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("project_root", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed goal.",
                draft_root=drafts,
                taskpack_id="malformed-goal",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["goal"] = ["not", "a", "string"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("goal", str(raised.exception))

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

    def test_validate_taskpack_rejects_malformed_task_runtime_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed task runtime fields.",
                draft_root=drafts,
                taskpack_id="malformed-task-runtime-fields",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            item = backlog["items"][0]
            del item["objective"]
            item["required_role"] = ""
            item["read_scope"] = "src/"
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertIn("objective", message)
            self.assertIn("required_role", message)
            self.assertIn("read_scope", message)

    def test_validate_taskpack_rejects_invalid_depends_on_without_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed dependencies.",
                draft_root=drafts,
                taskpack_id="malformed-depends-on",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["depends_on"] = None
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("depends_on", str(raised.exception))

    def test_validate_taskpack_rejects_non_string_dependency_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed dependency entries.",
                draft_root=drafts,
                taskpack_id="malformed-dependency-entry",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["depends_on"] = [123]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("depends_on", str(raised.exception))

    def test_validate_taskpack_rejects_unknown_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject unknown dependency.",
                draft_root=drafts,
                taskpack_id="unknown-dependency",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["task_id"] = "TASK-A"
            backlog["items"][0]["depends_on"] = ["TASK-MISSING"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertTrue("depends_on" in message or "unknown" in message)

    def test_validate_taskpack_rejects_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject dependency cycle.",
                draft_root=drafts,
                taskpack_id="dependency-cycle",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            task_a = dict(backlog["items"][0])
            task_b = dict(backlog["items"][0])
            task_a["task_id"] = "TASK-A"
            task_a["depends_on"] = ["TASK-B"]
            task_b["task_id"] = "TASK-B"
            task_b["depends_on"] = ["TASK-A"]
            backlog["items"] = [task_a, task_b]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertTrue("cycle" in message or "depends_on" in message)

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

    def test_validate_taskpack_rejects_non_object_agent_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed agent pool.",
                draft_root=drafts,
                taskpack_id="malformed-agent-pool",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool_path.write_text("[]", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("agent_pool", str(raised.exception))

    def test_validate_taskpack_rejects_missing_agent_for_required_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject missing required role agent.",
                draft_root=drafts,
                taskpack_id="missing-required-role-agent",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["agents"][0]["role"] = "different-role"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("required_role", str(raised.exception))

    def test_validate_taskpack_rejects_non_object_role_runtime_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed role runtime profiles.",
                draft_root=drafts,
                taskpack_id="malformed-role-runtime-profiles",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"] = []
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("role_runtime_profiles", str(raised.exception))

    def test_validate_taskpack_rejects_malformed_role_runtime_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed role runtime profile.",
                draft_root=drafts,
                taskpack_id="malformed-role-runtime-profile",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"] = {"adapter": "unknown"}
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            self.assertIn("role_runtime_profiles", str(raised.exception))

    def test_validate_taskpack_rejects_malformed_optional_role_maps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed optional role maps.",
                draft_root=drafts,
                taskpack_id="malformed-optional-role-maps",
                write_scope=["src/"],
            )
            agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_prompt_contracts"] = []
            agent_pool["role_context_packages"] = []
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertIn("role_prompt_contracts", message)
            self.assertIn("role_context_packages", message)

    def test_freeze_taskpack_rejects_extra_draft_file_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject extra draft files.",
                draft_root=drafts,
                taskpack_id="extra-draft-file",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            (taskpack_dir / "extra.txt").write_text("not inventoried\n", encoding="utf-8")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(taskpack_dir, frozen_root)

            self.assertFalse((frozen_root / "extra-draft-file").exists())

    def test_freeze_taskpack_rejects_symlink_in_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject symlink artifacts.",
                draft_root=drafts,
                taskpack_id="symlink-draft-file",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            symlink_path = taskpack_dir / "link.json"
            try:
                symlink_path.symlink_to(taskpack_dir / "backlog.json")
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unsupported: {exc}")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(taskpack_dir, frozen_root)

    def test_freeze_taskpack_rejects_inventoried_symlink_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            outside = tmp_path / "outside-readme.md"
            _init_repo(repo)
            outside.write_text("outside\n", encoding="utf-8")
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject inventoried symlink artifacts.",
                draft_root=drafts,
                taskpack_id="inventoried-symlink",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            readme_path = taskpack_dir / "README.md"
            readme_path.unlink()
            try:
                readme_path.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"symlink creation unsupported: {exc}")

            with self.assertRaises(TaskpackValidationError):
                freeze_taskpack(taskpack_dir, frozen_root)

            self.assertFalse((frozen_root / "inventoried-symlink").exists())

    def test_freeze_taskpack_honors_companion_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Freeze mapped companion artifacts.",
                draft_root=drafts,
                taskpack_id="mapped-companion",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            nested_dir = taskpack_dir / "nested"
            nested_dir.mkdir()
            (taskpack_dir / "backlog.json").replace(nested_dir / "backlog.json")
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["files"]["backlog"] = "nested/backlog.json"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            validation = validate_taskpack(taskpack_dir)
            self.assertEqual(validation["status"], "accepted")

            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertTrue((frozen_dir / "nested" / "backlog.json").exists())
            self.assertFalse((frozen_dir / "backlog.json").exists())
            self.assertEqual(len(manifest["digest_sha256"]), 64)

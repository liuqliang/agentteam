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

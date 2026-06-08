import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentteam_runtime import (
    TaskpackValidationError,
    build_taskpack_runtime_args,
    draft_taskpack_files,
    draft_taskpack_from_goal,
    freeze_taskpack,
    load_taskpack,
    validate_taskpack,
)
from agentteam_runtime.diagnostic_chat import (
    build_runtime_diagnostic_context,
    render_runtime_diagnostic_context,
)
from agentteam_runtime.taskpack_author import _command_list
from agentteam_runtime.taskpack_author import _canonicalize_codex_taskpack_files


def _init_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _test_env():
    import os

    env = os.environ.copy()
    runtime_root = str(Path(__file__).resolve().parents[1])
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = runtime_root if not current else f"{runtime_root}:{current}"
    return env


def _arg_value(args, flag):
    index = args.index(flag)
    return args[index + 1]


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_failed_integration_run(run_dir):
    run_dir = Path(run_dir)
    integration_worktree = run_dir / "integration" / "optimize-pipeline"
    integration_worktree.mkdir(parents=True)
    (integration_worktree / "gesture_recognition").mkdir()
    (integration_worktree / "gesture_recognition" / "sim_eval.py").write_text(
        "# changed\n",
        encoding="utf-8",
    )
    failure_stderr = "\n".join(
        [
            "test_fast_path (test_sim_eval.SimEvalTest.test_fast_path) ... ok",
            "test_host_c_model_matches_exported_python_reference_exactly "
            "(test_c_algo.CAlgoTest.test_host_c_model_matches_exported_python_reference_exactly) ... FAIL",
            "",
            "======================================================================",
            "FAIL: test_host_c_model_matches_exported_python_reference_exactly "
            "(test_c_algo.CAlgoTest.test_host_c_model_matches_exported_python_reference_exactly)",
            "----------------------------------------------------------------------",
            "AssertionError: First differing element 346: 'pinch' != 'others'",
            "",
            "FAILED (failures=1)",
        ]
    )
    _write_json(
        run_dir / "state" / "integration_queue.json",
        {
            "queue_schema_version": "integration_queue.v1",
            "items": [
                {
                    "task_id": "optimize-pipeline",
                    "attempt_id": "optimize-pipeline-ATTEMPT-001",
                    "queue_status": "blocked",
                    "integration_status": "applied",
                    "integration_verification_status": "failed",
                    "integration_verification_exit_code": 1,
                    "integration_worktree_path": str(integration_worktree),
                }
            ],
        },
    )
    _write_json(
        run_dir / "codex_results" / "codex_result_optimize-pipeline-ATTEMPT-001.json",
        {
            "result_status": "completed",
            "changed_files": [
                "gesture_recognition/sim_eval.py",
                "gesture_recognition/tests/test_sim_eval.py",
            ],
            "output": {
                "operator_summary": {
                    "what_changed": "Optimized feature extraction and negative window selection.",
                    "verification_summary": "Local sim_eval tests passed.",
                }
            },
        },
    )
    _write_json(
        run_dir / "steps" / "STEP-0001-optimize-pipeline" / "backlog.json",
        {
            "items": [
                {
                    "task_id": "optimize-pipeline",
                    "title": "Optimize gesture evaluation pipeline",
                    "objective": "Improve Python evaluation speed without changing generated outputs.",
                    "write_scope": [
                        "gesture_recognition/sim_eval.py",
                        "gesture_recognition/tests/test_sim_eval.py",
                    ],
                }
            ]
        },
    )
    _write_jsonl(
        run_dir / "events.jsonl",
        [
            {
                "event_id": "EVT-0001",
                "event_type": "worker_result_recorded",
                "sequence": 1,
                "payload": {
                    "task_id": "optimize-pipeline",
                    "attempt_id": "optimize-pipeline-ATTEMPT-001",
                    "result_status": "completed",
                },
            },
            {
                "event_id": "EVT-0002",
                "event_type": "integration_verified",
                "sequence": 2,
                "payload": {
                    "task_id": "optimize-pipeline",
                    "attempt_id": "optimize-pipeline-ATTEMPT-001",
                    "integration_verification_status": "failed",
                    "integration_verification_exit_code": 1,
                    "integration_verification_stderr": failure_stderr,
                    "integration_verification_stdout": "",
                },
            },
        ],
    )
    return run_dir


REPO_ROOT = Path(__file__).resolve().parents[4]


class TaskpackTests(unittest.TestCase):
    def test_runtime_diagnostic_context_summarizes_failed_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_failed_integration_run(Path(tmp) / "runs" / "taskpack-5")

            context = build_runtime_diagnostic_context(run_dir, topic="integration-failure")
            rendered = render_runtime_diagnostic_context(context)

            self.assertEqual(context["agent_role"], "runtime_diagnostic_agent")
            self.assertEqual(context["chat_status"], "context_ready")
            self.assertEqual(context["topic"], "integration-failure")
            self.assertEqual(context["latest_failure"]["task_id"], "optimize-pipeline")
            self.assertEqual(context["latest_failure"]["failed_test"], "test_host_c_model_matches_exported_python_reference_exactly")
            self.assertIn("First differing element 346", context["latest_failure"]["stderr_excerpt"])
            self.assertEqual(
                context["worker_results"][0]["changed_files"],
                [
                    "gesture_recognition/sim_eval.py",
                    "gesture_recognition/tests/test_sim_eval.py",
                ],
            )
            self.assertIn("runtime_diagnostic_agent", rendered)
            self.assertIn("test_host_c_model_matches_exported_python_reference_exactly", rendered)
            self.assertIn("Read-only role", rendered)

    def test_agentteam_cli_chat_prints_diagnostic_context_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_failed_integration_run(Path(tmp) / "runs" / "taskpack-5")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "chat",
                    "--run-dir",
                    str(run_dir),
                    "--topic",
                    "integration-failure",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["chat_status"], "context_ready")
            self.assertEqual(summary["agent_role"], "runtime_diagnostic_agent")
            self.assertEqual(summary["latest_failure"]["failed_test"], "test_host_c_model_matches_exported_python_reference_exactly")

    def test_agentteam_cli_chat_interactive_launches_codex_tui_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = _write_failed_integration_run(tmp_path / "runs" / "taskpack-5")
            capture_path = tmp_path / "codex-argv.json"
            fake_codex = tmp_path / "fake_codex.py"
            fake_codex.write_text(
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                f"Path({str(capture_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "chat",
                    "--run-dir",
                    str(run_dir),
                    "--topic",
                    "integration-failure",
                    "--interactive",
                    "--codex-command",
                    "python3",
                    str(fake_codex),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            argv = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertNotIn("exec", argv)
            self.assertEqual(_arg_value(argv, "-C"), str(run_dir.resolve()))
            self.assertEqual(_arg_value(argv, "-s"), "read-only")
            self.assertIn("--no-alt-screen", argv)
            self.assertIn("runtime_diagnostic_agent", argv[-1])
            self.assertIn("test_host_c_model_matches_exported_python_reference_exactly", argv[-1])

    def test_install_local_replaces_existing_launcher_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / ".local" / "bin"
            bin_dir.mkdir(parents=True)
            target = bin_dir / "agentteam"
            target.symlink_to(REPO_ROOT / "agentteam")
            env = {**os.environ, "HOME": str(home)}

            completed = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts" / "install-local.sh")],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(target.is_symlink())
            config = json.loads(
                (home / ".local" / "share" / "agentteam" / "launcher.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(config["development_repo_root"], str(REPO_ROOT))

    def test_agentteam_cli_submit_fake_one_shot_runs_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "taskpacks"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "submit",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Submit fake one-shot taskpack.",
                    "--work-root",
                    str(work_root),
                    "--taskpack-id",
                    "cli-submit-fake",
                    "--author-runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-submit-fake")
            self.assertEqual(summary["validation"]["status"], "accepted")
            self.assertEqual(summary["runtime"], "fake")
            self.assertTrue((work_root / "drafts" / "cli-submit-fake").exists())
            self.assertTrue((work_root / "frozen" / "cli-submit-fake" / "manifest.json").exists())
            self.assertEqual(summary["run"]["scheduler_status"], "idle")
            self.assertEqual(
                summary["run"]["snapshot"]["tasks"]["TASK-CLI_SUBMIT_FAKE-001"]["task_status"],
                "done",
            )

    def test_agentteam_cli_submit_interactive_prompts_for_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "taskpacks"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "submit",
                    "--interactive",
                ],
                input="\n".join(
                    [
                        str(repo),
                        "Submit interactive fake taskpack.",
                        str(work_root),
                        "cli-submit-interactive",
                        "fake",
                        "auto",
                        "y",
                        "n",
                    ]
                )
                + "\n",
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertIn("Project root", completed.stderr)
            self.assertIn("Goal", completed.stderr)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-submit-interactive")
            self.assertEqual(summary["runtime"], "fake")
            self.assertEqual(summary["run"]["scheduler_status"], "idle")
            self.assertEqual(
                summary["run"]["snapshot"]["tasks"]["TASK-CLI_SUBMIT_INTERACTIVE-001"]["task_status"],
                "done",
            )

    def test_agentteam_cli_init_writes_project_profile_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "fixture-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "codex",
                    "--runtime",
                    "auto",
                    "--notification-project",
                    "fixture-project",
                    "--feishu-webhook-env",
                    "AGENTTEAM_FEISHU_FIXTURE_WEBHOOK",
                    "--feishu-signing-secret-env",
                    "AGENTTEAM_FEISHU_FIXTURE_SECRET",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            profile_path = repo / ".agentteam" / "profile.json"
            self.assertEqual(Path(summary["profile_path"]), profile_path)
            self.assertTrue(profile_path.exists())
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["profile_schema_version"], "agentteam_profile.v1")
            self.assertEqual(profile["project_key"], "fixture-project")
            self.assertEqual(profile["work_root"], str(work_root.resolve()))
            self.assertEqual(profile["author_runtime"], "codex")
            self.assertEqual(profile["default_runtime"], "auto")
            self.assertEqual(profile["notification_project"], "fixture-project")
            self.assertEqual(profile["feishu"]["webhook_env"], "AGENTTEAM_FEISHU_FIXTURE_WEBHOOK")
            self.assertEqual(profile["feishu"]["signing_secret_env"], "AGENTTEAM_FEISHU_FIXTURE_SECRET")
            serialized = json.dumps(profile, sort_keys=True)
            self.assertNotIn("https://open.feishu.cn", serialized)
            self.assertNotIn("secret-token", serialized)

    def test_agentteam_cli_init_text_is_concise_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "fixture-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "codex",
                    "--runtime",
                    "auto",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("init_status: initialized\n", completed.stdout)
            self.assertIn("project: fixture-project\n", completed.stdout)
            self.assertIn("profile_path:", completed.stdout)
            self.assertNotIn("{", completed.stdout)
            self.assertNotIn("profile_schema_version", completed.stdout)

    def test_agentteam_cli_init_keeps_project_git_status_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "clean-profile",
                    "--author-runtime",
                    "codex",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            self.assertEqual(status.stdout, "")
            exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn(".agentteam/", exclude)

    def test_agentteam_cli_start_uses_project_profile_to_submit_fake_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)

            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "fixture-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "auto",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Start from project profile.",
                    "--taskpack-id",
                    "cli-start-profile",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-start-profile")
            self.assertEqual(summary["runtime"], "fake")
            self.assertEqual(summary["profile"]["profile_path"], str((repo / ".agentteam" / "profile.json").resolve()))
            self.assertEqual(summary["paths"]["work_root"], str(work_root.resolve()))
            self.assertTrue((work_root / "drafts" / "cli-start-profile").exists())
            self.assertEqual(summary["run"]["scheduler_status"], "idle")

    def test_agentteam_cli_start_prints_progress_to_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "progress-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Show progress while starting.",
                    "--taskpack-id",
                    "cli-start-progress",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            json.loads(completed.stdout)
            self.assertIn("[agentteam] profile loaded: progress-project", completed.stderr)
            self.assertIn("[agentteam] authoring taskpack with fake", completed.stderr)
            self.assertIn("[agentteam] draft accepted: cli-start-progress", completed.stderr)
            self.assertIn("[agentteam] frozen taskpack created: cli-start-progress", completed.stderr)
            self.assertIn("[agentteam] runtime started:", completed.stderr)
            self.assertIn("[agentteam] run idle", completed.stderr)

    def test_agentteam_cli_status_summarizes_latest_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for status.",
                    "--taskpack-id",
                    "cli-status-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("project: status-project", status_completed.stdout)
            self.assertIn("latest_run: cli-status-run", status_completed.stdout)
            self.assertIn("status: idle", status_completed.stdout)
            self.assertIn("tasks: 1 done, 0 blocked", status_completed.stdout)
            self.assertIn("inflight: 0", status_completed.stdout)
            self.assertIn("manual_gates: 0", status_completed.stdout)
            self.assertIn(str((work_root / "runs" / "cli-status-run").resolve()), status_completed.stdout)

    def test_agentteam_cli_status_reports_inflight_and_stopped_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "inflight-run"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "max_ticks_reached",
                        "backlog": {
                            "items": [
                                {
                                    "task_id": "optimize-pipeline",
                                    "backlog_status": "ready",
                                }
                            ]
                        },
                        "inflight_attempts": [
                            {
                                "task_id": "optimize-pipeline",
                                "attempt_id": "ATTEMPT-001",
                                "agent_id": "implementation-worker-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "exit_code": -15,
                                "stopped_by": "terminated",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("latest_run: inflight-run", status_completed.stdout)
            self.assertIn("status: max_ticks_reached", status_completed.stdout)
            self.assertIn("inflight: 1", status_completed.stdout)
            self.assertIn("workers: 1 stopped, 0 running, 0 quarantined", status_completed.stdout)
            self.assertIn(
                "last_worker: implementation-worker-1 stopped exit_code=-15 stopped_by=terminated",
                status_completed.stdout,
            )

    def test_agentteam_cli_status_reports_running_stale_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stale-run"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "worker_pid": 999999999,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            json_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
            summary = json.loads(json_completed.stdout)
            self.assertEqual(summary["liveness_status"], "running-stale")
            self.assertEqual(summary["processes"]["live"], 0)
            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("liveness: running-stale", text_completed.stdout)

    def test_agentteam_cli_status_reports_running_alive_liveness(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "alive-run"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "status-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "running",
                                "worker_pid": os.getpid(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            summary = json.loads(status_completed.stdout)
            self.assertEqual(summary["liveness_status"], "running-alive")
            self.assertEqual(summary["processes"]["live"], 1)

    def test_agentteam_cli_watch_prints_one_progress_line_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "runs" / "watch-run"
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            registry_path = run_dir / "state" / "worker_process_registry.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "backlog": {"items": [{"task_id": "watch-task", "backlog_status": "ready"}]},
                        "inflight_attempts": [{"task_id": "watch-task", "attempt_id": "ATTEMPT-001"}],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            registry_path.write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "worker_pid": 999999999,
                            }
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            before_state = state_path.read_text(encoding="utf-8")
            before_registry = registry_path.read_text(encoding="utf-8")

            watch_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "watch",
                    "--run-dir",
                    str(run_dir),
                    "--interval",
                    "0",
                    "--max-lines",
                    "1",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(watch_completed.returncode, 0, watch_completed.stderr)
            self.assertEqual(len([line for line in watch_completed.stdout.splitlines() if line.strip()]), 1)
            self.assertIn("run=watch-run", watch_completed.stdout)
            self.assertIn("liveness=running-stale", watch_completed.stdout)
            self.assertEqual(state_path.read_text(encoding="utf-8"), before_state)
            self.assertEqual(registry_path.read_text(encoding="utf-8"), before_registry)

    def test_agentteam_cli_stop_marks_latest_run_stopped_and_writes_stop_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stop-run"
            stop_file = run_dir / "workers" / "implementation-worker-1.stop"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "backlog": {"items": [{"task_id": "optimize", "backlog_status": "ready"}]},
                        "inflight_attempts": [{"task_id": "optimize", "attempt_id": "ATTEMPT-001"}],
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_pid": 999999999,
                                "worker_status": "running",
                                "stop_file": str(stop_file),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stop_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
            self.assertIn("project: stop-project", stop_completed.stdout)
            self.assertIn("latest_run: stop-run", stop_completed.stdout)
            self.assertIn("stop_status: stopped", stop_completed.stdout)
            self.assertTrue(stop_file.exists())
            registry = json.loads((run_dir / "state" / "worker_process_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["registry_status"], "stopped")
            self.assertEqual(registry["workers"][0]["worker_status"], "stopped")
            self.assertEqual(registry["workers"][0]["stopped_by"], "stale_pid")
            state = json.loads((run_dir / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["scheduler_status"], "stopped")
            self.assertEqual(state["previous_scheduler_status"], "running")
            self.assertEqual(len(state["inflight_attempts"]), 1)

    def test_agentteam_cli_stop_stale_skips_live_registered_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "live-run"
            stop_file = run_dir / "workers" / "implementation-worker-1.stop"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_pid": os.getpid(),
                                "worker_status": "running",
                                "stop_file": str(stop_file),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stop_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "live-run",
                    "--stale",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
            summary = json.loads(stop_completed.stdout)
            self.assertEqual(summary["stop_status"], "not_stale")
            self.assertFalse(stop_file.exists())
            registry = json.loads((run_dir / "state" / "worker_process_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["registry_status"], "running")
            self.assertEqual(registry["workers"][0]["worker_status"], "running")
            state = json.loads((run_dir / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["scheduler_status"], "running")

    def test_agentteam_cli_stop_stale_cleans_all_stale_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            stale_run = work_root / "runs" / "stale-run"
            live_run = work_root / "runs" / "live-run"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for run_dir, worker_pid in [(stale_run, 999999999), (live_run, os.getpid())]:
                (run_dir / "state").mkdir(parents=True)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                    encoding="utf-8",
                )
                (run_dir / "state" / "worker_process_registry.json").write_text(
                    json.dumps(
                        {
                            "registry_status": "running",
                            "workers": [
                                {
                                    "worker_agent_id": "implementation-worker-1",
                                    "worker_pid": worker_pid,
                                    "worker_status": "running",
                                    "stop_file": str(run_dir / "workers" / "implementation-worker-1.stop"),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            stop_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--project-root",
                    str(repo),
                    "--stale",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
            summary = json.loads(stop_completed.stdout)
            self.assertEqual(summary["stop_status"], "stale_cleaned")
            self.assertEqual(summary["cleaned_count"], 1)
            stale_registry = json.loads(
                (stale_run / "state" / "worker_process_registry.json").read_text(encoding="utf-8")
            )
            live_registry = json.loads(
                (live_run / "state" / "worker_process_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stale_registry["registry_status"], "stopped")
            self.assertEqual(stale_registry["workers"][0]["worker_status"], "stopped")
            self.assertEqual(live_registry["registry_status"], "running")
            self.assertEqual(live_registry["workers"][0]["worker_status"], "running")

    def test_agentteam_cli_stop_terminates_registered_worker_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "live-stop-run"
            stop_file = run_dir / "workers" / "implementation-worker-1.stop"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "stop-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                (run_dir / "state").mkdir(parents=True)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                    encoding="utf-8",
                )
                (run_dir / "state" / "worker_process_registry.json").write_text(
                    json.dumps(
                        {
                            "registry_status": "running",
                            "workers": [
                                {
                                    "worker_agent_id": "implementation-worker-1",
                                    "worker_pid": worker.pid,
                                    "worker_status": "running",
                                    "stop_file": str(stop_file),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                stop_completed = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "agentteam_runtime.agentteam",
                        "stop",
                        "--project-root",
                        str(repo),
                        "--run-dir",
                        str(run_dir),
                        "--grace-seconds",
                        "1",
                        "--json",
                    ],
                    env=_test_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )

                self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
                summary = json.loads(stop_completed.stdout)
                self.assertEqual(summary["stop_status"], "stopped")
                self.assertTrue(stop_file.exists())
                worker.wait(timeout=5)
                registry = json.loads((run_dir / "state" / "worker_process_registry.json").read_text(encoding="utf-8"))
                self.assertEqual(registry["registry_status"], "stopped")
                self.assertEqual(registry["workers"][0]["worker_status"], "stopped")
                self.assertEqual(registry["workers"][0]["stopped_by"], "terminated")
            finally:
                if worker.poll() is None:
                    worker.kill()
                worker.wait(timeout=5)

    def test_agentteam_cli_taskpack_list_shows_frozen_taskpacks_and_run_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "list-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create listed frozen taskpack.",
                    "--taskpack-id",
                    "listed-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            extra = draft_taskpack_from_goal(
                project_root=repo,
                goal="Create listed frozen taskpack without run.",
                draft_root=work_root / "drafts",
                author_runtime="fake",
                taskpack_id="listed-not-run",
            )
            freeze_taskpack(extra["taskpack_dir"], work_root / "frozen")

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            self.assertIn("project: list-project", list_completed.stdout)
            self.assertIn("frozen_count: 2", list_completed.stdout)
            self.assertIn("listed-run", list_completed.stdout)
            self.assertIn("run_status=idle", list_completed.stdout)
            self.assertIn("listed-not-run", list_completed.stdout)
            self.assertIn("run_status=not_run", list_completed.stdout)

    def test_agentteam_cli_taskpack_list_uses_liveness_aware_run_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stale-listed"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "list-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            taskpack = draft_taskpack_from_goal(
                project_root=repo,
                goal="Create stale listed frozen taskpack.",
                draft_root=work_root / "drafts",
                author_runtime="fake",
                taskpack_id="stale-listed",
            )
            freeze_taskpack(taskpack["taskpack_dir"], work_root / "frozen")
            (run_dir / "state").mkdir(parents=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "stopped",
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "stopped",
                                "worker_pid": 999999999,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            self.assertIn("stale-listed", list_completed.stdout)
            self.assertIn("run_status=running-stale", list_completed.stdout)

    def test_agentteam_cli_taskpack_delete_dry_run_reports_paths_without_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "delete-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for base in ["drafts", "frozen", "runs"]:
                path = work_root / base / "delete-me"
                path.mkdir(parents=True)
                (path / "marker.txt").write_text(base, encoding="utf-8")

            delete_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "delete",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "delete-me",
                    "--dry-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(delete_completed.returncode, 0, delete_completed.stderr)
            summary = json.loads(delete_completed.stdout)
            self.assertEqual(summary["delete_status"], "dry_run")
            self.assertEqual(summary["skipped_run"], str((work_root / "runs" / "delete-me").resolve()))
            self.assertTrue((work_root / "drafts" / "delete-me").exists())
            self.assertTrue((work_root / "frozen" / "delete-me").exists())
            self.assertTrue((work_root / "runs" / "delete-me").exists())

    def test_agentteam_cli_taskpack_delete_requires_explicit_run_delete_and_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "delete-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for base in ["drafts", "frozen", "runs"]:
                path = work_root / base / "delete-me"
                path.mkdir(parents=True)
                (path / "marker.txt").write_text(base, encoding="utf-8")

            refused = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "delete",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "delete-me",
                    "--force",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("run exists", refused.stderr)

            deleted = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "delete",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "delete-me",
                    "--delete-run",
                    "--force",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(deleted.returncode, 0, deleted.stderr)
            summary = json.loads(deleted.stdout)
            self.assertEqual(summary["delete_status"], "deleted")
            self.assertEqual(summary["deleted_count"], 3)
            self.assertFalse((work_root / "drafts" / "delete-me").exists())
            self.assertFalse((work_root / "frozen" / "delete-me").exists())
            self.assertFalse((work_root / "runs" / "delete-me").exists())

    def test_agentteam_cli_update_status_reports_releases_and_run_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "release-a"
            managed_run = work_root / "runs" / "managed-run"
            unmanaged_run = work_root / "runs" / "unmanaged-run"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            release_root.mkdir(parents=True)
            (release_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "release_id": "release-a",
                        "release_root": str(release_root),
                        "source_root": str(tmp_path / "checkout"),
                    }
                ),
                encoding="utf-8",
            )
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "release-a", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            for run_dir, release_id in [(managed_run, "release-a"), (unmanaged_run, None)]:
                (run_dir / "state").mkdir(parents=True)
                state = {"scheduler_status": "running", "inflight_attempts": []}
                if release_id:
                    state["runtime_release_id"] = release_id
                    state["runtime_release_root"] = str(release_root)
                (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                    json.dumps(state),
                    encoding="utf-8",
                )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--status",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            summary = json.loads(status_completed.stdout)
            self.assertEqual(summary["update_status"], "status")
            self.assertEqual(summary["active_release"]["release_id"], "release-a")
            self.assertEqual(summary["known_releases"][0]["release_id"], "release-a")
            self.assertEqual(summary["runs_by_release"]["release-a"], ["managed-run"])
            self.assertEqual(summary["unmanaged_runs"], ["unmanaged-run"])

    def test_agentteam_cli_update_status_text_lists_release_ids_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            for release_id in ["release-a", "release-b"]:
                release_root = work_root / "releases" / release_id
                release_root.mkdir(parents=True)
                (release_root / "manifest.json").write_text(
                    json.dumps(
                        {
                            "release_id": release_id,
                            "release_root": str(release_root),
                            "source_root": str(tmp_path / "checkout" / release_id),
                        }
                    ),
                    encoding="utf-8",
                )
            (work_root / "releases" / "active.json").write_text(
                json.dumps(
                    {
                        "release_id": "release-b",
                        "release_root": str(work_root / "releases" / "release-b"),
                    }
                ),
                encoding="utf-8",
            )
            unmanaged_run = work_root / "runs" / "unmanaged-run"
            (unmanaged_run / "state").mkdir(parents=True)
            (unmanaged_run / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps({"scheduler_status": "running", "inflight_attempts": []}),
                encoding="utf-8",
            )

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--status",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            self.assertIn("active_release: release-b\n", status_completed.stdout)
            self.assertIn(
                "known_releases:\n  - release-a\n  - release-b\n",
                status_completed.stdout,
            )
            self.assertNotIn("active_release_root", status_completed.stdout)
            self.assertNotIn("unmanaged_runs", status_completed.stdout)
            self.assertNotIn(str(work_root), status_completed.stdout)

    def test_agentteam_cli_update_from_installs_and_activates_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            checkout = tmp_path / "checkout"
            work_root = tmp_path / "agentteam-work"
            existing_run = work_root / "runs" / "existing-run"
            _init_repo(repo)
            _init_repo(checkout)
            runtime_pkg = checkout / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            runtime_pkg.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# fixture runtime\n", encoding="utf-8")
            (checkout / "agentteam").write_text("#!/usr/bin/env python3\nprint('fixture')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture agentteam release"],
                cwd=checkout,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "update-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            (existing_run / "state").mkdir(parents=True)
            (existing_run / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "runtime_release_id": "old-release",
                        "runtime_release_root": str(work_root / "releases" / "old-release"),
                    }
                ),
                encoding="utf-8",
            )

            update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from",
                    str(checkout),
                    "--release-id",
                    "fixture-release",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(update_completed.returncode, 0, update_completed.stderr)
            summary = json.loads(update_completed.stdout)
            self.assertEqual(summary["update_status"], "installed")
            self.assertEqual(summary["active_release"]["release_id"], "fixture-release")
            release_root = Path(summary["active_release"]["release_root"])
            self.assertTrue((release_root / "manifest.json").exists())
            self.assertTrue((release_root / "agentteam").exists())
            self.assertTrue((release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime" / "__init__.py").exists())
            active = json.loads((work_root / "releases" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["release_id"], "fixture-release")
            existing_state = json.loads(
                (existing_run / "state" / "two_phase_scheduler_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(existing_state["runtime_release_id"], "old-release")

            text_update_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "update",
                    "--project-root",
                    str(repo),
                    "--from",
                    str(checkout),
                    "--release-id",
                    "fixture-release-text",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(text_update_completed.returncode, 0, text_update_completed.stderr)
            self.assertIn("update_status: installed\n", text_update_completed.stdout)
            self.assertIn("active_release: fixture-release-text\n", text_update_completed.stdout)
            self.assertIn("  - fixture-release-text\n", text_update_completed.stdout)
            self.assertNotIn("release_root", text_update_completed.stdout)
            self.assertNotIn(str(work_root), text_update_completed.stdout)

    def test_agentteam_cli_start_records_active_runtime_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "active-release"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "release-record-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            release_root.mkdir(parents=True)
            (release_root / "manifest.json").write_text(
                json.dumps({"release_id": "active-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "active-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )

            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Record active release on run.",
                    "--taskpack-id",
                    "release-record-run",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            run_state_dir = work_root / "runs" / "release-record-run" / "state"
            state_path = run_state_dir / "two_phase_scheduler_state.json"
            if not state_path.exists():
                state_path = run_state_dir / "scheduler_state.json"
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(state["runtime_release_id"], "active-release")
            self.assertEqual(state["runtime_release_root"], str(release_root))

    def test_agentteam_cli_help_lists_commands_and_command_details(self):
        help_completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", "help"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        stop_completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", "help", "stop"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(help_completed.returncode, 0, help_completed.stderr)
        self.assertIn("AgentTeam commands", help_completed.stdout)
        self.assertIn("init", help_completed.stdout)
        self.assertIn("start", help_completed.stdout)
        self.assertIn("status", help_completed.stdout)
        self.assertIn("watch", help_completed.stdout)
        self.assertIn("stop", help_completed.stdout)
        self.assertIn("taskpack", help_completed.stdout)
        self.assertIn("update", help_completed.stdout)
        self.assertIn("agentteam help <command>", help_completed.stdout)
        self.assertEqual(stop_completed.returncode, 0, stop_completed.stderr)
        self.assertIn("agentteam stop", stop_completed.stdout)
        self.assertIn("Stop or clean up an existing run", stop_completed.stdout)
        self.assertIn("agentteam stop --project-root <repo>", stop_completed.stdout)
        self.assertIn("--stale", stop_completed.stdout)

    def test_agentteam_cli_continue_runs_existing_frozen_taskpack_without_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "continue-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            start_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create frozen taskpack for continue.",
                    "--taskpack-id",
                    "cli-continue",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)
            shutil.rmtree(work_root / "drafts" / "cli-continue")

            continue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "continue",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "cli-continue",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(continue_completed.returncode, 0, continue_completed.stderr)
            summary = json.loads(continue_completed.stdout)
            self.assertEqual(summary["continue_status"], "continued")
            self.assertEqual(summary["taskpack_id"], "cli-continue")
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["run"]["scheduler_status"], "idle")

    def test_repo_root_agentteam_launcher_invokes_cli_help(self):
        launcher = Path(__file__).resolve().parents[4] / "agentteam"

        completed = subprocess.run(
            [str(launcher), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("AgentTeam operator CLI", completed.stdout)

    def test_repo_root_agentteam_launcher_dispatches_active_release_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            release_root = work_root / "releases" / "fixture-release"
            runtime_package = release_root / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            launcher = Path(__file__).resolve().parents[4] / "agentteam"
            repo.mkdir()
            (repo / ".agentteam").mkdir()
            (repo / ".agentteam" / "profile.json").write_text(
                json.dumps(
                    {
                        "profile_schema_version": "agentteam_profile.v1",
                        "project_key": "launcher-release",
                        "work_root": str(work_root),
                        "author_runtime": "fake",
                        "default_runtime": "fake",
                        "one_shot": True,
                        "max_inflight": 2,
                        "max_attempts": 1,
                        "commit_verified_integration": False,
                        "notification_project": "launcher-release",
                        "feishu": {"enabled": False, "webhook_env": None, "signing_secret_env": None},
                    }
                ),
                encoding="utf-8",
            )
            runtime_package.mkdir(parents=True)
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            (runtime_package / "agentteam.py").write_text(
                "def main(argv=None):\n    print('active release runtime marker')\n    return 0\n",
                encoding="utf-8",
            )
            (work_root / "releases").mkdir(parents=True, exist_ok=True)
            (work_root / "releases" / "active.json").write_text(
                json.dumps({"release_id": "fixture-release", "release_root": str(release_root)}),
                encoding="utf-8",
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)

            completed = subprocess.run(
                [str(launcher), "status", "--project-root", str(repo)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "active release runtime marker")

    def test_repo_root_agentteam_launcher_start_runs_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            launcher = Path(__file__).resolve().parents[4] / "agentteam"
            env = _test_env()
            env.pop("PYTHONPATH", None)
            _init_repo(repo)

            init_completed = subprocess.run(
                [
                    str(launcher),
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "launcher-start",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--one-shot",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)

            completed = subprocess.run(
                [
                    str(launcher),
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Run launcher start without PYTHONPATH.",
                    "--taskpack-id",
                    "launcher-start-fake",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "launcher-start-fake")
            self.assertEqual(summary["run"]["scheduler_status"], "idle")

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

    def test_agentteam_cli_freeze_after_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
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
                    "Freeze through CLI.",
                    "--draft-root",
                    str(drafts),
                    "--taskpack-id",
                    "cli-freeze",
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

            freeze_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "freeze",
                    str(drafts / "cli-freeze"),
                    "--frozen-root",
                    str(frozen_root),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(freeze_completed.returncode, 0, freeze_completed.stderr)
            freeze_summary = json.loads(freeze_completed.stdout)
            self.assertEqual(freeze_summary["manifest"]["taskpack_id"], "cli-freeze")
            self.assertTrue((frozen_root / "cli-freeze" / "manifest.json").exists())

    def test_agentteam_cli_run_fake_frozen_taskpack_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Run fake frozen taskpack through CLI.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="cli-run-fake",
            )
            taskpack_dir = Path(result["taskpack_dir"])
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["runtime"]["default_backend"] = "fake"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            agent_pool_path = taskpack_dir / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"]["adapter"] = "fake"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            backlog["items"][0]["write_scope"] = ["generated/"]
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")
            frozen = freeze_taskpack(taskpack_dir, frozen_root)

            run_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "run",
                    frozen["frozen_taskpack_dir"],
                    "--run-root",
                    str(run_root),
                    "--one-shot",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(run_completed.returncode, 0, run_completed.stderr)
            run_summary = json.loads(run_completed.stdout)
            self.assertEqual(run_summary["scheduler_status"], "idle")
            self.assertEqual(
                run_summary["snapshot"]["tasks"]["TASK-CLI_RUN_FAKE-001"]["task_status"],
                "done",
            )

    def test_agentteam_cli_run_forwards_child_failure_exit_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            wrapper_run_root = tmp_path / "wrapper-runs"
            direct_run_root = tmp_path / "direct-runs"
            _init_repo(repo)
            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Forward child runtime CLI failure.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="cli-child-failure",
            )
            taskpack_dir = Path(result["taskpack_dir"])
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["runtime"]["default_backend"] = "fake"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            agent_pool_path = taskpack_dir / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"]["adapter"] = "fake"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            frozen = freeze_taskpack(taskpack_dir, frozen_root)

            direct_args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=direct_run_root,
                max_inflight=0,
            )
            direct_completed = subprocess.run(
                ["python3", "-m", "agentteam_runtime.cli", *direct_args],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(direct_completed.returncode, 2, direct_completed.stderr)
            self.assertIn("--max-inflight must be at least 1", direct_completed.stderr)

            wrapper_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "run",
                    frozen["frozen_taskpack_dir"],
                    "--run-root",
                    str(wrapper_run_root),
                    "--max-inflight",
                    "0",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(wrapper_completed.returncode, direct_completed.returncode)
            self.assertEqual(wrapper_completed.stdout, direct_completed.stdout)
            self.assertEqual(wrapper_completed.stderr, direct_completed.stderr)

    def test_agentteam_cli_run_prelaunch_failure_returns_json_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing_taskpack = tmp_path / "missing"

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "run",
                    str(missing_taskpack),
                    "--run-root",
                    str(tmp_path / "runs"),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            error = json.loads(completed.stderr)
            self.assertEqual(error["status"], "error")
            self.assertIn("missing", error["error"])

    def test_agentteam_cli_failure_returns_json_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_taskpack = Path(tmp) / "missing"

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "validate",
                    str(missing_taskpack),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(completed.stdout, "")
            error = json.loads(completed.stderr)
            self.assertEqual(error["status"], "error")
            self.assertIn("missing", error["error"])

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

    def test_fake_taskpack_author_draft_can_be_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Improve fixture behavior.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="fake-freezable",
            )

            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            self.assertEqual(frozen["manifest"]["taskpack_id"], "fake-freezable")
            self.assertTrue((Path(frozen["frozen_taskpack_dir"]) / "manifest.json").exists())

    def test_taskpack_author_uses_unique_implicit_id_when_default_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            first = draft_taskpack_from_goal(
                project_root=repo,
                goal="优化现有比赛代码",
                draft_root=drafts,
                author_runtime="fake",
            )
            second = draft_taskpack_from_goal(
                project_root=repo,
                goal="优化现有比赛代码",
                draft_root=drafts,
                author_runtime="fake",
            )

            self.assertEqual(first["taskpack_id"], "taskpack")
            self.assertEqual(second["taskpack_id"], "taskpack-2")
            self.assertTrue((drafts / "taskpack").exists())
            self.assertTrue((drafts / "taskpack-2").exists())

    def test_taskpack_author_rejects_explicit_existing_taskpack_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            draft_taskpack_from_goal(
                project_root=repo,
                goal="Create explicit taskpack.",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="explicit-repeat",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Create explicit taskpack again.",
                    draft_root=drafts,
                    author_runtime="fake",
                    taskpack_id="explicit-repeat",
                )

            self.assertIn("already exists", str(raised.exception))

    def test_taskpack_author_rejects_unsupported_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            with self.assertRaises(TaskpackValidationError):
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="human",
                    taskpack_id="unsupported-author",
                )

    def test_codex_taskpack_author_default_command_allows_non_git_draft_root(self):
        self.assertEqual(_command_list(None), ["codex", "exec", "--skip-git-repo-check"])

    def test_codex_taskpack_author_canonicalizes_common_schema_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-aliases"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-aliases",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "Improve existing project.",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_id": "runtime-optimization-audit-001",
                                "title": "Audit and optimize runtime path",
                                "status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["README.md"],
                                "write_scope": ["src/runtime.py"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps({"command": ["python3", "-m", "unittest", "discover"]}),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            agent_pool = json.loads((taskpack_dir / "agent_pool.json").read_text(encoding="utf-8"))
            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            self.assertEqual(agent_pool["scheduler_agent_id"], "agent-scheduler")
            self.assertEqual(
                agent_pool["agents"][0]["inbox_path"],
                "mailboxes/implementation-worker-1/inbox.jsonl",
            )
            self.assertEqual(
                agent_pool["agents"][0]["outbox_path"],
                "mailboxes/implementation-worker-1/outbox.jsonl",
            )
            self.assertEqual(backlog["items"][0]["task_id"], "runtime-optimization-audit-001")
            self.assertEqual(backlog["items"][0]["objective"], "Audit and optimize runtime path")
            self.assertEqual(backlog["items"][0]["backlog_status"], "ready")
            self.assertEqual(backlog["items"][0]["blockers"], [])

    def test_codex_taskpack_author_uses_project_venv_for_python_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "venv-command"
            _init_repo(repo)
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "venv-command",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "Use the project test environment.",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "mailboxes/implementation-worker-1/inbox.jsonl",
                                "outbox_path": "mailboxes/implementation-worker-1/outbox.jsonl",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": "TASK-VENV-001",
                                "objective": "Use project venv.",
                                "backlog_status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["gesture_recognition/tests"],
                                "write_scope": ["gesture_recognition/"],
                                "blockers": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps(
                    {
                        "command": [
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "gesture_recognition/tests",
                            "-v",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(
                verification["command"],
                [
                    str(venv_python.resolve()),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "gesture_recognition/tests",
                    "-v",
                ],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_preserves_project_venv_symlink_for_python_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "venv-symlink-command"
            _init_repo(repo)
            venv_python = repo / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(Path(sys.executable))
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "venv-symlink-command",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "Use the project venv symlink.",
                        "runtime": {"default_backend": "codex"},
                        "files": {
                            "agent_pool": "agent_pool.json",
                            "backlog": "backlog.json",
                            "verification": "verification.json",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps(
                    {
                        "scheduler_agent_id": "agent-scheduler",
                        "agents": [
                            {
                                "agent_id": "implementation-worker-1",
                                "role": "implementation_worker",
                                "status": "idle",
                                "inbox_path": "mailboxes/implementation-worker-1/inbox.jsonl",
                                "outbox_path": "mailboxes/implementation-worker-1/outbox.jsonl",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": "TASK-VENV-SYMLINK-001",
                                "objective": "Use project venv symlink.",
                                "backlog_status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["gesture_recognition/tests"],
                                "write_scope": ["gesture_recognition/"],
                                "blockers": [],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps(
                    {
                        "command": [
                            "/usr/bin/python3.12",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "gesture_recognition/tests",
                            "-v",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(
                verification["command"],
                [
                    str(venv_python),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "gesture_recognition/tests",
                    "-v",
                ],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_normalizes_system_python_verification_without_project_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Normalize system Python verification.",
                draft_root=drafts,
                taskpack_id="system-python-command",
                read_scope=["README.md"],
                write_scope=["README.md"],
                verification_command=["/usr/bin/python3.12", "-m", "unittest", "discover"],
            )
            taskpack_dir = Path(result["taskpack_dir"])

            _canonicalize_codex_taskpack_files(taskpack_dir)

            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(
                verification["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")

    def test_codex_taskpack_author_rejects_dirty_repo_before_running_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_codex_author.py"
            marker = tmp_path / "codex-ran.marker"
            _init_repo(repo)
            (repo / "untracked.txt").write_text("preexisting untracked file\n", encoding="utf-8")
            fake_codex.write_text(
                "\n".join(
                    [
                        "import pathlib",
                        f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="dirty-codex-author",
                    codex_command=["python3", str(fake_codex)],
                    codex_timeout_seconds=5,
                )

            self.assertIn("clean", str(raised.exception))
            self.assertFalse(marker.exists())

    def test_codex_taskpack_author_reports_target_repo_change_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_timeout_author.py"
            changed_file = "codex-timeout-side-effect.txt"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import pathlib",
                        "import sys",
                        "import time",
                        "repo = pathlib.Path(sys.argv[1])",
                        f"(repo / {changed_file!r}).write_text('changed\\n', encoding='utf-8')",
                        "time.sleep(10)",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="timeout-side-effect",
                    codex_command=["python3", str(fake_codex), str(repo)],
                    codex_timeout_seconds=1,
                )

            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIn("modified the target repository", str(raised.exception))
            self.assertIn(changed_file, status.stdout)

    def test_codex_taskpack_author_rejects_committed_target_repo_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            taskpack_dir = drafts / "committed-side-effect"
            fake_codex = tmp_path / "fake_committing_author.py"
            changed_file = "codex-committed-side-effect.txt"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import subprocess",
                        "import sys",
                        "repo = pathlib.Path(sys.argv[1]).resolve()",
                        "taskpack_dir = pathlib.Path(sys.argv[2]).resolve()",
                        f"changed_file = {changed_file!r}",
                        "(repo / changed_file).write_text('changed\\n', encoding='utf-8')",
                        "subprocess.run(['git', 'add', changed_file], cwd=repo, check=True)",
                        (
                            "subprocess.run(['git', 'commit', '-m', 'codex side effect'], "
                            "cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)"
                        ),
                        "taskpack_id = taskpack_dir.name",
                        "task_id = 'TASK-COMMITTED-SIDE-EFFECT-001'",
                        "taskpack = {",
                        "    'taskpack_schema_version': 'taskpack.v1',",
                        "    'taskpack_id': taskpack_id,",
                        "    'status': 'draft',",
                        "    'project_root': str(repo),",
                        "    'goal': 'Improve fixture behavior.',",
                        "    'runtime': {'default_backend': 'codex'},",
                        (
                            "    'policy': {'allow_merge': False, "
                            "'merge_requires_verified_integration': True},"
                        ),
                        (
                            "    'files': {'agent_pool': 'agent_pool.json', "
                            "'backlog': 'backlog.json', 'verification': 'verification.json'},"
                        ),
                        "}",
                        "agent_pool = {",
                        "    'scheduler_agent_id': 'agent-scheduler',",
                        "    'role_runtime_profiles': {'implementation_worker': {'adapter': 'codex'}},",
                        "    'agents': [{",
                        "        'agent_id': 'agent-implementation-worker-1',",
                        "        'role': 'implementation_worker',",
                        "        'status': 'idle',",
                        "        'inbox_path': 'mailboxes/agent-implementation-worker-1/inbox.jsonl',",
                        "    }],",
                        "}",
                        "backlog = {'backlog_id': 'BL-committed-side-effect', 'items': [{",
                        "    'task_id': task_id,",
                        "    'milestone_id': 'TASKPACK-M0',",
                        "    'objective': 'Improve fixture behavior.',",
                        "    'backlog_status': 'ready',",
                        "    'risk_target': 'L1',",
                        "    'depends_on': [],",
                        "    'read_scope': ['.'],",
                        "    'write_scope': ['src/'],",
                        "    'required_role': 'implementation_worker',",
                        "    'blockers': [],",
                        "}]}",
                        (
                            "verification = {'verification_schema_version': "
                            "'taskpack_verification.v1', 'command': ['python3', '-m', "
                            "'unittest', 'discover'], 'success_criteria': ['tests pass']}"
                        ),
                        "for name, payload in [",
                        "    ('taskpack.yaml', taskpack),",
                        "    ('agent_pool.json', agent_pool),",
                        "    ('backlog.json', backlog),",
                        "    ('verification.json', verification),",
                        "]:",
                        (
                            "    (taskpack_dir / name).write_text(json.dumps(payload), "
                            "encoding='utf-8')"
                        ),
                        "(taskpack_dir / 'README.md').write_text('# committed-side-effect\\n', encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="committed-side-effect",
                    codex_command=["python3", str(fake_codex), str(repo), str(taskpack_dir)],
                    codex_timeout_seconds=5,
                )

            self.assertIn("modified the target repository", str(raised.exception))

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

    def test_validate_taskpack_rejects_taskpack_runtime_profile_launch_commands(self):
        cases = [
            (
                "role-shell-profile",
                lambda agent_pool: agent_pool["role_runtime_profiles"].__setitem__(
                    "implementation_worker",
                    {"adapter": "shell", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
            (
                "role-codex-command-profile",
                lambda agent_pool: agent_pool["role_runtime_profiles"].__setitem__(
                    "implementation_worker",
                    {"adapter": "codex", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
            (
                "agent-shell-profile",
                lambda agent_pool: agent_pool["agents"][0].__setitem__(
                    "runtime_profile",
                    {"adapter": "shell", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
            (
                "agent-codex-command-profile",
                lambda agent_pool: agent_pool["agents"][0].__setitem__(
                    "runtime_profile",
                    {"adapter": "codex", "command": ["bash", "-lc", "echo unsafe"]},
                ),
            ),
        ]
        for taskpack_id, mutate_agent_pool in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject launch command injection.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    agent_pool_path = Path(result["taskpack_dir"]) / "agent_pool.json"
                    agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
                    mutate_agent_pool(agent_pool)
                    agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    message = str(raised.exception)
                    self.assertTrue("adapter" in message or "command" in message)

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

            self.assertEqual(
                args[0:2],
                ["--agent-pool", str(Path(frozen["frozen_taskpack_dir"]) / "agent_pool.json")],
            )
            self.assertEqual(_arg_value(args, "--runtime"), "codex")
            self.assertEqual(
                json.loads(_arg_value(args, "--integration-verification-command-json")),
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertIn("--daemon-run-until-idle", args)
            self.assertIn("--daemon-two-phase-worker-pool", args)
            self.assertEqual(_arg_value(args, "--max-steps"), "45000")
            self.assertEqual(_arg_value(args, "--codex-timeout-seconds"), "1800")
            self.assertEqual(_arg_value(args, "--lease-timeout-seconds"), "1860")
            self.assertIn("--integrate-accepted-patch", args)
            self.assertNotIn("--commit-verified-integration", args)
            self.assertTrue((run_root / "runtime-args").exists())

    def test_build_taskpack_runtime_args_supports_one_shot_and_commit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build one-shot runtime args.",
                draft_root=drafts,
                taskpack_id="one-shot-runtime-args",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                daemon=False,
                commit_verified_integration=True,
            )

            self.assertIn("--run-until-idle", args)
            self.assertNotIn("--daemon-run-until-idle", args)
            self.assertNotIn("--daemon-two-phase-worker-pool", args)
            self.assertIn("--commit-verified-integration", args)
            self.assertEqual(_arg_value(args, "--runtime"), "codex")

    def test_build_taskpack_runtime_args_rejects_draft_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject draft runtime launch.",
                draft_root=drafts,
                taskpack_id="draft-runtime-args",
                write_scope=["src/"],
            )

            with self.assertRaises(TaskpackValidationError):
                build_taskpack_runtime_args(result["taskpack_dir"], run_root=run_root)

            self.assertFalse((run_root / "draft-runtime-args").exists())

    def test_build_taskpack_runtime_args_honors_mapped_companion_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Use mapped runtime companion files.",
                draft_root=drafts,
                taskpack_id="mapped-runtime-args",
                write_scope=["src/"],
            )
            taskpack_dir = Path(result["taskpack_dir"])
            nested_dir = taskpack_dir / "nested"
            nested_dir.mkdir()
            (taskpack_dir / "agent_pool.json").replace(nested_dir / "agent_pool.json")
            (taskpack_dir / "backlog.json").replace(nested_dir / "backlog.json")
            taskpack_path = taskpack_dir / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["files"]["agent_pool"] = "nested/agent_pool.json"
            taskpack["files"]["backlog"] = "nested/backlog.json"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])

            args = build_taskpack_runtime_args(frozen_dir, run_root=run_root)

            self.assertEqual(_arg_value(args, "--agent-pool"), str(frozen_dir / "nested" / "agent_pool.json"))
            self.assertEqual(_arg_value(args, "--backlog"), str(frozen_dir / "nested" / "backlog.json"))

    def test_build_taskpack_runtime_args_defaults_missing_files_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Use default runtime companion files.",
                draft_root=drafts,
                taskpack_id="default-runtime-files",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            del taskpack["files"]
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])

            args = build_taskpack_runtime_args(frozen_dir, run_root=run_root)

            self.assertEqual(_arg_value(args, "--agent-pool"), str(frozen_dir / "agent_pool.json"))
            self.assertEqual(_arg_value(args, "--backlog"), str(frozen_dir / "backlog.json"))
            self.assertTrue((run_root / "default-runtime-files").exists())

    def test_validate_taskpack_rejects_invalid_runtime_metadata(self):
        cases = [
            ("non-object-validate-runtime", []),
            ("missing-validate-runtime-backend", {}),
            ("unknown-validate-runtime-backend", {"default_backend": "unknown"}),
            ("shell-validate-runtime-backend", {"default_backend": "shell"}),
        ]
        for taskpack_id, runtime in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    frozen_root = tmp_path / "frozen"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject invalid runtime metadata.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    taskpack["runtime"] = runtime
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        validate_taskpack(result["taskpack_dir"])

                    self.assertIn("runtime", str(raised.exception))
                    with self.assertRaises(TaskpackValidationError):
                        freeze_taskpack(result["taskpack_dir"], frozen_root)
                    self.assertFalse((frozen_root / taskpack_id).exists())

    def test_build_taskpack_runtime_args_rejects_invalid_runtime_without_run_dir(self):
        cases = [
            ("non-object-runtime", []),
            ("missing-runtime-backend", {}),
            ("unknown-runtime-backend", {"default_backend": "unknown"}),
        ]
        for taskpack_id, runtime in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    frozen_root = tmp_path / "frozen"
                    run_root = tmp_path / "runs"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject invalid runtime metadata.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
                    taskpack_path = Path(frozen["frozen_taskpack_dir"]) / "taskpack.yaml"
                    taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
                    taskpack["runtime"] = runtime
                    taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError) as raised:
                        build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

                    self.assertIn("runtime", str(raised.exception))
                    self.assertFalse((run_root / taskpack_id).exists())

    def test_build_taskpack_runtime_args_rejects_shell_backend_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject shell runtime launch.",
                draft_root=drafts,
                taskpack_id="shell-runtime-backend",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            taskpack_path = Path(frozen["frozen_taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["runtime"]["default_backend"] = "shell"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertIn("runtime", str(raised.exception))
            self.assertFalse((run_root / "shell-runtime-backend").exists())

    def test_build_taskpack_runtime_args_rejects_tampered_runtime_profile_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject tampered frozen runtime profile.",
                draft_root=drafts,
                taskpack_id="tampered-runtime-profile",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            frozen_dir = Path(frozen["frozen_taskpack_dir"])
            agent_pool_path = frozen_dir / "agent_pool.json"
            agent_pool = json.loads(agent_pool_path.read_text(encoding="utf-8"))
            agent_pool["role_runtime_profiles"]["implementation_worker"] = {
                "adapter": "shell",
                "command": ["bash", "-lc", "echo unsafe"],
            }
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                build_taskpack_runtime_args(frozen_dir, run_root=run_root)

            message = str(raised.exception)
            self.assertTrue("adapter" in message or "command" in message)
            self.assertFalse((run_root / "tampered-runtime-profile").exists())

    def test_build_taskpack_runtime_args_rejects_invalid_launch_metadata_without_run_dir(self):
        cases = [
            ("missing-project-root", "taskpack.yaml", lambda value: value.pop("project_root")),
            ("missing-verification-command", "verification.json", lambda value: value.pop("command")),
        ]
        for taskpack_id, artifact_name, mutate in cases:
            with self.subTest(taskpack_id=taskpack_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    drafts = tmp_path / "drafts"
                    frozen_root = tmp_path / "frozen"
                    run_root = tmp_path / "runs"
                    _init_repo(repo)
                    result = draft_taskpack_files(
                        project_root=repo,
                        goal="Reject invalid launch metadata.",
                        draft_root=drafts,
                        taskpack_id=taskpack_id,
                        write_scope=["src/"],
                    )
                    frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
                    artifact_path = Path(frozen["frozen_taskpack_dir"]) / artifact_name
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                    mutate(artifact)
                    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

                    with self.assertRaises(TaskpackValidationError):
                        build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

                    self.assertFalse((run_root / taskpack_id).exists())

try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class CliMixin:
    def test_agentteam_cli_db_rebuild_and_check_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "projection-cli")
            _write_completed_operator_run(work_root / "runs" / "projection-run")
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection CLI fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            rebuild_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "db",
                    "rebuild",
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
            check_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "db",
                    "check",
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

            self.assertEqual(rebuild_completed.returncode, 0, rebuild_completed.stderr)
            self.assertEqual(check_completed.returncode, 0, check_completed.stderr)
            rebuild_summary = json.loads(rebuild_completed.stdout)
            check_summary = json.loads(check_completed.stdout)
            self.assertEqual(rebuild_summary["db_status"], "rebuilt")
            self.assertEqual(rebuild_summary["project"], "projection-cli")
            self.assertEqual(rebuild_summary["runs"], 1)
            self.assertEqual(check_summary["check_status"], "passed")
            self.assertEqual(check_summary["db_path"], rebuild_summary["db_path"])


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
            controller_roots = list(
                (
                    run_dir
                    / "state"
                    / "controller_invocations"
                    / "runtime_diagnostic"
                ).glob("DIAGNOSTIC-SESSION-*")
            )
            self.assertEqual(len(controller_roots), 1)
            claim = json.loads(
                (controller_roots[0] / "controller_claim.json").read_text(
                    encoding="utf-8"
                )
            )
            invocation_dirs = list(
                (controller_roots[0] / "model_invocations").glob("INV-*")
            )
            self.assertEqual(len(invocation_dirs), 1)
            started = json.loads(
                (invocation_dirs[0] / "started.json").read_text(
                    encoding="utf-8"
                )
            )
            terminal = json.loads(
                (invocation_dirs[0] / "terminal.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(claim["usage_stage"], "runtime_diagnostic")
            self.assertEqual(started["usage_stage"], "runtime_diagnostic")
            self.assertEqual(
                started["runtime_execution_session_id"],
                claim["runtime_execution_session_id"],
            )
            self.assertEqual(
                terminal["terminal_writer"],
                "runtime_diagnostic_controller",
            )


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


    def test_agentteam_cli_init_writes_project_verification_profile(self):
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
                    "benchmark-project",
                    "--work-root",
                    str(work_root),
                    "--verification-command-json",
                    json.dumps(["python3", "tools/check.py"]),
                    "--performance-command-json",
                    json.dumps(["python3", "tools/bench.py", "--json"]),
                    "--metric",
                    "accuracy",
                    "--metric",
                    "latency_ms",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            profile = json.loads((repo / ".agentteam" / "profile.json").read_text(encoding="utf-8"))
            verification_profile = profile["verification_profile"]
            self.assertEqual(
                verification_profile["correctness"]["command"],
                ["python3", "tools/check.py"],
            )
            self.assertEqual(
                verification_profile["performance"]["command"],
                ["python3", "tools/bench.py", "--json"],
            )
            self.assertEqual(verification_profile["performance"]["metrics"], ["accuracy", "latency_ms"])


    def test_agentteam_cli_init_infers_native_runtime_verification_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            runtime_root = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime"
            (runtime_root / "agentteam_runtime").mkdir(parents=True)
            (runtime_root / "agentteam_runtime" / "__init__.py").write_text("", encoding="utf-8")
            (runtime_root / "tests").mkdir()
            (runtime_root / "tests" / "test_taskpack.py").write_text("", encoding="utf-8")
            (runtime_root / "tests" / "test_m0_runtime.py").write_text("", encoding="utf-8")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "agentteam-native",
                    "--work-root",
                    str(work_root),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            profile = json.loads((repo / ".agentteam" / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(
                profile["verification_profile"]["correctness"]["command"],
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                ],
            )


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
                    "--verification-command-json",
                    json.dumps(["python3", "-c", "print('profile-check')"]),
                    "--performance-command-json",
                    json.dumps(["python3", "tools/bench.py", "--json"]),
                    "--metric",
                    "latency_ms",
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
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "cli-start-profile")
            self.assertEqual(summary["runtime"], "fake")
            self.assertEqual(summary["profile"]["profile_path"], str((repo / ".agentteam" / "profile.json").resolve()))
            self.assertEqual(summary["paths"]["work_root"], str(work_root.resolve()))
            self.assertTrue((work_root / "drafts" / "cli-start-profile").exists())
            loaded = load_taskpack(work_root / "frozen" / "cli-start-profile")
            self.assertEqual(loaded["verification"]["command"], ["python3", "-c", "print('profile-check')"])
            self.assertEqual(loaded["verification"]["performance"]["metrics"], ["latency_ms"])
            self.assertEqual(summary["run"]["scheduler_status"], "idle")
            baseline_worktree = work_root / "runs" / "cli-start-profile" / "integration-baseline"
            self.assertTrue(baseline_worktree.exists())
            baseline_ref = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "rev-parse",
                    "agentteam/run/cli-start-profile/integration",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(baseline_ref.returncode, 0, baseline_ref.stderr)
            repo_status = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(repo_status.returncode, 0, repo_status.stderr)
            self.assertEqual(repo_status.stdout, "")


    def test_agentteam_cli_integrate_fast_forwards_verified_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "integrate-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for integration.",
                    "--taskpack-id",
                    "integrate-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            baseline_worktree = work_root / "runs" / "integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nintegrated\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam integration fixture"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            baseline_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "integrate-run",
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
            self.assertEqual(summary["integrate_status"], "merged")
            self.assertEqual(summary["merge_status"], "fast_forward")
            self.assertEqual(summary["after_head"], baseline_head)
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# fixture\n\nintegrated\n")


    def test_agentteam_cli_integrate_record_only_marks_baseline_acknowledged_without_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "record-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a run for record-only integration.",
                "record-integrate-run",
            )
            baseline_worktree = work_root / "runs" / "record-integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nmanual-only\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam record-only fixture"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "record-integrate-run",
                    "--record-only",
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
            self.assertEqual(summary["integrate_status"], "acknowledged")
            self.assertEqual(summary["merge_status"], "record_only")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# fixture\n")
            state = json.loads(
                (
                    work_root
                    / "runs"
                    / "record-integrate-run"
                    / "state"
                    / "two_phase_scheduler_state.json"
                ).read_text(encoding="utf-8")
            )
            baseline = state["integration_baseline"]
            self.assertEqual(baseline["integration_baseline_status"], "acknowledged")
            self.assertEqual(baseline["integration_acknowledged_by"], "operator")
            self.assertTrue(baseline["integration_acknowledged_head_sha"])

            status_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(work_root / "runs" / "record-integrate-run"),
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(status_completed.returncode, 0, status_completed.stderr)
            status = json.loads(status_completed.stdout)
            self.assertEqual(status["integration_baseline"]["status"], "acknowledged")
            self.assertNotIn("agentteam integrate", status.get("next_action") or "")
            self.assertNotIn("review integration baseline", status.get("next_action") or "")


    def test_agentteam_cli_integrate_rebases_diverged_baseline_before_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "rebase-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a run for rebase integration.",
                "rebase-integrate-run",
            )
            baseline_worktree = work_root / "runs" / "rebase-integrate-run" / "integration-baseline"
            (baseline_worktree / "agentteam-result.txt").write_text("baseline change\n", encoding="utf-8")
            subprocess.run(["git", "add", "agentteam-result.txt"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam baseline change"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            (repo / "main-change.txt").write_text("main branch change\n", encoding="utf-8")
            subprocess.run(["git", "add", "main-change.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "main branch advanced"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "rebase-integrate-run",
                    "--rebase",
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
            self.assertEqual(summary["integrate_status"], "merged")
            self.assertEqual(summary["rebase_status"], "rebased")
            self.assertEqual(summary["merge_status"], "rebased_fast_forward")
            self.assertEqual((repo / "main-change.txt").read_text(encoding="utf-8"), "main branch change\n")
            self.assertEqual((repo / "agentteam-result.txt").read_text(encoding="utf-8"), "baseline change\n")
            self.assertEqual(summary["after_head"], summary["integration_baseline"]["head_sha"])


    def test_agentteam_cli_integrate_requires_clean_target_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "dirty-integrate-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Create a run for dirty integration.",
                    "--taskpack-id",
                    "dirty-integrate-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            baseline_worktree = work_root / "runs" / "dirty-integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nintegrated\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam dirty integration fixture"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            before_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            (repo / "local.txt").write_text("uncommitted\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "dirty-integrate-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 1)
            error = json.loads(completed.stderr)
            self.assertEqual(error["error"], "target repository must be clean before integrate")
            after_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(after_head, before_head)


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
            self.assertIn("status: completed\n", completed.stdout)
            self.assertIn("taskpack_id: cli-start-progress\n", completed.stdout)
            self.assertIn(
                "work_report: changed=Worker did not provide a natural-language change summary.",
                completed.stdout,
            )
            self.assertIn("integration=blocked", completed.stdout)
            self.assertIn(
                "recommendation: merge=Do not merge until integration passes.",
                completed.stdout,
            )
            self.assertIn("report:", completed.stdout)
            self.assertNotIn('"draft"', completed.stdout)
            self.assertLessEqual(len([line for line in completed.stdout.splitlines() if line.strip()]), 12)
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
            self.assertIn("overall_status: idle", status_completed.stdout)
            self.assertIn("run_status: idle", status_completed.stdout)
            self.assertIn("tasks: 2 done, 0 blocked", status_completed.stdout)
            self.assertIn("inflight: 0", status_completed.stdout)
            self.assertIn("manual_gates: 0", status_completed.stdout)
            self.assertIn("projection_source: files", status_completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", status_completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", status_completed.stdout)
            self.assertIn(str((work_root / "runs" / "cli-status-run").resolve()), status_completed.stdout)


    def test_agentteam_cli_status_prioritizes_running_guidance_before_integration_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-running-baseline"
            baseline_worktree = run_dir / "integration-baseline"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-running-project")
            baseline_worktree.mkdir(parents=True)
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "running",
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": "agentteam/run/status-running-baseline/integration",
                        "integration_baseline_worktree_path": str(baseline_worktree.resolve()),
                        "integration_baseline_head_sha": "abc123",
                    },
                    "inflight_attempts": [
                        {
                            "task_id": "optimize-pipeline",
                            "attempt_id": "ATTEMPT-001",
                            "agent_id": "implementation-worker-1",
                        }
                    ],
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
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
            self.assertEqual(summary["run_status"], "running")
            self.assertIn("agentteam watch --taskpack status-running-baseline", summary["next_action"])
            self.assertNotIn("review integration baseline", summary["next_action"])
            self.assertNotIn("agentteam integrate", summary["next_action"])
            self.assertIn("run is still active", summary["operator_hint"])


    def test_agentteam_cli_logs_tails_latest_run_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "1",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("run: logs-run", completed.stdout)
            self.assertIn("projection_source: files", completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", completed.stdout)
            self.assertIn("EVT-0001 run_completed", completed.stdout)


    def test_agentteam_cli_explain_status_describes_idle_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "explain-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "explain-project")
            _write_completed_operator_run(run_dir)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "explain-status",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("overall_status: idle", completed.stdout)
            self.assertIn(
                "Explanation: no worker or authoring process is currently active.",
                completed.stdout,
            )


    def test_agentteam_cli_status_treats_stopped_stale_inflight_as_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stopped-stale-inflight-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-project")
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "stopped",
                    "previous_scheduler_status": "running",
                    "stop_mode": "stale_cleanup",
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
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {
                    "registry_status": "stopped",
                    "stop_mode": "stale_cleanup",
                    "workers": [
                        {
                            "worker_agent_id": "implementation-worker-1",
                            "worker_status": "stopped",
                            "stopped_by": "stale_pid",
                        }
                    ],
                },
            )

            json_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
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
                    "--run-dir",
                    str(run_dir),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
            summary = json.loads(json_completed.stdout)
            self.assertEqual(summary["overall_status"], "stopped")
            self.assertEqual(summary["run_status"], "stopped")
            self.assertEqual(summary["liveness_status"], "stopped")
            self.assertEqual(summary["inflight"]["total"], 0)
            self.assertEqual(summary["inactive_inflight"]["total"], 1)
            self.assertNotIn("agentteam watch", summary.get("next_action", ""))
            self.assertNotIn("run is still active", summary.get("operator_hint", ""))

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("overall_status: stopped", text_completed.stdout)
            self.assertIn("run_status: stopped", text_completed.stdout)
            self.assertIn("liveness: stopped", text_completed.stdout)
            self.assertIn("inflight: 0", text_completed.stdout)
            self.assertIn("inactive_inflight: 1", text_completed.stdout)
            self.assertNotIn("agentteam watch", text_completed.stdout)
            self.assertNotIn("run is still active", text_completed.stdout)


    def test_agentteam_cli_status_treats_stop_requested_inflight_as_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "stop-requested-inflight-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-project")
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "stop_requested",
                    "previous_scheduler_status": "waiting",
                    "stop_mode": "stop",
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
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {
                    "registry_status": "stop_requested",
                    "stop_mode": "stop",
                    "workers": [
                        {
                            "worker_agent_id": "implementation-worker-1",
                            "worker_status": "stop_requested",
                            "stopped_by": "terminate_requested",
                        }
                    ],
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
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
            self.assertEqual(summary["run_status"], "stop_requested")
            self.assertEqual(summary["liveness_status"], "stop_requested")
            self.assertEqual(summary["inflight"]["total"], 0)
            self.assertEqual(summary["inactive_inflight"]["total"], 1)
            self.assertNotIn("agentteam watch", summary.get("next_action", ""))


    def test_agentteam_cli_stop_explicit_run_dir_does_not_require_project_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "standalone-run"
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "inflight_attempts": [],
                    "steps": [],
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {"registry_status": "idle", "workers": []},
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stop",
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=tmp_path,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["stop_status"], "stopped")
            self.assertEqual(summary["project"], "unknown")
            self.assertEqual(Path(summary["run_dir"]), run_dir.resolve())


    def test_agentteam_cli_status_prefers_active_worker_for_last_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "active-worker-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-project")
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "running",
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
                            "agent_id": "implementation-worker-active",
                        }
                    ],
                    "steps": [],
                },
            )
            _write_json(
                run_dir / "state" / "worker_process_registry.json",
                {
                    "registry_status": "running",
                    "workers": [
                        {
                            "worker_agent_id": "implementation-worker-active",
                            "worker_status": "running",
                            "worker_diagnostic_state": "processing",
                            "last_activity": "processing",
                            "heartbeat_task_id": "optimize-pipeline",
                            "heartbeat_progress_summary": "reading target files",
                        },
                        {
                            "worker_agent_id": "implementation-worker-old",
                            "worker_status": "stopped",
                            "exit_code": 0,
                        },
                    ],
                },
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertIn("implementation-worker-active running", summary["last_worker"])
            self.assertIn("progress=reading target files", summary["last_worker"])


    def test_agentteam_cli_status_run_dir_does_not_require_project_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = _write_completed_operator_run(tmp_path / "work" / "runs" / "profileless-run")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=tmp_path,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["latest_run"], "profileless-run")
            self.assertEqual(summary["project"], "unknown")
            self.assertEqual(summary["run_dir"], str(run_dir.resolve()))


    def test_agentteam_cli_status_run_dir_uses_run_dir_work_root_over_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            profile_work_root = tmp_path / "profile-work"
            explicit_work_root = tmp_path / "explicit-work"
            run_dir = _write_completed_operator_run(explicit_work_root / "runs" / "explicit-run")
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, profile_work_root, "profile-project")
            _write_json(
                explicit_work_root / "pursue" / "explicit-run-goal-memory.json",
                {
                    "memory_schema_version": "goal_memory.v1",
                    "latest_taskpack_id": "explicit-run",
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "status",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
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
            self.assertEqual(summary["project"], "profile-project")
            self.assertEqual(summary["latest_run"], "explicit-run")
            self.assertEqual(summary.get("projection_status"), "missing")
            self.assertEqual(
                summary.get("projection_db_path"),
                str((explicit_work_root / "agentteam.db").resolve()),
            )
            self.assertNotEqual(
                summary.get("projection_db_path"),
                str((profile_work_root / "agentteam.db").resolve()),
            )
            self.assertIn(
                "agentteam report --taskpack explicit-run",
                summary.get("next_action") or "",
            )


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
            self.assertIn("projection_source: files", list_completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", list_completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", list_completed.stdout)
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
        self.assertIn("next", help_completed.stdout)
        self.assertIn("status", help_completed.stdout)
        self.assertIn("paths", help_completed.stdout)
        self.assertIn("integrate", help_completed.stdout)
        self.assertIn("notify", help_completed.stdout)
        self.assertIn("watch", help_completed.stdout)
        self.assertIn("stop", help_completed.stdout)
        self.assertIn("taskpack", help_completed.stdout)
        self.assertIn("update", help_completed.stdout)
        self.assertIn("pursue", help_completed.stdout)
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
                    "--json",
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


    def test_agentteam_cli_next_creates_followup_taskpack_from_completed_run(self):
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
                    "next-project",
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
            first_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Initial optimization pass.",
                    "--taskpack-id",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(first_completed.returncode, 0, first_completed.stderr)

            next_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "next",
                    "--project-root",
                    str(repo),
                    "--from-taskpack",
                    "first-pass",
                    "--goal",
                    "Plan and implement the next optimization step.",
                    "--taskpack-id",
                    "second-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(next_completed.returncode, 0, next_completed.stderr)
            summary = json.loads(next_completed.stdout)
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["taskpack_id"], "second-pass")
            self.assertEqual(summary["follow_up"]["source_taskpack_id"], "first-pass")
            self.assertTrue(Path(summary["follow_up"]["source_report_path"]).exists())
            drafted = json.loads((work_root / "drafts" / "second-pass" / "taskpack.yaml").read_text(encoding="utf-8"))
            self.assertIn("Follow-up goal:", drafted["goal"])
            self.assertIn("Plan and implement the next optimization step.", drafted["goal"])
            self.assertIn("Previous taskpack context:", drafted["goal"])
            self.assertIn("source_taskpack_id: first-pass", drafted["goal"])
            self.assertIn("final_report.md", drafted["goal"])


    def test_agentteam_cli_next_reuses_repo_map_handoff_from_previous_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            baseline = tmp_path / "integration-baseline"
            _init_repo(repo)
            profile = build_project_profile(
                repo,
                project_key="next-reuse-project",
                work_root=work_root,
                author_runtime="fake",
                default_runtime="fake",
                one_shot=True,
            )
            write_project_profile(repo, profile, force=True)
            _write_json(
                baseline / taskpack_module.REPO_MAP_HANDOFF_PATH,
                {"schema_version": "repo_map_handoff.v1"},
            )
            _write_json(
                work_root / "runs" / "first-pass" / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "integration_baseline": {
                        "integration_baseline_branch": "agentteam/run/first-pass/integration",
                        "integration_baseline_worktree_path": str(baseline),
                        "integration_baseline_head_sha": "abc123",
                    },
                },
            )

            next_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "next",
                    "--project-root",
                    str(repo),
                    "--from-taskpack",
                    "first-pass",
                    "--goal",
                    "Implement the next optimization step using prior context.",
                    "--taskpack-id",
                    "second-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(next_completed.returncode, 0, next_completed.stderr)
            summary = json.loads(next_completed.stdout)
            self.assertEqual(
                summary["follow_up"]["repo_map_handoff_reuse"],
                taskpack_module.REPO_MAP_HANDOFF_PATH,
            )
            self.assertEqual(summary["repo_map_handoff_reuse"]["status"], "applied")
            loaded = load_taskpack(work_root / "drafts" / "second-pass")
            items = loaded["backlog"]["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["required_role"], "implementation_worker")
            self.assertEqual(items[0]["depends_on"], [])
            self.assertEqual(items[0]["input_artifacts"], [taskpack_module.REPO_MAP_HANDOFF_PATH])


    def test_agentteam_cli_next_default_output_is_concise(self):
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
                    "next-project",
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
            first_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Initial optimization pass.",
                    "--taskpack-id",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(first_completed.returncode, 0, first_completed.stderr)

            next_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "next",
                    "--project-root",
                    str(repo),
                    "--from-taskpack",
                    "first-pass",
                    "--goal",
                    "Plan and implement the next optimization step.",
                    "--taskpack-id",
                    "second-pass",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(next_completed.returncode, 0, next_completed.stderr)
            self.assertIn("status: completed\n", next_completed.stdout)
            self.assertIn("taskpack_id: second-pass\n", next_completed.stdout)
            self.assertIn("source_taskpack_id: first-pass\n", next_completed.stdout)
            self.assertIn("report:", next_completed.stdout)
            self.assertNotIn('"draft"', next_completed.stdout)
            self.assertLessEqual(len([line for line in next_completed.stdout.splitlines() if line.strip()]), 9)


    def test_agentteam_cli_queue_show_run_dir_does_not_require_project_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            profileless_repo = tmp_path / "profileless-repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "explicit-run"
            _init_repo(profileless_repo)
            _write_completed_operator_run(run_dir)

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "show",
                    "--project-root",
                    str(profileless_repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=profileless_repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["queue_status"], "ready")
            self.assertEqual(summary["source_taskpack_id"], "explicit-run")
            self.assertEqual(summary["source_run_dir"], str(run_dir))
            self.assertTrue(summary["items"])


    def test_agentteam_cli_queue_show_run_dir_uses_run_dir_work_root_over_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            profile_work_root = tmp_path / "profile-work-root"
            explicit_work_root = tmp_path / "explicit-work-root"
            run_dir = explicit_work_root / "runs" / "explicit-run"
            memory_path = explicit_work_root / "pursue" / "explicit-goal-memory.json"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, profile_work_root, "queue-project")
            _write_completed_operator_run(run_dir)
            _write_json(
                memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(memory_path),
                    "latest_taskpack_id": "explicit-run",
                    "follow_up_queue": [
                        {
                            "objective": "继续验证显式 run-dir 的 goal memory 读取。",
                            "source_taskpack_id": "explicit-run",
                            "source_report_path": str(run_dir / "reports" / "final_report.md"),
                            "readiness": "ready",
                        }
                    ],
                },
            )

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "show",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_dir),
                    "--json",
                ],
                cwd=repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["goal_memory_path"], str(memory_path))
            self.assertTrue(
                any(item["source"] == "goal_memory.follow_up_queue" for item in summary["items"])
            )


    def test_agentteam_cli_queue_next_renders_suggested_next_command(self):
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
                    "queue-project",
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
            first_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "start",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Initial optimization pass.",
                    "--taskpack-id",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(first_completed.returncode, 0, first_completed.stderr)

            queue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "queue",
                    "next",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            self.assertIn("queue_status: ready", queue_completed.stdout)
            self.assertIn("source_taskpack_id: first-pass", queue_completed.stdout)
            self.assertIn("next_goal:", queue_completed.stdout)
            self.assertIn("next_command: agentteam next --from-taskpack first-pass", queue_completed.stdout)
            self.assertNotIn("status: completed", queue_completed.stdout)


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
                    "--json",
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
                    "--expected-authoring-mode",
                    "direct_draft",
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
            for profile in agent_pool["role_runtime_profiles"].values():
                profile["adapter"] = "fake"
            agent_pool_path.write_text(json.dumps(agent_pool), encoding="utf-8")
            backlog_path = taskpack_dir / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            for item in backlog["items"]:
                item["write_scope"] = ["generated/"]
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
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(run_completed.returncode, 0, run_completed.stderr)
            run_summary = json.loads(run_completed.stdout)
            self.assertEqual(run_summary["run"]["scheduler_status"], "idle")
            self.assertEqual(
                run_summary["run"]["snapshot"]["tasks"]["TASK-CLI_RUN_FAKE-001"]["task_status"],
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


    def test_agentteam_cli_taskpack_draft_supports_deterministic_author_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            runtime_pkg.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (runtime_pkg / "taskpack_author.py").write_text("def author():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add runtime package"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "draft",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "Add deterministic taskpack author runtime.",
                    "--draft-root",
                    str(drafts),
                    "--taskpack-id",
                    "cli-deterministic-author",
                    "--author-runtime",
                    "deterministic",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["taskpack_id"], "cli-deterministic-author")
            self.assertEqual(
                validate_taskpack(drafts / "cli-deterministic-author")["status"],
                "accepted",
            )


    def test_pre04_18_launcher_rejects_schema_extras_before_runtime_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            pair = self._publish_pre04_run(work_root, release, "run-1")
            identity_path = Path(pair["run_dir"]) / "state" / "run_identity.v1.json"
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            identity["unexpected"] = True
            _write_json(identity_path, identity)
            launcher = runpy.run_path(str(Path(__file__).resolve().parents[4] / "agentteam"))

            with self.assertRaises(launcher["LauncherError"]):
                launcher["_validate_run_pair"](pair["run_dir"], "pre04")

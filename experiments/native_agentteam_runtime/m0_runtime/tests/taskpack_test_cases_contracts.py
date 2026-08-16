try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ContractsMixin:
    def test_submit_args_upgrade_legacy_native_runtime_default_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            runtime_root = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime"
            (runtime_root / "agentteam_runtime").mkdir(parents=True)
            (runtime_root / "agentteam_runtime" / "__init__.py").write_text("", encoding="utf-8")
            (runtime_root / "tests").mkdir()
            (runtime_root / "tests" / "test_taskpack.py").write_text("", encoding="utf-8")
            (runtime_root / "tests" / "test_m0_runtime.py").write_text("", encoding="utf-8")
            args = SimpleNamespace(
                goal="Improve reports.",
                work_root=None,
                taskpack_id=None,
                author_runtime=None,
                runtime=None,
                codex_timeout_seconds=600,
                one_shot=None,
                max_inflight=None,
                max_attempts=None,
                commit_verified_integration=None,
                notification_project=None,
                feishu_webhook_env=None,
                feishu_signing_secret_env=None,
                codex_command=None,
            )
            profile = {
                "project_key": "agentteam-native",
                "work_root": str(tmp_path / "work"),
                "author_runtime": "codex",
                "default_runtime": "codex",
                "verification_profile": {
                    "verification_profile_schema_version": "agentteam_verification_profile.v1",
                    "correctness": {"command": ["python3", "-m", "unittest", "discover"]},
                    "performance": {"command": None, "metrics": []},
                },
            }

            submit_args = _submit_args_from_profile(args, repo, profile)

            self.assertEqual(
                submit_args.verification_profile["correctness"]["command"],
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                ],
            )


    def test_post_backlog_context_maps_versioned_run_to_versioned_frozen_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_root = root / "work"
            project_root = root / "repo"
            run_dir = work_root / "runs" / "v4" / "phase1-run"
            run_dir.mkdir(parents=True)
            project_root.mkdir()
            versioned = {
                "taskpack_id": "phase1-run",
                "project_root": str(project_root),
                "post_backlog_gates": [{"gate_id": "P1-LIVE"}],
                "marker": "versioned",
            }
            flat = {**versioned, "marker": "flat"}
            _write_json(
                work_root / "frozen" / "v4" / "phase1-run" / "taskpack.yaml",
                versioned,
            )
            _write_json(
                work_root / "frozen" / "phase1-run" / "taskpack.yaml",
                flat,
            )

            context = agentteam_module._post_backlog_gate_context(
                {"work_root": str(work_root)},
                run_dir,
            )

            self.assertEqual(context["taskpack"]["marker"], "versioned")
            self.assertEqual(
                context["frozen_dir"],
                (work_root / "frozen" / "v4" / "phase1-run").resolve(),
            )


    def test_run_paths_for_frozen_taskpack_accepts_concrete_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            runs_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args.",
                draft_root=drafts,
                taskpack_id="runtime-paths",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            paths = _run_paths_for_frozen_taskpack(
                frozen["frozen_taskpack_dir"],
                runs_root / "runtime-paths",
            )

            self.assertEqual(paths["taskpack_id"], "runtime-paths")
            self.assertEqual(paths["run_root"], runs_root.resolve())
            self.assertEqual(paths["run_dir"], (runs_root / "runtime-paths").resolve())
            self.assertTrue(paths["normalized_from_concrete_run_dir"])


    def test_deterministic_taskpack_skeleton_keeps_uncertain_semantics_as_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="deterministic-skeleton",
                context_refs={
                    "source_report_path": "/tmp/work/runs/previous/reports/final_report.md",
                    "repo_map_manifest_path": "/tmp/work/state/repo_map/manifest.json",
                    "selected_next_goal": "Investigate the next implementation step.",
                },
                verification_command=["python3", "-m", "unittest", "discover"],
            )

            taskpack_dir = Path(result["taskpack_dir"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            item = loaded["backlog"]["items"][0]

            self.assertTrue(taskpack["semantic_authoring_required"])
            self.assertEqual(taskpack["authoring_mode"], "deterministic_skeleton")
            self.assertEqual(
                taskpack["context_refs"]["repo_map_manifest_path"],
                "/tmp/work/state/repo_map/manifest.json",
            )
            self.assertEqual(item["work_type"], "code_investigation")
            self.assertTrue(item["semantic_authoring_required"])
            self.assertEqual(item["write_scope"], [".agentteam/generated/"])
            self.assertIn("semantic_slots", item)
            self.assertIn("task_specific_objective", item["semantic_slots"])
            self.assertNotIn("optimization_candidate_matrix", item["required_deliverables"])
            self.assertNotIn("agentteam/**", item["write_scope"])


    def test_deterministic_taskpack_skeleton_rejects_high_semantic_goals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            with self.assertRaises(TaskpackValidationError) as raised:
                draft_deterministic_taskpack_skeleton(
                    project_root=repo,
                    goal="Optimize the existing competition repository latency.",
                    draft_root=drafts,
                    taskpack_id="deterministic-optimization",
                    context_refs={"repo_map_manifest_path": "/tmp/repo-map/manifest.json"},
                )

            self.assertIn("requires semantic authoring", str(raised.exception))


    def test_materialize_semantic_taskpack_turns_skeleton_into_runtime_launchable_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)

            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="semantic-skeleton",
                context_refs={
                    "source_report_path": "/tmp/work/runs/previous/reports/final_report.md",
                    "repo_map_manifest_path": "/tmp/work/state/repo_map/manifest.json",
                },
            )

            materialized = taskpack_module.materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                output_root=materialized_root,
                taskpack_id="semantic-executable",
                semantic_task={
                    "objective": "Implement the bounded parser cache fix described by the source report.",
                    "goal_alignment": "Uses source_report_path evidence to select one bounded implementation change.",
                    "read_scope": ["src/", "tests/"],
                    "write_scope": ["src/parser.py", "tests/test_parser.py"],
                    "work_type": "code_implementation",
                    "required_deliverables": [
                        "implemented_changes_or_no_safe_change_rationale",
                        "verification_summary",
                        "recommended_next_implementation_tasks",
                    ],
                    "verification_command": ["python3", "-m", "unittest", "discover"],
                    "evidence_paths": ["/tmp/work/runs/previous/reports/final_report.md"],
                },
            )

            taskpack_dir = Path(materialized["taskpack_dir"])
            validation = validate_taskpack(taskpack_dir)
            self.assertEqual(validation["status"], "accepted")
            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            item = loaded["backlog"]["items"][0]
            self.assertFalse(taskpack.get("semantic_authoring_required"))
            self.assertEqual(taskpack["authoring_mode"], "semantic_materialized")
            self.assertEqual(taskpack["materialized_from"]["taskpack_id"], "semantic-skeleton")
            self.assertFalse(item.get("semantic_authoring_required"))
            self.assertEqual(item["blockers"], [])
            self.assertEqual(item["objective"], "Implement the bounded parser cache fix described by the source report.")
            self.assertEqual(item["write_scope"], ["src/parser.py", "tests/test_parser.py"])
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])

            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            args = build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertEqual(_arg_value(args, "--backlog"), str(Path(frozen["frozen_taskpack_dir"]) / "backlog.json"))
            self.assertTrue((run_root / "semantic-executable").exists())


    def test_auto_materialize_semantic_taskpack_uses_grounding_candidate_verification_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            _init_repo(repo)

            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Use deterministic grounding to produce an executable follow-up taskpack.",
                draft_root=drafts,
                taskpack_id="candidate-verification-skeleton",
                context_refs={
                    "selected_next_goal": "Implement the bounded parser task.",
                    "read_scope": "src/parser.py\ntests/test_parser.py",
                    "write_scope": "src/parser.py\ntests/test_parser.py",
                    "repo_grounding_candidate_verification_commands": json.dumps(
                        [
                            {
                                "command": ["python3", "-m", "pytest", "tests/test_parser.py"],
                                "reason": "detected focused python test entrypoint",
                            }
                        ]
                    ),
                },
                verification_command=["python3", "-m", "unittest", "discover"],
            )

            materialized = taskpack_module.auto_materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                output_root=materialized_root,
                taskpack_id="candidate-verification-executable",
            )

            loaded = load_taskpack(materialized["taskpack_dir"])
            self.assertEqual(
                loaded["verification"]["command"],
                ["python3", "-m", "pytest", "tests/test_parser.py"],
            )
            self.assertEqual(validate_taskpack(materialized["taskpack_dir"])["status"], "accepted")


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
                worker_max_restart_count=3,
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
            self.assertEqual(_arg_value(args, "--worker-max-restart-count"), "3")
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


    def test_build_taskpack_runtime_args_uses_trusted_verification_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args with protocol-owned verification.",
                draft_root=drafts,
                taskpack_id="trusted-verification-runtime-args",
                write_scope=["src/"],
                verification_command=["python3", "-m", "unittest"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            trusted_command = [
                "/usr/bin/python3.12",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            ]

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                trusted_project_root=repo,
                trusted_verification_command=trusted_command,
            )

            self.assertEqual(
                json.loads(
                    _arg_value(
                        args,
                        "--integration-verification-command-json",
                    )
                ),
                trusted_command,
            )


    def test_build_taskpack_runtime_args_appends_trusted_codex_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Bind benchmark Codex context controls.",
                draft_root=tmp_path / "drafts",
                taskpack_id="trusted-codex-command",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(
                result["taskpack_dir"],
                tmp_path / "frozen",
            )
            trusted_command = [
                "codex",
                "exec",
                "-c",
                "tool_output_token_limit=4000",
                "-c",
                'web_search="disabled"',
            ]

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=tmp_path / "runs",
                trusted_codex_command=trusted_command,
            )

            self.assertEqual(
                args[-len(trusted_command) - 1 :],
                ["--codex-command", *trusted_command],
            )

    def test_notification_args_precede_trusted_codex_command_remainder(self):
        args = agentteam_module._insert_before_codex_command_remainder(
            [
                "--runtime",
                "codex",
                "--codex-command",
                "codex",
                "exec",
                "-c",
                "tool_output_token_limit=4000",
            ],
            ["--notification-project", "agentteam"],
        )

        marker = args.index("--codex-command")
        self.assertEqual(
            args[marker - 2 : marker],
            ["--notification-project", "agentteam"],
        )
        self.assertNotIn("--notification-project", args[marker + 1 :])


    def test_build_taskpack_runtime_args_rejects_unbound_trusted_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject an unbound trusted verification command.",
                draft_root=drafts,
                taskpack_id="unbound-trusted-verification",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "requires a trusted project root",
            ):
                build_taskpack_runtime_args(
                    frozen["frozen_taskpack_dir"],
                    run_root=tmp_path / "runs",
                    trusted_verification_command=["python3", "-m", "unittest"],
                )


    def test_build_taskpack_runtime_args_passes_codex_model_from_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args with a worker model.",
                draft_root=drafts,
                taskpack_id="codex-model-runtime-args",
                write_scope=["src/"],
                codex_timeout_seconds=123,
                codex_model="medium",
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertEqual(_arg_value(args, "--codex-model"), "medium")
            self.assertEqual(_arg_value(args, "--codex-timeout-seconds"), "123")


    def test_build_taskpack_runtime_args_uses_trusted_experiment_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args with a frozen experiment timeout.",
                draft_root=drafts,
                taskpack_id="trusted-timeout-runtime-args",
                write_scope=["src/"],
                codex_timeout_seconds=120,
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                trusted_codex_timeout_seconds=1800,
            )

            self.assertEqual(
                _arg_value(args, "--codex-timeout-seconds"),
                "1800",
            )
            self.assertEqual(
                _arg_value(args, "--lease-timeout-seconds"),
                "1860",
            )


    def test_build_taskpack_runtime_args_rejects_invalid_trusted_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject an invalid trusted timeout.",
                draft_root=drafts,
                taskpack_id="invalid-trusted-timeout",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            for invalid in (True, 0, 86401, "1800"):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    TaskpackValidationError,
                    "trusted Codex timeout",
                ):
                    build_taskpack_runtime_args(
                        frozen["frozen_taskpack_dir"],
                        run_root=tmp_path / f"runs-{invalid}",
                        trusted_codex_timeout_seconds=invalid,
                    )


    def test_reuse_repo_map_handoff_in_taskpack_removes_repo_map_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement the next bounded feature in the repository.",
                draft_root=drafts,
                taskpack_id="reuse-repo-map-handoff",
                write_scope=["src/"],
            )

            reuse = taskpack_module.reuse_repo_map_handoff_in_taskpack(result["taskpack_dir"])

            loaded = load_taskpack(result["taskpack_dir"])
            roles = [agent["role"] for agent in loaded["agent_pool"]["agents"]]
            items = loaded["backlog"]["items"]
            handoff_path = taskpack_module.REPO_MAP_HANDOFF_PATH

            self.assertEqual(reuse["status"], "applied")
            self.assertEqual(reuse["handoff_path"], handoff_path)
            self.assertEqual(reuse["removed_task_count"], 1)
            self.assertNotIn("repo_map_agent", roles)
            self.assertEqual(roles, ["implementation_worker"])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["required_role"], "implementation_worker")
            self.assertEqual(items[0]["depends_on"], [])
            self.assertEqual(items[0]["input_artifacts"], [handoff_path])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")

    def test_controller_grounding_completes_repo_map_prerequisite(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement the next bounded feature in the repository.",
                draft_root=tmp_path / "drafts",
                taskpack_id="controller-grounded-handoff",
                write_scope=["src/"],
            )

            binding = taskpack_module.satisfy_repo_map_with_controller_handoff(
                result["taskpack_dir"]
            )

            loaded = load_taskpack(result["taskpack_dir"])
            repo_task = next(
                item
                for item in loaded["backlog"]["items"]
                if item["task_id"] == binding["producer_task_id"]
            )
            implementation = next(
                item
                for item in loaded["backlog"]["items"]
                if item["required_role"] == "implementation_worker"
            )
            self.assertEqual(repo_task["backlog_status"], "done")
            self.assertIn(repo_task["task_id"], implementation["depends_on"])
            self.assertEqual(
                validate_taskpack(result["taskpack_dir"])["status"],
                "accepted",
            )


    def test_build_taskpack_runtime_args_passes_initial_integration_base_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Build runtime args with inherited integration baseline.",
                draft_root=drafts,
                taskpack_id="runtime-args-inherited-baseline",
                write_scope=["src/"],
            )
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)

            args = build_taskpack_runtime_args(
                frozen["frozen_taskpack_dir"],
                run_root=run_root,
                initial_integration_base_ref="abc123",
            )

            self.assertEqual(_arg_value(args, "--initial-integration-base-ref"), "abc123")


    def test_build_taskpack_runtime_args_rejects_semantic_skeleton_without_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal="Prepare the next bounded implementation task from supplied context.",
                draft_root=drafts,
                taskpack_id="runtime-semantic-skeleton",
                context_refs={"source_report_path": "/tmp/work/report.md"},
            )
            frozen = freeze_taskpack(skeleton["taskpack_dir"], frozen_root)

            with self.assertRaises(TaskpackValidationError) as raised:
                build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertIn("semantic authoring required before runtime launch", str(raised.exception))
            self.assertFalse((run_root / "runtime-semantic-skeleton").exists())


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

try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class AuthoringMixin:
    def test_blueprint_materialization_rejects_authority_drift_from_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            blueprint["tasks"][0]["objective"] = "Uncommitted authority drift."
            _write_json(repo / blueprint_path, blueprint)
            _write_blueprint_approval(repo, blueprint)

            with self.assertRaisesRegex(
                TaskpackValidationError,
                "blueprint_path must match the committed HEAD bytes",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "drafts",
                )


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
            self.assertEqual(_implementation_item(loaded["backlog"])["required_role"], "implementation_worker")
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_fake_taskpack_author_marks_optimization_goals_code_facing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="阅读这个比赛代码仓库并检查能否优化现有工作。",
                draft_root=drafts,
                author_runtime="fake",
                taskpack_id="optimize-competition",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            task = _implementation_item(loaded["backlog"])
            self.assertEqual(loaded["taskpack"]["goal_kind"], "optimization")
            self.assertEqual(task["work_type"], "code_implementation")
            self.assertIn("baseline_or_current_behavior", task["required_deliverables"])
            self.assertIn("optimization_candidate_matrix", task["required_deliverables"])
            self.assertIn("metric_delta_or_no_safe_change_evidence", task["required_deliverables"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_draft_taskpack_canonicalizes_env_python_verification_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            profile = {
                "correctness": {
                    "command": [
                        "env",
                        "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                        "python3",
                        "-m",
                        "unittest",
                        "discover",
                    ]
                }
            }

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a small parser improvement.",
                draft_root=drafts,
                taskpack_id="env-python-profile",
                verification_profile=profile,
            )

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(
                loaded["verification"]["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertEqual(
                loaded["verification"]["verification_profile"]["correctness"]["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_apply_verification_profile_canonicalizes_env_python_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            profile = {
                "correctness": {
                    "command": [
                        "env",
                        "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                        "python3",
                        "-m",
                        "unittest",
                        "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                        "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                    ]
                }
            }

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement AgentTeam report diagnostics.",
                draft_root=drafts,
                taskpack_id="codex-profile-apply",
            )
            _apply_verification_profile_to_taskpack(result["taskpack_dir"], profile)

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(
                loaded["verification"]["command"],
                [
                    "python3",
                    "-m",
                    "unittest",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_taskpack",
                    "experiments.native_agentteam_runtime.m0_runtime.tests.test_m0_runtime",
                ],
            )
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_deterministic_taskpack_author_materializes_executable_taskpack_from_grounding(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            tests_dir = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "tests"
            runtime_pkg.mkdir(parents=True)
            tests_dir.mkdir(parents=True)
            (runtime_pkg / "__init__.py").write_text("# runtime\n", encoding="utf-8")
            (runtime_pkg / "taskpack_author.py").write_text("def author():\n    return True\n", encoding="utf-8")
            (runtime_pkg / "taskpack.py").write_text("def materialize():\n    return True\n", encoding="utf-8")
            (runtime_pkg / "repo_grounding.py").write_text("def grounding():\n    return True\n", encoding="utf-8")
            (runtime_pkg / "repo_map.py").write_text("def map_repo():\n    return True\n", encoding="utf-8")
            (tests_dir / "test_taskpack.py").write_text("def test_taskpack():\n    assert True\n", encoding="utf-8")
            (tests_dir / "test_m0_runtime.py").write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'agentteam-fixture'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add runtime files"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal=(
                    "Feed compact repo_grounding.v1 and repo_structure.v1 signals into "
                    "taskpack authoring and automatic semantic materialization context."
                ),
                draft_root=drafts,
                author_runtime="deterministic",
                taskpack_id="deterministic-author",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            taskpack = loaded["taskpack"]
            task = loaded["backlog"]["items"][0]
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")
            self.assertEqual(taskpack["taskpack_id"], "deterministic-author")
            self.assertEqual(taskpack["authoring_mode"], "semantic_materialized")
            self.assertFalse(taskpack.get("semantic_authoring_required"))
            self.assertEqual(taskpack["semantic_completion"]["authority"], "automatic_deterministic")
            self.assertIn("repo_grounding.v1", task["goal_alignment"])
            self.assertIn("agentteam_target_review_gate", task["required_deliverables"])
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                task["write_scope"],
            )
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])
            context_refs = taskpack["context_refs"]
            self.assertEqual(context_refs["repo_grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(context_refs["repo_structure_schema_version"], "repo_structure.v1")
            grounding_languages = json.loads(context_refs["repo_grounding_languages"])
            self.assertIn(
                {"language": "python", "file_count": 7},
                [
                    {
                        "language": item["language"],
                        "file_count": item["file_count"],
                    }
                    for item in grounding_languages
                ],
            )
            candidate_commands = json.loads(context_refs["repo_grounding_candidate_verification_commands"])
            self.assertIn(
                ["python3", "-m", "unittest", "discover"],
                [item["command"] for item in candidate_commands],
            )
            repo_structure_budget = json.loads(context_refs["repo_structure_budget"])
            self.assertEqual(repo_structure_budget["omitted_count"], 0)
            self.assertGreaterEqual(repo_structure_budget["included_count"], 3)
            top_level_entries = json.loads(context_refs["repo_structure_top_level_entries"])
            self.assertIn(
                "experiments/",
                [entry["path"] for entry in top_level_entries],
            )
            self.assertEqual(
                taskpack["policy"]["source_control_restrictions"],
                ["no_merge", "no_push", "no_release_activation"],
            )


    def test_deterministic_taskpack_author_prefers_domain_scope_and_records_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "agentteam"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            runtime_pkg = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "agentteam_runtime"
            tests_dir = repo / "experiments" / "native_agentteam_runtime" / "m0_runtime" / "tests"
            runtime_pkg.mkdir(parents=True)
            tests_dir.mkdir(parents=True)
            for name in [
                "__init__.py",
                "artifact_lint.py",
                "artifact_repo.py",
                "completion_summary.py",
                "mailbox_worker.py",
                "notifications.py",
                "operator_report.py",
                "taskpack.py",
                "taskpack_author.py",
                "token_usage.py",
                "two_phase_scheduler.py",
                "worker_pool.py",
            ]:
                (runtime_pkg / name).write_text(
                    f"def {name.replace('.', '_')}():\n    return True\n",
                    encoding="utf-8",
                )
            (tests_dir / "test_m0_runtime.py").write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
            (tests_dir / "test_taskpack.py").write_text("def test_taskpack():\n    assert True\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'agentteam-fixture'\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add runtime files"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal=(
                    "Improve scheduler status report worker heartbeat token usage "
                    "and deterministic scope grounding diagnostics."
                ),
                draft_root=drafts,
                author_runtime="deterministic",
                taskpack_id="deterministic-scope-quality",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            task = loaded["backlog"]["items"][0]
            context_refs = loaded["taskpack"]["context_refs"]
            diagnostic = json.loads(context_refs["deterministic_scope_diagnostic"])

            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/two_phase_scheduler.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/operator_report.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/mailbox_worker.py",
                task["write_scope"],
            )
            self.assertIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/token_usage.py",
                task["write_scope"],
            )
            self.assertNotIn(
                "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/artifact_repo.py",
                task["write_scope"],
            )
            self.assertEqual(diagnostic["confidence"], "high")
            self.assertFalse(diagnostic["authoring_needs_review"])
            self.assertEqual(diagnostic["missing_expected_modules"], [])
            self.assertIn("scheduler", diagnostic["matched_goal_tokens"])
            self.assertIn("heartbeat", diagnostic["matched_goal_tokens"])


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

            prompt = _author_prompt(
                project_root=repo,
                goal="Implement M49 policy hardening for AgentTeam-as-target tasks.",
                taskpack_id="m49-agentteam-target",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("AgentTeam-as-target", prompt)
            self.assertIn("functional or semantic requirement", prompt)
            self.assertIn("do not request git merge or git push", prompt)
            self.assertIn("open-ended improvement requests", prompt)


    def test_codex_taskpack_author_prompt_treats_supplied_verification_as_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "protocol-acceptance"
            author_context_dir = tmp_path / "drafts" / ".protocol-acceptance-author"
            _init_repo(repo)
            command = [
                "python3",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            ]

            prompt = _author_prompt(
                project_root=repo,
                goal="Implement the bounded fixture change.",
                taskpack_id="protocol-acceptance",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile={"correctness": {"command": command}},
            )

            self.assertIn(json.dumps(command), prompt)
            self.assertIn("verification profile is authoritative", prompt)
            self.assertIn("Do not search for, infer, substitute, broaden, or execute alternative", prompt)


    def test_codex_taskpack_author_prompt_hardens_decomposition_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "m59-quality"
            author_context_dir = tmp_path / "drafts" / ".m59-quality-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Keep pursuing the operator goal across several bounded implementation rounds.",
                taskpack_id="m59-quality",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("Preserve the operator's original goal", prompt)
            self.assertIn("decompose broad or long-running goals into narrow, measurable next-step tasks", prompt)
            self.assertIn("tie each executable next-step objective to previous evidence", prompt)
            self.assertIn(
                "include a concise rationale naming source_report_path, verification results, blockers, goal_memory_path, or the queue-selected next_goal",
                prompt,
            )
            self.assertIn("avoid safe-but-trivial documentation-only changes unless the operator explicitly asked for documentation", prompt)


    def test_codex_taskpack_author_prompt_requires_role_routed_repo_map_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "role-routing"
            author_context_dir = tmp_path / "drafts" / ".role-routing-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Implement a bounded repository feature.",
                taskpack_id="role-routing",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("repo_map_agent", prompt)
            self.assertIn("implementation_worker", prompt)
            self.assertIn("repo_map_handoff", prompt)
            self.assertIn("depends_on", prompt)
            self.assertIn("risk_target in L0 or L1", prompt)
            self.assertIn("risk_target L2", prompt)
            self.assertIn("risk_target L3", prompt)
            self.assertIn("missing or unclear risk_target as L2", prompt)


    def test_codex_taskpack_author_prompt_requires_broad_framework_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "framework-quality"
            author_context_dir = tmp_path / "drafts" / ".framework-quality-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal="Strengthen the AgentTeam framework long-run reliability.",
                taskpack_id="framework-quality",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
            )

            self.assertIn("broad framework enhancement goals", prompt)
            self.assertIn("candidate_changes_or_no_safe_change_rationale", prompt)
            self.assertIn("non_goals", prompt)
            self.assertIn("review_gate", prompt)


    def test_codex_taskpack_author_prompt_references_required_file_template_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            author_context_dir = tmp_path / "drafts" / ".direct-author-author"
            taskpack_dir = tmp_path / "drafts" / "direct-author"
            _init_repo(repo)
            author_context_dir.mkdir(parents=True)
            template_path = _write_author_template_bundle(
                author_context_dir=author_context_dir,
                taskpack_id="direct-author",
                project_root=repo,
                goal="Draft a bounded follow-up taskpack.",
                verification_profile={
                    "correctness": {
                        "command": [
                            "env",
                            "PYTHONPATH=experiments/native_agentteam_runtime/m0_runtime",
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                        ]
                    }
                },
            )

            prompt = _author_prompt(
                project_root=repo,
                goal="Draft a bounded follow-up taskpack.",
                taskpack_id="direct-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
                template_bundle_path=template_path,
            )
            bundle = json.loads(template_path.read_text(encoding="utf-8"))

            self.assertEqual(set(bundle["templates"]), set(REQUIRED_TASKPACK_FILES))
            self.assertEqual(
                bundle["templates"]["verification.json"]["command"],
                ["python3", "-m", "unittest", "discover"],
            )
            self.assertIn("Required file template bundle:", prompt)
            self.assertIn(str(template_path), prompt)
            self.assertIn("replace placeholder values", prompt)


    def test_codex_taskpack_author_bundle_carries_compact_repo_grounding_context_for_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            author_context_dir = tmp_path / "drafts" / ".m67-followup-author"
            taskpack_dir = tmp_path / "drafts" / "m67-followup"
            _init_repo(repo)
            author_context_dir.mkdir(parents=True)
            grounding = {
                "grounding_schema_version": "repo_grounding.v1",
                "scan_status": "ok",
                "tracked_file_count": 42,
                "languages": [
                    {
                        "language": "python",
                        "file_count": 11,
                        "sample_files": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack_author.py"],
                    }
                ],
                "project_tools": [
                    {
                        "tool_id": "python-pyproject",
                        "tool_type": "python",
                        "path": "pyproject.toml",
                    }
                ],
                "test_entrypoints": [
                    {
                        "path": "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                        "language": "python",
                        "test_framework_hint": "python",
                    }
                ],
                "candidate_verification_commands": [
                    {
                        "command": ["python3", "-m", "unittest", "discover"],
                        "reason": "detected python test files",
                    }
                ],
                "repository_structure": {
                    "structure_schema_version": "repo_structure.v1",
                    "top_level_entry_budget": {
                        "max_entries": 12,
                        "total_entry_count": 3,
                        "included_count": 3,
                        "omitted_count": 0,
                    },
                    "top_level_entries": [
                        {
                            "path": "experiments/",
                            "entry_type": "directory",
                            "file_count": 40,
                        }
                    ],
                },
            }

            template_path = _write_author_template_bundle(
                author_context_dir=author_context_dir,
                taskpack_id="m67-followup",
                project_root=repo,
                goal=(
                    "Follow-up goal: Continue the roadmap-derived implementation route. "
                    "Previous taskpack context: source_report_path=/tmp/work/report.md"
                ),
                repo_grounding=grounding,
            )
            prompt = _author_prompt(
                project_root=repo,
                goal=(
                    "Follow-up goal: Continue the roadmap-derived implementation route. "
                    "Previous taskpack context: source_report_path=/tmp/work/report.md"
                ),
                taskpack_id="m67-followup",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                repo_map={
                    "paths": {
                        "manifest_path": "manifest.json",
                        "inventory_path": "inventory.json",
                        "symbols_path": "symbols.json",
                    }
                },
                verification_profile=None,
                template_bundle_path=template_path,
                repo_grounding=grounding,
            )
            bundle = json.loads(template_path.read_text(encoding="utf-8"))

            context = bundle["repo_grounding_context"]
            self.assertEqual(context["repo_grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(context["repo_grounding_scan_status"], "ok")
            self.assertEqual(context["repo_grounding_tracked_file_count"], 42)
            self.assertEqual(
                context["repo_grounding_languages"],
                [{"language": "python", "file_count": 11}],
            )
            self.assertEqual(
                context["repo_grounding_candidate_verification_commands"],
                [
                    {
                        "command": ["python3", "-m", "unittest", "discover"],
                        "reason": "detected python test files",
                    }
                ],
            )
            self.assertEqual(context["repo_structure_schema_version"], "repo_structure.v1")
            self.assertEqual(
                context["repo_structure_top_level_entries"],
                [{"path": "experiments/", "entry_type": "directory", "file_count": 40}],
            )
            self.assertIn("repo_grounding_context", prompt)
            self.assertIn("language, tool, test-entrypoint, and candidate verification-command", prompt)


    def test_codex_taskpack_author_captures_supported_usage_separately_from_estimates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "usage-author"
            author_context_dir = draft_root / ".usage-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            prompt_path = author_context_dir / "author_prompt.md"
            prompt_path.write_text("short prompt", encoding="utf-8")
            fake_codex = tmp_path / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, sys",
                        "sys.stdin.read()",
                        "print(json.dumps({",
                        "  'type': 'turn.completed',",
                        "  'usage': {",
                        "    'input_tokens': 101,",
                        "    'cached_input_tokens': 11,",
                        "    'output_tokens': 23,",
                        "    'total_tokens': 124,",
                        "  },",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"

            completed = _run_codex_author_command(
                [str(fake_codex), "exec", "--json"],
                draft_root=draft_root,
                prompt="short prompt",
                timeout_seconds=5,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="usage-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
                model="test-model",
                author_invocation_context={
                    "project": "usage-project",
                    "pursue_id": "PURSUE-1",
                    "round_index": 2,
                    "usage_stage": "follow_up_author",
                },
                systemd_runner_factory=_AuthorFakeGatedExecution,
            )

            self.assertEqual(completed.returncode, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            started = json.loads(
                Path(result["model_invocation"]["started_path"]).read_text(
                    encoding="utf-8"
                )
            )
            terminal = result["model_invocation_usage"]
            self.assertEqual(started["usage_stage"], "follow_up_author")
            self.assertEqual(started["round_index"], 2)
            self.assertEqual(started["pursue_id"], "PURSUE-1")
            self.assertEqual(started["coverage_class"], "supported_model_invocation")
            self.assertEqual(terminal["terminal_writer"], "taskpack_author")
            self.assertEqual(terminal["usage_status"], "reported")
            self.assertEqual(terminal["input_tokens"], 101)
            self.assertEqual(terminal["total_tokens"], 124)
            self.assertEqual(result["input_metrics"]["prompt_estimated_tokens"], 3)
            self.assertNotEqual(
                result["input_metrics"]["prompt_estimated_tokens"],
                terminal["input_tokens"],
            )


    def test_author_lifecycle_bootstrap_uses_contained_relative_digest_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            authority_root = work_root / "drafts" / ".bootstrap-author"
            context = _author_model_invocation_context(
                taskpack_id="bootstrap",
                draft_root=work_root / "drafts",
                model=None,
                supported=False,
                supplied={"project": "project"},
            )
            lifecycle = InvocationLifecycle(authority_root, context)
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            terminal = lifecycle.finalize(
                "completed",
                terminal_writer="taskpack_author",
            )

            bootstrap = _publish_author_lifecycle_bootstrap(
                work_root / "runs" / "bootstrap",
                work_root,
                {
                    **lifecycle.summary(),
                    "usage_event_id": terminal["usage_event_id"],
                },
            )

            self.assertFalse(Path(bootstrap["started_path"]).is_absolute())
            self.assertFalse(Path(bootstrap["terminal_path"]).is_absolute())
            self.assertEqual(len(bootstrap["started_sha256"]), 64)
            self.assertEqual(len(bootstrap["terminal_sha256"]), 64)
            self.assertEqual(
                bootstrap,
                json.loads(
                    (
                        work_root
                        / "runs"
                        / "bootstrap"
                        / "state"
                        / "author_lifecycle_bootstrap.v1.json"
                    ).read_text(encoding="utf-8")
                ),
            )


    def test_author_bootstrap_import_is_idempotent_and_rejects_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            run_dir = work_root / "runs" / "bootstrap-import"
            authority_root = work_root / "drafts" / ".bootstrap-import-author"
            context = _author_model_invocation_context(
                taskpack_id="bootstrap-import",
                draft_root=work_root / "drafts",
                model=None,
                supported=False,
                supplied={
                    "project": "project",
                    "run_id": "bootstrap-import",
                },
            )
            lifecycle = InvocationLifecycle(
                authority_root,
                context,
                invocation_id="INV-author-bootstrap-import",
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            terminal = lifecycle.finalize(
                "completed",
                terminal_writer="taskpack_author",
                finished_at="2026-07-23T00:00:01Z",
            )
            _publish_author_lifecycle_bootstrap(
                run_dir,
                work_root,
                {
                    **lifecycle.summary(),
                    "usage_event_id": terminal["usage_event_id"],
                },
            )

            first = import_author_lifecycle_bootstrap(run_dir)
            second = import_author_lifecycle_bootstrap(run_dir)
            projection = replay_model_invocation_events(
                run_dir / "events.jsonl"
            )

            self.assertEqual(len(first), 2)
            self.assertEqual(second, [])
            self.assertEqual(projection["invocation_count"], 1)
            self.assertEqual(projection["terminal_count"], 1)
            self.assertEqual(projection["open_invocation_ids"], [])
            start_event = next(
                event
                for event in _read_jsonl(run_dir / "events.jsonl")
                if event["event_type"] == "model_invocation_started"
            )
            self.assertEqual(
                start_event["source_event_id"],
                lifecycle.invocation_id,
            )

            bootstrap_path = (
                run_dir
                / "state"
                / "author_lifecycle_bootstrap.v1.json"
            )
            bootstrap = json.loads(
                bootstrap_path.read_text(encoding="utf-8")
            )
            bootstrap["started_sha256"] = "0" * 64
            bootstrap_path.write_text(
                json.dumps(bootstrap, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaises(ModelInvocationIntegrityError):
                import_author_lifecycle_bootstrap(run_dir)


    def test_author_recovery_preserves_live_and_reconciles_proven_death_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            live_root = tmp_path / "live-author"
            live_lifecycle = InvocationLifecycle(
                live_root,
                _author_model_invocation_context(
                    taskpack_id="live-author",
                    draft_root=tmp_path,
                    model=None,
                    supported=False,
                    supplied={"project": "project"},
                ),
            )
            live_lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            read_fd, write_fd = os.pipe()
            try:
                live = recover_open_author_invocations(
                    live_root,
                    fence_assessor=lambda _start: {
                        "fence_status": "live_pinned",
                        "proof": "pidfd_and_start_ticks_match",
                        "pidfd": read_fd,
                    },
                    service_stopper=lambda _start: False,
                )
            finally:
                os.close(write_fd)
            self.assertEqual(live[0]["reconciliation_status"], "live")
            self.assertFalse(live_lifecycle.terminal_path.exists())

            changed_boot_root = tmp_path / "changed-boot-author"
            changed_boot_lifecycle = InvocationLifecycle(
                changed_boot_root,
                _author_model_invocation_context(
                    taskpack_id="changed-boot-author",
                    draft_root=tmp_path,
                    model=None,
                    supported=False,
                    supplied={"project": "project"},
                ),
            )
            changed_boot_lifecycle.publish_start(
                ExecutionGroupIdentity.not_applicable()
            )
            recovered = recover_open_author_invocations(
                changed_boot_root,
                fence_assessor=lambda _start: {
                    "fence_status": "death_proven",
                    "proof": "host_boot_changed",
                },
                service_stopper=lambda _start: False,
            )
            self.assertEqual(
                recovered[0]["reconciliation_status"],
                "recovered",
            )
            terminal = json.loads(
                changed_boot_lifecycle.terminal_path.read_text(encoding="utf-8")
            )
            self.assertEqual(terminal["terminal_status"], "recovered_orphan")
            self.assertEqual(terminal["terminal_writer"], "recovery_controller")
            replay = recover_open_author_invocations(
                changed_boot_root,
                fence_assessor=lambda _start: {
                    "fence_status": "death_proven",
                    "proof": "host_boot_changed",
                },
                service_stopper=lambda _start: False,
            )
            self.assertEqual(
                replay[0]["reconciliation_status"],
                "terminal_available",
            )

            service_root = tmp_path / "service-author"
            service_lifecycle = InvocationLifecycle(
                service_root,
                _author_model_invocation_context(
                    taskpack_id="service-author",
                    draft_root=tmp_path,
                    model=None,
                    supported=False,
                    supplied={"project": "project"},
                ),
            )
            service_lifecycle.publish_start(
                ExecutionGroupIdentity.not_applicable()
            )
            assessments = iter(
                [
                    {
                        "fence_status": "exact_service_stop_required",
                        "proof": "exact_transient_cgroup_populated",
                    },
                    {
                        "fence_status": "death_proven",
                        "proof": "exact_transient_cgroup_empty",
                    },
                ]
            )
            stopped = []
            exact = recover_open_author_invocations(
                service_root,
                fence_assessor=lambda _start: next(assessments),
                service_stopper=lambda start: stopped.append(
                    start["invocation_id"]
                )
                or True,
            )
            self.assertEqual(exact[0]["reconciliation_status"], "recovered")
            self.assertEqual(stopped, [service_lifecycle.invocation_id])


    def test_ordinary_taskpack_delete_preserves_author_lifecycle_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            taskpack_id = "preserved-author"
            draft_dir = work_root / "drafts" / taskpack_id
            author_dir = work_root / "drafts" / f".{taskpack_id}-author"
            draft_dir.mkdir(parents=True)
            author_dir.mkdir(parents=True)
            context = _author_model_invocation_context(
                taskpack_id=taskpack_id,
                draft_root=work_root / "drafts",
                model=None,
                supported=False,
                supplied={"project": "project"},
            )
            lifecycle = InvocationLifecycle(author_dir, context)
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            lifecycle.finalize(
                "failed",
                terminal_writer="taskpack_author",
            )

            deleted = agentteam_module._delete_taskpack_from_profile(
                {"work_root": str(work_root)},
                taskpack_id,
                force=True,
            )

            self.assertEqual(deleted["deleted_count"], 1)
            self.assertFalse(draft_dir.exists())
            self.assertTrue(author_dir.exists())
            self.assertTrue(lifecycle.started_path.exists())
            self.assertTrue(lifecycle.terminal_path.exists())


    def test_codex_taskpack_author_timeout_result_includes_file_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "timeout-author"
            author_context_dir = draft_root / ".timeout-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"
            prompt_path = author_context_dir / "author_prompt.md"
            prompt_path.write_text("prompt", encoding="utf-8")

            completed = _run_codex_author_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time; "
                        "sys.stderr.write('author noise\\n' * 3); "
                        "sys.stderr.flush(); "
                        "time.sleep(5)"
                    ),
                ],
                draft_root=draft_root,
                prompt="",
                timeout_seconds=0.2,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="timeout-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
            )

            self.assertEqual(completed.returncode, -9)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertIn("diagnostic", result)
            diagnostic = result["diagnostic"]
            self.assertEqual(diagnostic["required_file_count"], len(REQUIRED_TASKPACK_FILES))
            self.assertEqual(diagnostic["written_required_file_count"], 0)
            self.assertEqual(set(diagnostic["missing_required_files"]), set(REQUIRED_TASKPACK_FILES))
            self.assertEqual(diagnostic["largest_stream"], "stderr")
            self.assertIn("author-direct", diagnostic["next_action"])
            terminal = result["model_invocation_usage"]
            self.assertEqual(terminal["terminal_status"], "timed_out")
            self.assertEqual(terminal["terminal_writer"], "taskpack_author")
            self.assertEqual(terminal["usage_status"], "not_applicable")


    def test_codex_taskpack_author_spools_raw_output_outside_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "spooled-author"
            author_context_dir = draft_root / ".spooled-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"
            prompt_path = author_context_dir / "author_prompt.md"
            prompt_path.write_text("prompt", encoding="utf-8")
            stdout_text = "stdout-line\n" * 700
            stderr_text = "stderr-line\n" * 700

            completed = _run_codex_author_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        f"sys.stdout.write({stdout_text!r}); "
                        f"sys.stderr.write({stderr_text!r}); "
                        "sys.exit(1)"
                    ),
                ],
                draft_root=draft_root,
                prompt="",
                timeout_seconds=5,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="spooled-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
            )

            self.assertEqual(completed.returncode, 1)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("stdout", result)
            self.assertNotIn("stderr", result)
            output = result["output"]
            self.assertEqual(output["stdout_bytes"], len(stdout_text.encode("utf-8")))
            self.assertEqual(output["stderr_bytes"], len(stderr_text.encode("utf-8")))
            self.assertLess(len(output["stdout_excerpt"]), len(stdout_text))
            self.assertLess(len(output["stderr_excerpt"]), len(stderr_text))
            self.assertEqual(Path(output["stdout_path"]).read_text(encoding="utf-8"), stdout_text)
            self.assertEqual(Path(output["stderr_path"]).read_text(encoding="utf-8"), stderr_text)
            self.assertEqual(state["output"]["stdout_path"], output["stdout_path"])
            self.assertEqual(state["output"]["stderr_path"], output["stderr_path"])


    def test_codex_taskpack_author_records_prompt_and_context_input_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            draft_root = tmp_path / "drafts"
            taskpack_dir = draft_root / "measured-author"
            author_context_dir = draft_root / ".measured-author-author"
            taskpack_dir.mkdir(parents=True)
            author_context_dir.mkdir(parents=True)
            result_path = author_context_dir / "author_result.json"
            state_path = author_context_dir / "author_state.json"
            prompt_path = author_context_dir / "author_prompt.md"
            prompt = "line one\nline two\n"
            prompt_path.write_text(prompt, encoding="utf-8")
            template_text = json.dumps(
                {"repo_grounding_context": {"repo_grounding_schema_version": "repo_grounding.v1"}},
                sort_keys=True,
            )
            (author_context_dir / "required_file_templates.json").write_text(
                template_text,
                encoding="utf-8",
            )

            completed = _run_codex_author_command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.read(); sys.exit(0)",
                ],
                draft_root=draft_root,
                prompt=prompt,
                timeout_seconds=5,
                state_path=state_path,
                result_path=result_path,
                taskpack_id="measured-author",
                taskpack_dir=taskpack_dir,
                author_context_dir=author_context_dir,
                prompt_path=prompt_path,
            )

            self.assertEqual(completed.returncode, 0)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("input_metrics", result)
            self.assertIn("input_metrics", state)
            metrics = result["input_metrics"]
            self.assertEqual(state["input_metrics"], metrics)
            self.assertEqual(metrics["prompt_bytes"], len(prompt.encode("utf-8")))
            self.assertEqual(metrics["prompt_chars"], len(prompt))
            self.assertEqual(metrics["prompt_line_count"], 2)
            self.assertEqual(metrics["prompt_estimated_tokens"], 5)
            self.assertEqual(metrics["author_context_file_count"], 2)
            self.assertEqual(
                metrics["author_context_bytes"],
                len(prompt.encode("utf-8")) + len(template_text.encode("utf-8")),
            )


    def test_codex_taskpack_author_failure_message_includes_compact_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_failed_author.py"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import sys",
                        "sys.stderr.write('failure-detail\\n' * 100)",
                        "sys.exit(2)",
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
                    taskpack_id="failed-author",
                    codex_command=[sys.executable, str(fake_codex)],
                    codex_timeout_seconds=5,
                )

            message = str(raised.exception)
            self.assertIn("codex taskpack author failed with exit code 2", message)
            self.assertIn("required_files_written=0/5", message)
            self.assertIn("largest_stream=stderr", message)
            self.assertIn("result_path=", message)
            self.assertIn("state_path=", message)
            self.assertNotIn("failure-detail", message)
            result_path = drafts / ".failed-author-author" / "author_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertIn("output", result)
            self.assertNotIn("stderr", result)
            self.assertEqual(
                result["model_invocation_usage"]["terminal_status"],
                "failed",
            )
            self.assertTrue(
                Path(result["model_invocation"]["started_path"]).is_file()
            )
            self.assertTrue(
                Path(result["model_invocation"]["terminal_path"]).is_file()
            )


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


    def test_draft_taskpack_files_adds_quality_deliverables_for_long_running_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the original implementation.\n\n"
                    "Previous taskpack context:\n"
                    "- source_report_path: /tmp/work/runs/previous/reports/final_report.md\n"
                ),
                draft_root=drafts,
                taskpack_id="followup-quality-deliverables",
                write_scope=["src/runtime.py"],
            )

            loaded = load_taskpack(result["taskpack_dir"])
            deliverables = _implementation_item(loaded["backlog"])["required_deliverables"]
            self.assertIn("roadmap_followup_route_template", deliverables)
            self.assertIn("candidate_changes_or_no_safe_change_rationale", deliverables)
            self.assertIn("non_goals", deliverables)
            self.assertIn("review_gate", deliverables)


    def test_canonicalize_codex_taskpack_restores_optimization_goal_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Optimize existing competition repository latency.",
                draft_root=drafts,
                taskpack_id="canonicalize-optimization",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["goal_kind"] = "audit"
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            _canonicalize_codex_taskpack_files(result["taskpack_dir"])

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(loaded["taskpack"]["goal_kind"], "optimization")
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_canonicalize_codex_taskpack_binds_direct_authoring_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen = tmp_path / "frozen"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded code change.",
                draft_root=drafts,
                taskpack_id="canonicalize-authoring-mode",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack.pop("authoring_mode")
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            _canonicalize_codex_taskpack_files(result["taskpack_dir"])

            loaded = load_taskpack(result["taskpack_dir"])
            self.assertEqual(loaded["taskpack"]["authoring_mode"], "direct_draft")
            frozen_result = freeze_taskpack(
                result["taskpack_dir"],
                frozen,
                expected_authoring_mode="direct_draft",
            )
            self.assertEqual(
                frozen_result["manifest"]["taskpack_id"],
                "canonicalize-authoring-mode",
            )


    def test_canonicalize_codex_taskpack_without_project_root_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded code change.",
                draft_root=drafts,
                taskpack_id="missing-project-root",
                write_scope=["src/"],
            )
            taskpack_path = Path(result["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack.pop("project_root")
            taskpack_path.write_text(json.dumps(taskpack), encoding="utf-8")

            _canonicalize_codex_taskpack_files(result["taskpack_dir"])

            loaded = json.loads(taskpack_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["goal_kind"], "implementation")
            self.assertNotIn("operator_review_required", loaded.get("policy", {}))


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
            self.assertEqual(
                taskpack["policy"]["source_control_restrictions"],
                ["no_merge", "no_push", "no_release_activation"],
            )
            self.assertIn("AgentTeam-as-target", task["goal_alignment"])
            self.assertIn("agentteam_target_review_gate", task["required_deliverables"])
            self.assertIn("do not merge", task["objective"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")


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


    def test_codex_taskpack_author_preserves_validation_error_for_non_object_taskpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            taskpack_dir = tmp_path / "drafts" / "codex-invalid-taskpack"
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text("[]", encoding="utf-8")
            (taskpack_dir / "agent_pool.json").write_text(
                json.dumps({"agents": []}),
                encoding="utf-8",
            )
            (taskpack_dir / "backlog.json").write_text(
                json.dumps({"items": []}),
                encoding="utf-8",
            )
            (taskpack_dir / "verification.json").write_text(
                json.dumps({"command": ["python3", "-m", "unittest", "discover"]}),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(taskpack_dir)
            self.assertIn("taskpack must be an object", str(raised.exception))


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
            implementation_agent = next(
                agent
                for agent in agent_pool["agents"]
                if agent["role"] == "implementation_worker"
            )
            implementation_item = _implementation_item(backlog)
            self.assertEqual(agent_pool["scheduler_agent_id"], "agent-scheduler")
            self.assertEqual(
                implementation_agent["inbox_path"],
                "mailboxes/implementation-worker-1/inbox.jsonl",
            )
            self.assertEqual(
                implementation_agent["outbox_path"],
                "mailboxes/implementation-worker-1/outbox.jsonl",
            )
            self.assertEqual(implementation_item["task_id"], "runtime-optimization-audit-001")
            self.assertEqual(implementation_item["objective"], "Audit and optimize runtime path")
            self.assertEqual(implementation_item["backlog_status"], "ready")
            self.assertEqual(implementation_item["blockers"], [])


    def test_codex_taskpack_author_unwraps_nested_taskpack_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-wrapped-taskpack"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "goal_kind": "implementation",
                        "semantic_contract_version": "task_semantics.v1",
                        "taskpack": {
                            "taskpack_schema_version": "taskpack.v1",
                            "taskpack_id": "codex-wrapped-taskpack",
                            "status": "draft",
                            "semantic_contract_version": "task_semantics.v1",
                            "project_root": str(repo),
                            "goal": "Implement a bounded runtime improvement.",
                            "original_goal": "Implement a bounded runtime improvement.",
                            "goal_kind": "implementation",
                            "runtime": {"default_backend": "codex"},
                            "files": {
                                "agent_pool": "agent_pool.json",
                                "backlog": "backlog.json",
                                "verification": "verification.json",
                            },
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
                                "item_id": "wrapped-001",
                                "title": "Implement bounded runtime improvement.",
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
            taskpack = json.loads((taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8"))
            self.assertEqual(taskpack["taskpack_id"], "codex-wrapped-taskpack")
            self.assertNotIn("taskpack", taskpack)


    def test_canonicalize_codex_taskpack_preserves_declared_implementation_for_generic_improve_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-generic-improve"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            goal = "Improve repo grounding with deterministic structure signals."
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-generic-improve",
                        "status": "draft",
                        "semantic_contract_version": "task_semantics.v1",
                        "project_root": str(repo),
                        "goal": goal,
                        "original_goal": goal,
                        "goal_kind": "implementation",
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
                                "item_id": "generic-improve-001",
                                "title": "Implement deterministic grounding signals.",
                                "status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["README.md"],
                                "write_scope": ["src/grounding.py"],
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
            taskpack = json.loads((taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8"))
            self.assertEqual(taskpack["goal_kind"], "implementation")


    def test_codex_taskpack_author_unwraps_nested_verification_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-wrapped-verification"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-wrapped-verification",
                        "status": "draft",
                        "semantic_contract_version": "task_semantics.v1",
                        "project_root": str(repo),
                        "goal": "Implement a bounded runtime feature.",
                        "original_goal": "Implement a bounded runtime feature.",
                        "goal_kind": "implementation",
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
                                "item_id": "wrapped-verification-001",
                                "title": "Implement bounded runtime feature.",
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
                json.dumps(
                    {
                        "verification": {
                            "command": ["python3", "-m", "unittest", "discover"],
                            "expected_evidence": ["exit code"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            _canonicalize_codex_taskpack_files(taskpack_dir)

            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            verification = json.loads((taskpack_dir / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(verification["command"], ["python3", "-m", "unittest", "discover"])
            self.assertNotIn("verification", verification)


    def test_codex_taskpack_author_canonicalizes_optimization_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-optimization-contract"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-optimization-contract",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "优化现有比赛代码，寻找可以验证的代码改进。",
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
                                "item_id": "optimize-code-001",
                                "title": "Find and implement one safe optimization",
                                "status": "ready",
                                "required_role": "implementation_worker",
                                "read_scope": ["."],
                                "write_scope": ["src/"],
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

            taskpack = json.loads((taskpack_dir / "taskpack.yaml").read_text(encoding="utf-8"))
            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            self.assertEqual(taskpack["goal_kind"], "optimization")
            self.assertEqual(task["work_type"], "code_implementation")
            self.assertIn("baseline_or_current_behavior", task["required_deliverables"])
            self.assertIn("metric_delta_or_no_safe_change_evidence", task["required_deliverables"])
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")


    def test_codex_taskpack_author_canonicalizes_optimization_audit_to_code_investigation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "codex-optimization-audit"
            _init_repo(repo)
            taskpack_dir.mkdir(parents=True)
            (taskpack_dir / "taskpack.yaml").write_text(
                json.dumps(
                    {
                        "taskpack_schema_version": "taskpack.v1",
                        "taskpack_id": "codex-optimization-audit",
                        "status": "draft",
                        "project_root": str(repo),
                        "goal": "优化 AgentTeam 长期运行状态提示。",
                        "goal_kind": "optimization",
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
                                "task_id": "optimize-progress-breadcrumb",
                                "objective": (
                                    "Audit progress breadcrumb output and implement a narrow "
                                    "code/test fix if a concrete ambiguity is found."
                                ),
                                "backlog_status": "ready",
                                "required_role": "implementation_worker",
                                "work_type": "audit",
                                "goal_alignment": (
                                    "Ties directly to previous evidence and the queue-selected next_goal."
                                ),
                                "read_scope": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py"],
                                "write_scope": ["experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/agentteam.py"],
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

            backlog = json.loads((taskpack_dir / "backlog.json").read_text(encoding="utf-8"))
            task = _implementation_item(backlog)
            self.assertEqual(task["work_type"], "code_investigation")
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")


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


    def test_codex_taskpack_author_records_timeout_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            fake_codex = tmp_path / "fake_timeout_author.py"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import time",
                        "time.sleep(10)",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TaskpackValidationError):
                draft_taskpack_from_goal(
                    project_root=repo,
                    goal="Improve fixture behavior.",
                    draft_root=drafts,
                    author_runtime="codex",
                    taskpack_id="author-state-timeout",
                    codex_command=["python3", str(fake_codex)],
                    codex_timeout_seconds=1,
                )

            state_path = drafts / ".author-state-timeout-author" / "author_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["author_status"], "timed_out")
            self.assertEqual(state["taskpack_id"], "author-state-timeout")
            self.assertEqual(state["timeout_seconds"], 1)
            self.assertTrue(state["pid"])
            self.assertTrue(Path(state["prompt_path"]).exists())
            self.assertTrue(Path(state["result_path"]).exists())


    def test_codex_taskpack_author_accepts_valid_complete_draft_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            taskpack_dir = drafts / "timeout-complete-draft"
            fake_codex = tmp_path / "fake_timeout_complete_author.py"
            _init_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import sys",
                        "import time",
                        "repo = pathlib.Path(sys.argv[1]).resolve()",
                        "taskpack_dir = pathlib.Path(sys.argv[2]).resolve()",
                        "taskpack_id = taskpack_dir.name",
                        "goal = 'Improve fixture behavior.'",
                        "taskpack = {",
                        "    'taskpack_schema_version': 'taskpack.v1',",
                        "    'taskpack_id': taskpack_id,",
                        "    'status': 'draft',",
                        "    'semantic_contract_version': 'task_semantics.v1',",
                        "    'project_root': str(repo),",
                        "    'goal': goal,",
                        "    'original_goal': goal,",
                        "    'goal_kind': 'implementation',",
                        "    'runtime': {'default_backend': 'codex'},",
                        (
                            "    'files': {'agent_pool': 'agent_pool.json', "
                            "'backlog': 'backlog.json', 'verification': 'verification.json'},"
                        ),
                        "    'policy': {'allow_merge': False},",
                        "}",
                        "agent_pool = {",
                        "    'scheduler_agent_id': 'agent-scheduler',",
                        "    'role_runtime_profiles': {'implementation_worker': {'adapter': 'codex'}},",
                        "    'agents': [{",
                        "        'agent_id': 'agent-implementation-worker-1',",
                        "        'role': 'implementation_worker',",
                        "        'status': 'idle',",
                        "        'inbox_path': 'mailboxes/agent-implementation-worker-1/inbox.jsonl',",
                        "        'outbox_path': 'mailboxes/agent-implementation-worker-1/outbox.jsonl',",
                        "    }],",
                        "}",
                        "backlog = {'backlog_id': 'BL-timeout-complete-draft', 'items': [{",
                        "    'task_id': 'TASK-TIMEOUT-COMPLETE-DRAFT-001',",
                        "    'objective': 'Improve fixture behavior.',",
                        "    'goal_alignment': 'Preserves the original goal: Improve fixture behavior.',",
                        "    'work_type': 'code_implementation',",
                        "    'required_deliverables': ['verification_summary'],",
                        "    'backlog_status': 'ready',",
                        "    'risk_target': 'L1',",
                        "    'depends_on': [],",
                        "    'read_scope': ['README.md'],",
                        "    'write_scope': ['README.md'],",
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
                        "(taskpack_dir / 'README.md').write_text('# timeout complete draft\\n', encoding='utf-8')",
                        "time.sleep(10)",
                    ]
                ),
                encoding="utf-8",
            )

            result = draft_taskpack_from_goal(
                project_root=repo,
                goal="Improve fixture behavior.",
                draft_root=drafts,
                author_runtime="codex",
                taskpack_id="timeout-complete-draft",
                codex_command=["python3", str(fake_codex), str(repo), str(taskpack_dir)],
                codex_timeout_seconds=0.2,
            )

            self.assertTrue(result["author_timeout_salvaged"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")
            result_path = drafts / ".timeout-complete-draft-author" / "author_result.json"
            state_path = drafts / ".timeout-complete-draft-author" / "author_state.json"
            author_result = json.loads(result_path.read_text(encoding="utf-8"))
            author_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(author_result["status"], "accepted_after_timeout")
            self.assertEqual(author_state["author_status"], "accepted_after_timeout")
            self.assertTrue(author_result["salvage"]["accepted"])


    def test_stop_authoring_terminates_recorded_author_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            author_dir = work_root / "drafts" / ".sleep-author"
            author_dir.mkdir(parents=True)
            process = subprocess.Popen(
                ["python3", "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _write_json(
                    author_dir / "author_state.json",
                    {
                        "author_status": "running",
                        "taskpack_id": "sleep",
                        "pid": process.pid,
                        "started_at": "2026-06-10T00:00:00Z",
                        "updated_at": "2026-06-10T00:00:01Z",
                    },
                )
                profile = {"project_key": "fixture", "work_root": str(work_root)}

                summary = _stop_authoring(profile, grace_seconds=1, force=True, operator="tester")

                process.wait(timeout=5)
                state = json.loads((author_dir / "author_state.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["stop_status"], "stopped_authoring")
                self.assertEqual(summary["taskpack_id"], "sleep")
                self.assertEqual(state["author_status"], "stopped")
                self.assertEqual(state["stopped_by"], "tester")
                self.assertIn(state["stop_signal"], {"SIGTERM", "SIGKILL"})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


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


    def test_draft_taskpack_routes_implementation_through_repo_map_then_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded feature in the repository.",
                draft_root=drafts,
                taskpack_id="role-routed-implementation",
                write_scope=["src/"],
            )

            loaded = load_taskpack(result["taskpack_dir"])
            agent_roles = {
                agent["role"]
                for agent in loaded["agent_pool"]["agents"]
            }
            items = loaded["backlog"]["items"]
            handoff_path = ".agentteam/generated/repo_map_handoff.json"

            self.assertIn("repo_map_agent", agent_roles)
            self.assertIn("implementation_worker", agent_roles)
            self.assertEqual([item["required_role"] for item in items], [
                "repo_map_agent",
                "implementation_worker",
            ])
            self.assertEqual(items[0]["work_type"], "repository_mapping")
            self.assertEqual(items[0]["write_scope"], [".agentteam/generated/"])
            self.assertEqual(items[0]["expected_output_artifacts"], [handoff_path])
            self.assertEqual(items[1]["depends_on"], [items[0]["task_id"]])
            self.assertEqual(items[1]["input_artifacts"], [handoff_path])
            self.assertEqual(items[1]["risk_target"], "L2")
            self.assertIn("repo_map_handoff", items[0]["required_deliverables"])
            self.assertIn("repo_map_handoff", items[1]["required_deliverables"])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


    def test_draft_taskpack_skips_repo_map_for_l1_implementation_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)

            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement a bounded local code change.",
                draft_root=drafts,
                taskpack_id="l1-direct-implementation",
                write_scope=["src/"],
                risk_target="L1",
            )

            loaded = load_taskpack(result["taskpack_dir"])
            agent_roles = [agent["role"] for agent in loaded["agent_pool"]["agents"]]
            items = loaded["backlog"]["items"]

            self.assertEqual(agent_roles, ["implementation_worker"])
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["risk_target"], "L1")
            self.assertEqual(items[0]["required_role"], "implementation_worker")
            self.assertEqual(items[0].get("input_artifacts"), None)
            self.assertEqual(items[0]["depends_on"], [])
            self.assertEqual(validate_taskpack(result["taskpack_dir"])["status"], "accepted")


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

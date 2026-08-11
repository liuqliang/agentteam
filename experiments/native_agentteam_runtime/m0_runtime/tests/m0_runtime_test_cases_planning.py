try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class PlanningMixin:
    def test_build_planner_context_summarizes_state_roles_and_scopes(self):
        agent_pool = {
            "agents": [
                {"agent_id": "agent-planner", "role": "task_planner"},
                {"agent_id": "agent-repo-map", "role": "repo_map_agent"},
            ]
        }
        state = {
            "backlog": {
                "items": [
                    {"task_id": "TASK-DONE", "backlog_status": "done"},
                    {"task_id": "TASK-BLOCKED", "backlog_status": "blocked"},
                ]
            },
            "steps": [{"task_id": "TASK-DONE", "step_status": "processed"}],
            "inflight_attempts": [],
        }

        context = build_planner_context(
            agent_pool,
            state,
            milestone_id="M22",
            default_worker_role="repo_map_agent",
            allowed_read_scopes=["."],
            allowed_write_scopes=["generated/"],
        )

        self.assertEqual(context["context_schema_version"], "planner_context.v1")
        self.assertEqual(context["milestone_id"], "M22")
        self.assertEqual(context["default_worker_role"], "repo_map_agent")
        self.assertEqual(context["allowed_write_scopes"], ["generated/"])
        self.assertEqual(
            context["available_agent_roles"],
            ["repo_map_agent", "task_planner"],
        )
        self.assertEqual(context["backlog_summary"]["done"], 1)
        self.assertEqual(context["backlog_summary"]["blocked"], 1)
        self.assertEqual(context["completed_task_ids"], ["TASK-DONE"])
        self.assertIn("proposal_contract", context)


    def test_repo_context_selects_files_inside_task_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "docs").mkdir()
            (repo / "tests").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            (repo / "pkg" / "helper.py").write_text(
                "def helper():\n    return 'helper'\n",
                encoding="utf-8",
            )
            (repo / "docs" / "guide.md").write_text(
                "# Guide\n\nUnrelated docs.\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_module.py").write_text(
                "from pkg.module import build_worker\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "pkg/module.py", "pkg/helper.py", "docs/guide.md", "tests/test_module.py"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add context files"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = {
                "task_id": "TASK-CTX-001",
                "objective": "Update build_worker behavior in pkg/module.py",
                "read_scope": ["pkg/"],
                "write_scope": ["pkg/module.py"],
            }

            context = build_repo_context(
                repo,
                output_dir,
                task,
                agent_role="repo_map_agent",
                max_files=3,
            )

            context_path = output_dir / "repo_contexts" / "TASK-CTX-001-repo_map_agent.json"
            self.assertEqual(context["repo_context_schema_version"], "repo_context.v1")
            self.assertEqual(context["repo_context_path"], str(context_path))
            self.assertTrue(context_path.exists())
            self.assertEqual(context["task_id"], "TASK-CTX-001")
            self.assertEqual(context["agent_role"], "repo_map_agent")
            self.assertEqual(context["selected_files"][0]["path"], "pkg/module.py")
            self.assertEqual(
                context["selected_files"][0]["selection_reasons"],
                ["write_scope", "read_scope", "objective"],
            )
            self.assertEqual(
                context["selected_files"][0]["symbols"]["functions"],
                [{"name": "build_worker", "line": 1}],
            )
            self.assertIn("repo_map_manifest_path", context)
            self.assertGreaterEqual(context["omitted_file_count"], 1)
            self.assertNotIn(
                "docs/guide.md",
                [entry["path"] for entry in context["selected_files"]],
            )
            structure = context["repository_structure"]
            self.assertEqual(structure["structure_schema_version"], "repo_structure.v1")
            self.assertEqual(structure["tracked_file_count"], 5)
            self.assertEqual(
                structure["category_counts"],
                [
                    {"category": "source", "file_count": 2},
                    {"category": "docs", "file_count": 2},
                    {"category": "test", "file_count": 1},
                ],
            )
            self.assertEqual(
                structure["top_level_entries"],
                [
                    {
                        "path": "README.md",
                        "entry_type": "file",
                        "file_count": 1,
                        "category_counts": [{"category": "docs", "file_count": 1}],
                        "language_counts": [{"language": "markdown", "file_count": 1}],
                    },
                    {
                        "path": "docs/",
                        "entry_type": "directory",
                        "file_count": 1,
                        "category_counts": [{"category": "docs", "file_count": 1}],
                        "language_counts": [{"language": "markdown", "file_count": 1}],
                    },
                    {
                        "path": "pkg/",
                        "entry_type": "directory",
                        "file_count": 2,
                        "category_counts": [{"category": "source", "file_count": 2}],
                        "language_counts": [{"language": "python", "file_count": 2}],
                    },
                    {
                        "path": "tests/",
                        "entry_type": "directory",
                        "file_count": 1,
                        "category_counts": [{"category": "test", "file_count": 1}],
                        "language_counts": [{"language": "python", "file_count": 1}],
                    },
                ],
            )
            self.assertEqual(
                structure["task_scope_summaries"],
                [
                    {
                        "scope_type": "read",
                        "scope": "pkg/",
                        "matched_file_count": 2,
                        "sample_paths": ["pkg/helper.py", "pkg/module.py"],
                        "category_counts": [{"category": "source", "file_count": 2}],
                        "language_counts": [{"language": "python", "file_count": 2}],
                    },
                    {
                        "scope_type": "write",
                        "scope": "pkg/module.py",
                        "matched_file_count": 1,
                        "sample_paths": ["pkg/module.py"],
                        "category_counts": [{"category": "source", "file_count": 1}],
                        "language_counts": [{"language": "python", "file_count": 1}],
                    },
                ],
            )


    def test_repo_context_reports_candidate_tests_for_selected_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "tests").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_module.py").write_text(
                "\n".join(
                    [
                        "from pkg.module import build_worker",
                        "",
                        "def test_build_worker():",
                        "    assert build_worker() == 'worker'",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "pkg/module.py", "tests/test_module.py"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add source and test"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = {
                "task_id": "TASK-CTX-TESTS-001",
                "objective": "Update build_worker behavior in pkg/module.py",
                "read_scope": ["pkg/"],
                "write_scope": ["pkg/module.py"],
            }

            context = build_repo_context(
                repo,
                output_dir,
                task,
                agent_role="repo_map_agent",
                max_files=1,
            )

            self.assertEqual(context["selected_files"][0]["path"], "pkg/module.py")
            self.assertEqual(
                context["candidate_tests"],
                [
                    {
                        "path": "tests/test_module.py",
                        "language": "python",
                        "selection_reasons": [
                            "imports_selected_module",
                            "path_match",
                            "objective",
                        ],
                    }
                ],
            )


    def test_repo_context_reports_candidate_tests_for_selected_typescript_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "src").mkdir()
            (repo / "tests").mkdir()
            (repo / "src" / "service.ts").write_text(
                "\n".join(
                    [
                        "export function buildDashboard() {",
                        "  return 'dashboard';",
                        "}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (repo / "tests" / "service.test.ts").write_text(
                "\n".join(
                    [
                        "import { buildDashboard } from '../src/service';",
                        "",
                        "test('builds dashboard', () => {",
                        "  expect(buildDashboard()).toBe('dashboard');",
                        "});",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "src/service.ts", "tests/service.test.ts"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add typescript source and test"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = {
                "task_id": "TASK-CTX-TS-TESTS-001",
                "objective": "Update buildDashboard behavior in src/service.ts",
                "read_scope": ["src/"],
                "write_scope": ["src/service.ts"],
            }

            context = build_repo_context(
                repo,
                output_dir,
                task,
                agent_role="repo_map_agent",
                max_files=1,
            )

            self.assertEqual(context["selected_files"][0]["path"], "src/service.ts")
            self.assertEqual(
                context["candidate_tests"],
                [
                    {
                        "path": "tests/service.test.ts",
                        "language": "typescript",
                        "selection_reasons": [
                            "imports_selected_module",
                            "path_match",
                            "objective",
                        ],
                    }
                ],
            )


    def test_repo_context_ranks_symbol_match_above_path_only_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "a_build_worker_notes.py").write_text(
                "def helper():\n    return 'notes'\n",
                encoding="utf-8",
            )
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "pkg/a_build_worker_notes.py", "pkg/module.py"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add ranking fixtures"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = {
                "task_id": "TASK-CTX-RANK-001",
                "objective": "Update build_worker behavior.",
                "read_scope": ["pkg/"],
                "write_scope": [],
            }

            context = build_repo_context(
                repo,
                output_dir,
                task,
                agent_role="repo_map_agent",
                max_files=1,
            )

            self.assertEqual(context["selected_files"][0]["path"], "pkg/module.py")


    def test_task_proposal_normalizes_valid_generated_tasks(self):
        proposal = {
            "milestone_id": "M21",
            "tasks": [
                {
                    "task_id": "TASK-M21-001",
                    "objective": "Add a bounded generated task.",
                    "read_scope": ["experiments/native_agentteam_runtime/"],
                    "write_scope": ["experiments/native_agentteam_runtime/generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L1",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        normalized = normalize_task_proposal(
            proposal,
            existing_task_ids={"DECOMPOSE-M21-001"},
        )

        self.assertEqual(normalized["proposal_status"], "accepted")
        self.assertEqual(normalized["generated_task_ids"], ["TASK-M21-001"])
        self.assertEqual(normalized["tasks"][0]["backlog_status"], "ready")
        self.assertEqual(normalized["tasks"][0]["milestone_id"], "M21")


    def test_task_proposal_rejects_duplicate_existing_task_id(self):
        proposal = {
            "milestone_id": "M21",
            "tasks": [
                {
                    "task_id": "TASK-M21-001",
                    "objective": "Duplicate task id.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "duplicate task_id"):
            normalize_task_proposal(
                proposal,
                existing_task_ids={"TASK-M21-001"},
            )


    def test_task_proposal_rejects_unknown_required_role(self):
        proposal = {
            "milestone_id": "M22",
            "tasks": [
                {
                    "task_id": "TASK-M22-001",
                    "objective": "Use an unknown role.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "required_role": "unknown_role",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "unknown required_role"):
            normalize_task_proposal(
                proposal,
                allowed_roles={"repo_map_agent"},
            )


    def test_task_proposal_rejects_write_scope_outside_allowed_prefix(self):
        proposal = {
            "milestone_id": "M22",
            "tasks": [
                {
                    "task_id": "TASK-M22-001",
                    "objective": "Write outside generated scope.",
                    "read_scope": ["."],
                    "write_scope": ["src/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "write_scope outside allowed scope"):
            normalize_task_proposal(
                proposal,
                allowed_roles={"repo_map_agent"},
                allowed_write_scopes=["generated/"],
            )


    def test_task_proposal_rejects_self_dependency(self):
        proposal = {
            "milestone_id": "M25",
            "tasks": [
                {
                    "task_id": "TASK-M25-001",
                    "objective": "Depend on itself.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": ["TASK-M25-001"],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "self dependency"):
            normalize_task_proposal(proposal)


    def test_task_proposal_rejects_generated_dependency_cycle(self):
        proposal = {
            "milestone_id": "M25",
            "tasks": [
                {
                    "task_id": "TASK-M25-001",
                    "objective": "First cyclic task.",
                    "read_scope": ["."],
                    "write_scope": ["generated/one/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L1",
                    "depends_on": ["TASK-M25-002"],
                    "blockers": [],
                },
                {
                    "task_id": "TASK-M25-002",
                    "objective": "Second cyclic task.",
                    "read_scope": ["."],
                    "write_scope": ["generated/two/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L1",
                    "depends_on": ["TASK-M25-001"],
                    "blockers": [],
                },
            ],
        }

        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            normalize_task_proposal(proposal)


    def test_task_proposal_rejects_unsupported_risk_target(self):
        proposal = {
            "milestone_id": "M25",
            "tasks": [
                {
                    "task_id": "TASK-M25-001",
                    "objective": "Use unsupported risk target.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L9",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "unsupported risk_target"):
            normalize_task_proposal(proposal)


    def test_task_proposal_rejects_l0_with_multiple_write_scopes(self):
        proposal = {
            "milestone_id": "M25",
            "tasks": [
                {
                    "task_id": "TASK-M25-001",
                    "objective": "Declare too many write scopes for L0.",
                    "read_scope": ["."],
                    "write_scope": ["generated/one/", "generated/two/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "L0 task may not declare multiple write scopes",
        ):
            normalize_task_proposal(proposal)


    def test_task_proposal_routes_l2_generated_task_to_review(self):
        proposal = {
            "milestone_id": "M25",
            "tasks": [
                {
                    "task_id": "TASK-M25-REVIEW-001",
                    "objective": "Require review before execution.",
                    "read_scope": ["."],
                    "write_scope": ["generated/review/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L2",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        normalized = normalize_task_proposal(proposal)
        task = normalized["tasks"][0]

        self.assertEqual(task["backlog_status"], "blocked")
        self.assertEqual(task["blockers"], ["requires_review"])


    def test_task_proposal_routes_l3_generated_task_to_semantic_escalation(self):
        proposal = {
            "milestone_id": "M39",
            "tasks": [
                {
                    "task_id": "TASK-M39-ARCH-001",
                    "objective": "Resolve a semantic architecture contract change.",
                    "read_scope": ["design/"],
                    "write_scope": ["design/architecture.md"],
                    "required_role": "implementation_worker",
                    "risk_target": "L3",
                    "depends_on": [],
                    "blockers": [],
                }
            ],
        }

        normalized = normalize_task_proposal(proposal)
        task = normalized["tasks"][0]

        self.assertEqual(task["backlog_status"], "ready")
        self.assertEqual(task["blockers"], [])
        self.assertEqual(task["semantic_escalation_status"], "required")
        self.assertEqual(task["required_role"], "semantic_architecture_agent")
        self.assertEqual(task["recommended_role"], "semantic_architecture_agent")


    def test_repo_context_path_is_recorded_on_attempt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "pkg/module.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add module"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = _backlog_task("TASK-001", write_scope=["generated/"])
            task["objective"] = "Update build_worker behavior in pkg/module.py"
            task["read_scope"] = ["pkg/"]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[task],
            )

            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            state_index = read_scheduler_state_index(output_dir)
            context_path = output_dir / "repo_contexts" / "ATTEMPT-001-repo_map_agent.json"

            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["repo_context_path"],
                str(context_path),
            )
            self.assertEqual(
                state_index["attempts"][0]["repo_context_path"],
                str(context_path),
            )


    def test_runtime_observability_reports_repo_context_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "pkg/module.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add module"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = _backlog_task("TASK-001", write_scope=["generated/"])
            task["objective"] = "Update build_worker behavior in pkg/module.py"
            task["read_scope"] = ["pkg/"]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[task],
            )

            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            repo_contexts_view = build_runtime_observability(
                output_dir,
                view="repo-contexts",
            )
            repo_context = repo_contexts_view["repo_contexts"][0]

            self.assertEqual(repo_contexts_view["view"], "repo-contexts")
            self.assertEqual(repo_contexts_view["repo_context_count"], 1)
            self.assertEqual(repo_context["attempt_id"], "ATTEMPT-001")
            self.assertEqual(repo_context["task_id"], "TASK-001")
            self.assertEqual(repo_context["agent_role"], "repo_map_agent")
            self.assertEqual(repo_context["selected_file_count"], 2)
            self.assertEqual(repo_context["selected_files"][0]["path"], "pkg/module.py")
            self.assertIn("repo_map_manifest_path", repo_context)


    def test_runtime_observability_reports_repo_context_diff_hit_metrics(self):
        class SourceEditRuntimeAdapter:
            def run(self, message, worktree_path=None):
                target = Path(worktree_path) / "pkg" / "module.py"
                target.write_text(
                    "def build_worker():\n    return 'updated-worker'\n",
                    encoding="utf-8",
                )
                return {
                    "result_status": "completed",
                    "changed_files": ["pkg/module.py"],
                    "output": {"adapter": "source-edit"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "pkg/module.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add module"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = _backlog_task("TASK-001", write_scope=["pkg/module.py"])
            task["objective"] = "Update build_worker behavior in pkg/module.py"
            task["read_scope"] = ["pkg/"]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["pkg/module.py"],
                tasks=[task],
            )

            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=SourceEditRuntimeAdapter(),
            )

            repo_contexts_view = build_runtime_observability(
                output_dir,
                view="repo-contexts",
            )
            repo_context = repo_contexts_view["repo_contexts"][0]

            self.assertEqual(repo_context["actual_changed_file_count"], 1)
            self.assertEqual(repo_context["changed_selected_file_count"], 1)
            self.assertEqual(repo_context["changed_selected_files"], ["pkg/module.py"])
            self.assertEqual(repo_context["changed_unselected_files"], [])
            self.assertEqual(repo_context["selected_file_hit_rate"], 1.0)


    def test_runtime_observability_reports_repo_context_candidate_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "tests").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_module.py").write_text(
                "import pkg.module\n\n"
                "def test_build_worker():\n"
                "    assert pkg.module.build_worker() == 'worker'\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "pkg/module.py", "tests/test_module.py"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add module and test"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = _backlog_task("TASK-001", write_scope=["generated/"])
            task["objective"] = "Update build_worker behavior in pkg/module.py"
            task["read_scope"] = ["pkg/"]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[task],
            )

            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            repo_contexts_view = build_runtime_observability(
                output_dir,
                view="repo-contexts",
            )
            repo_context = repo_contexts_view["repo_contexts"][0]

            self.assertEqual(repo_context["candidate_test_count"], 1)
            self.assertEqual(
                repo_context["candidate_tests"],
                [
                    {
                        "path": "tests/test_module.py",
                        "language": "python",
                        "selection_reasons": [
                            "imports_selected_module",
                            "path_match",
                            "objective",
                        ],
                    }
                ],
            )


    def test_cli_can_show_runtime_observability_repo_contexts_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def build_worker():\n    return 'worker'\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "pkg/module.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add module"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            task = _backlog_task("TASK-001", write_scope=["generated/"])
            task["objective"] = "Update build_worker behavior in pkg/module.py"
            task["read_scope"] = ["pkg/"]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[task],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--output-dir",
                    str(output_dir),
                    "--show-runtime-observability",
                    "--observability-view",
                    "repo-contexts",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["view"], "repo-contexts")
            self.assertEqual(summary["repo_context_count"], 1)
            self.assertEqual(
                summary["repo_contexts"][0]["selected_files"][0]["path"],
                "pkg/module.py",
            )


    def test_runtime_observability_reports_current_milestone_and_decomposition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [("agent-planner", "task_planner")],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M30",
            )

            scheduler.dispatch_ready()
            summary = build_runtime_observability(output_dir)

            self.assertIn("current_milestone", summary)
            self.assertIn("next_decomposition", summary)
            self.assertEqual(summary["current_milestone"]["milestone_id"], "M30")
            self.assertEqual(
                summary["current_milestone"]["current_decomposition_task_id"],
                "DECOMPOSE-M30-001",
            )
            self.assertEqual(
                summary["next_decomposition"]["task_id"],
                "DECOMPOSE-M30-001",
            )
            self.assertEqual(summary["next_decomposition"]["task_status"], "ready")
            self.assertEqual(summary["next_decomposition"]["required_role"], "task_planner")


    def test_agentteam_resume_interactive_supports_context_commands_before_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            scheduler = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "blocked",
                [],
                {
                    "manual_gate": {
                        "question": "Which context-aware route should runtime take?",
                        "options": ["minimal", "expanded"],
                        "reason": "Worker cannot choose between two operator-visible routes.",
                    }
                },
            )
            scheduler.collect_ready_results()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "resume",
                    "--run-dir",
                    str(output_dir),
                    "--interactive",
                    "--operator",
                    "liuql",
                ],
                input=(
                    "/task\n"
                    "/why\n"
                    "/events\n"
                    "/context\n"
                    "/answer Use the context-aware answer.\n"
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertIn("Commands:", completed.stderr)
            self.assertIn("TASK-001", completed.stderr)
            self.assertIn("Create generated repo index for TASK-001.", completed.stderr)
            self.assertIn(
                "Worker cannot choose between two operator-visible routes.",
                completed.stderr,
            )
            self.assertIn("manual_gate_required", completed.stderr)
            self.assertEqual(summary["resume_status"], "answered_manual_gate")
            self.assertEqual(summary["answered_count"], 1)
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["answer"],
                "Use the context-aware answer.",
            )


    def test_codex_runtime_adapter_builds_planner_prompt_contract(self):
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-planner",
            "message_type": "dispatch_task",
            "correlation_id": "DECOMPOSE-M23-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "DECOMPOSE-M23-001",
                "attempt_id": "DECOMPOSE-M23-001-ATTEMPT-001",
                "lease_id": "DECOMPOSE-M23-001-LEASE-001",
                "task_kind": "decompose_backlog",
                "milestone_id": "M23",
                "planner_context_path": (
                    "/tmp/planner_contexts/DECOMPOSE-M23-001.json"
                ),
                "objective": "Generate bounded backlog tasks.",
                "read_scope": ["."],
                "write_scope": [],
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("AgentTeam planner", prompt)
        self.assertIn("task_proposal", prompt)
        self.assertIn("DECOMPOSE-M23-001.json", prompt)
        self.assertIn('"changed_files": []', prompt)
        self.assertIn('"required_role"', prompt)


    def test_codex_runtime_adapter_includes_role_context_path(self):
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-repo-map",
            "message_type": "dispatch_task",
            "correlation_id": "TASK-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "TASK-001",
                "attempt_id": "ATTEMPT-001",
                "lease_id": "LEASE-001",
                "objective": "Implement a bounded change.",
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "role_context_path": "/tmp/role_contexts/ATTEMPT-001-repo_map_agent.json",
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("Role context package:", prompt)
        self.assertIn("ATTEMPT-001-repo_map_agent.json", prompt)
        self.assertIn("Read role_context_path before using role-specific context.", prompt)


    def test_codex_runtime_adapter_includes_repo_context_path(self):
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-repo-map",
            "message_type": "dispatch_task",
            "correlation_id": "TASK-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "TASK-001",
                "attempt_id": "ATTEMPT-001",
                "lease_id": "LEASE-001",
                "objective": "Implement a bounded change.",
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "repo_context_path": "/tmp/repo_contexts/ATTEMPT-001-repo_map_agent.json",
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("Repo context package:", prompt)
        self.assertIn("ATTEMPT-001-repo_map_agent.json", prompt)
        self.assertIn("Read repo_context_path before selecting implementation files.", prompt)

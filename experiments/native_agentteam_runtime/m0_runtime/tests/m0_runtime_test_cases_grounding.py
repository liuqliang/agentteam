try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class GroundingMixin:
    def test_build_planner_context_includes_bounded_artifact_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            artifact = tmp_path / "roadmap.md"
            artifact.write_text(
                "\n".join(
                    [
                        "# Native Runtime Roadmap",
                        "",
                        "This is the selected roadmap artifact.",
                        "",
                        "## M24",
                        *["context line" for _ in range(40)],
                        "UNIQUE_TAIL_MARKER_SHOULD_NOT_BE_EMBEDDED",
                    ]
                ),
                encoding="utf-8",
            )

            context = build_planner_context(
                {"agents": [{"agent_id": "agent-planner", "role": "task_planner"}]},
                {"backlog": {"items": []}, "steps": [], "inflight_attempts": []},
                milestone_id="M24",
                default_worker_role="repo_map_agent",
                context_artifact_paths=[artifact],
                context_artifact_excerpt_chars=80,
            )

            artifact_context = context["artifact_context"]
            source = artifact_context["sources"][0]

            self.assertEqual(artifact_context["schema_version"], "artifact_context.v1")
            self.assertEqual(artifact_context["excerpt_budget_chars"], 80)
            self.assertEqual(source["path"], str(artifact))
            self.assertEqual(len(source["sha256"]), 64)
            self.assertEqual(source["size_bytes"], artifact.stat().st_size)
            self.assertIn("modified_at", source)
            self.assertEqual(source["headings"], ["Native Runtime Roadmap", "M24"])
            self.assertLessEqual(source["excerpt_chars"], 80)
            self.assertGreater(source["omitted_chars"], 0)
            self.assertNotIn(
                "UNIQUE_TAIL_MARKER_SHOULD_NOT_BE_EMBEDDED",
                json.dumps(artifact_context, sort_keys=True),
            )


    def test_build_planner_context_warns_for_missing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-design.md"

            context = build_planner_context(
                {"agents": [{"agent_id": "agent-planner", "role": "task_planner"}]},
                {"backlog": {"items": []}, "steps": [], "inflight_attempts": []},
                milestone_id="M24",
                default_worker_role="repo_map_agent",
                context_artifact_paths=[missing],
            )

            self.assertEqual(context["artifact_context"]["sources"], [])
            self.assertEqual(
                context["artifact_context"]["warnings"],
                [{"path": str(missing), "warning": "missing"}],
            )


    def test_repo_map_inventory_records_tracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "import os\n\n\ndef run():\n    return os.getcwd()\n",
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

            repo_map = build_repository_map(repo, output_dir)

            inventory_path = output_dir / "state" / "repo_map" / "inventory.json"
            manifest_path = output_dir / "state" / "repo_map" / "manifest.json"
            self.assertEqual(repo_map["manifest"]["repo_map_schema_version"], "repo_map.v1")
            self.assertEqual(repo_map["manifest"]["scan_status"], "ok")
            self.assertEqual(repo_map["paths"]["inventory_path"], str(inventory_path))
            self.assertTrue(inventory_path.exists())
            self.assertTrue(manifest_path.exists())

            files = {entry["path"]: entry for entry in repo_map["inventory"]["files"]}
            self.assertEqual(sorted(files), ["README.md", "pkg/module.py"])
            self.assertEqual(files["README.md"]["language"], "markdown")
            self.assertEqual(files["README.md"]["category"], "docs")
            self.assertEqual(files["pkg/module.py"]["language"], "python")
            self.assertEqual(files["pkg/module.py"]["category"], "source")
            self.assertEqual(len(files["pkg/module.py"]["sha256"]), 64)


    def test_repo_map_skips_tracked_gitlink_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "vendor").mkdir()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "160000",
                    "0123456789012345678901234567890123456789",
                    "vendor/submodule",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add gitlink"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            (repo / "vendor" / "submodule").mkdir()

            repo_map = build_repository_map(repo, output_dir)

            files = {entry["path"]: entry for entry in repo_map["inventory"]["files"]}
            warnings = {
                (warning.get("path"), warning.get("warning"))
                for warning in repo_map["manifest"]["warnings"]
            }
            self.assertNotIn("vendor/submodule", files)
            self.assertIn(("vendor/submodule", "tracked_path_is_not_file"), warnings)


    def test_repo_map_reuses_cache_for_clean_same_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "def run():\n    return 'ok'\n",
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

            first = build_repository_map(repo, output_dir)
            second = build_repository_map(repo, output_dir)

            self.assertEqual(first["manifest"]["cache_status"], "rebuilt")
            self.assertEqual(second["manifest"]["cache_status"], "reused")
            self.assertEqual(second["manifest"]["repo_commit"], _git_rev_parse(repo, "HEAD"))
            self.assertEqual(second["manifest"]["working_tree_state"], "clean")
            self.assertEqual(first["inventory"], second["inventory"])
            self.assertEqual(first["symbols"], second["symbols"])


    def test_repo_grounding_detects_languages_project_tools_and_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "tests").mkdir()
            (repo / "src").mkdir()
            (repo / "pkg" / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            (repo / "tests" / "test_module.py").write_text("from pkg.module import run\n", encoding="utf-8")
            (repo / "src" / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
            (repo / "src" / "tool.ts").write_text("export const value = 1;\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
            (repo / "package.json").write_text('{"scripts":{"test":"vitest"}}\n', encoding="utf-8")
            (repo / "Makefile").write_text("test:\n\tpython3 -m unittest discover\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "add",
                    "pkg/module.py",
                    "tests/test_module.py",
                    "src/main.cpp",
                    "src/tool.ts",
                    "pyproject.toml",
                    "package.json",
                    "Makefile",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add mixed project"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            grounding = build_repo_grounding(repo)

            self.assertEqual(grounding["grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(grounding["scan_status"], "ok")
            languages = {item["language"]: item for item in grounding["languages"]}
            self.assertEqual(languages["python"]["file_count"], 2)
            self.assertEqual(languages["cpp"]["file_count"], 1)
            self.assertEqual(languages["typescript"]["file_count"], 1)
            tools = {item["tool_id"]: item for item in grounding["project_tools"]}
            self.assertEqual(tools["python-pyproject"]["path"], "pyproject.toml")
            self.assertEqual(tools["node-package-json"]["path"], "package.json")
            self.assertEqual(tools["make"]["path"], "Makefile")
            self.assertIn(
                {
                    "path": "tests/test_module.py",
                    "language": "python",
                    "test_framework_hint": "python",
                },
                grounding["test_entrypoints"],
            )
            commands = [item["command"] for item in grounding["candidate_verification_commands"]]
            self.assertIn(["python3", "-m", "unittest", "discover"], commands)
            self.assertIn(["npm", "test"], commands)
            structure = grounding["repository_structure"]
            self.assertEqual(structure["structure_schema_version"], "repo_structure.v1")
            self.assertEqual(structure["tracked_file_count"], 8)
            self.assertEqual(
                structure["category_counts"],
                [
                    {"category": "source", "file_count": 3},
                    {"category": "config", "file_count": 2},
                    {"category": "test", "file_count": 1},
                    {"category": "docs", "file_count": 1},
                    {"category": "build", "file_count": 1},
                ],
            )
            self.assertEqual(
                structure["top_level_entries"],
                [
                    {
                        "path": "Makefile",
                        "entry_type": "file",
                        "file_count": 1,
                        "category_counts": [{"category": "build", "file_count": 1}],
                        "language_counts": [{"language": "unknown", "file_count": 1}],
                    },
                    {
                        "path": "README.md",
                        "entry_type": "file",
                        "file_count": 1,
                        "category_counts": [{"category": "docs", "file_count": 1}],
                        "language_counts": [{"language": "unknown", "file_count": 1}],
                    },
                    {
                        "path": "package.json",
                        "entry_type": "file",
                        "file_count": 1,
                        "category_counts": [{"category": "config", "file_count": 1}],
                        "language_counts": [{"language": "unknown", "file_count": 1}],
                    },
                    {
                        "path": "pkg/",
                        "entry_type": "directory",
                        "file_count": 1,
                        "category_counts": [{"category": "source", "file_count": 1}],
                        "language_counts": [{"language": "python", "file_count": 1}],
                    },
                    {
                        "path": "pyproject.toml",
                        "entry_type": "file",
                        "file_count": 1,
                        "category_counts": [{"category": "config", "file_count": 1}],
                        "language_counts": [{"language": "unknown", "file_count": 1}],
                    },
                    {
                        "path": "src/",
                        "entry_type": "directory",
                        "file_count": 2,
                        "category_counts": [{"category": "source", "file_count": 2}],
                        "language_counts": [
                            {"language": "cpp", "file_count": 1},
                            {"language": "typescript", "file_count": 1},
                        ],
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


    def test_repo_grounding_budgets_top_level_structure_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_git_repo(repo)
            for name in ["alpha", "beta", "gamma", "delta"]:
                package_dir = repo / name
                package_dir.mkdir()
                (package_dir / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add top level packages"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            grounding = build_repo_grounding(repo, top_level_entry_limit=3)

            structure = grounding["repository_structure"]
            self.assertEqual(
                structure["top_level_entry_budget"],
                {
                    "max_entries": 3,
                    "total_entry_count": 5,
                    "included_count": 3,
                    "omitted_count": 2,
                },
            )
            self.assertEqual(
                [entry["path"] for entry in structure["top_level_entries"]],
                ["README.md", "alpha/", "beta/"],
            )
            self.assertEqual(structure["tracked_file_count"], 5)


    def test_repo_map_extracts_python_symbol_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "service.py").write_text(
                "\n".join(
                    [
                        "import os",
                        "from pathlib import Path",
                        "",
                        "CONSTANT = 'SECRET_BODY_MARKER'",
                        "",
                        "class Worker:",
                        "    def run(self):",
                        "        return Path(os.getcwd())",
                        "",
                        "def build_worker():",
                        "    return Worker()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "pkg/service.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add service"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            repo_map = build_repository_map(repo, output_dir)

            symbols_path = output_dir / "state" / "repo_map" / "symbols.json"
            self.assertEqual(repo_map["paths"]["symbols_path"], str(symbols_path))
            self.assertTrue(symbols_path.exists())
            self.assertEqual(repo_map["symbols"]["symbols_schema_version"], "repo_symbols.v1")
            symbols_by_path = {
                file_symbols["path"]: file_symbols
                for file_symbols in repo_map["symbols"]["files"]
            }
            service = symbols_by_path["pkg/service.py"]
            self.assertEqual(service["imports"], ["os", "pathlib.Path"])
            self.assertEqual(service["functions"], [{"name": "build_worker", "line": 10}])
            self.assertEqual(
                service["classes"],
                [{"name": "Worker", "line": 6, "methods": [{"name": "run", "line": 7}]}],
            )
            self.assertNotIn("SECRET_BODY_MARKER", json.dumps(repo_map["symbols"]))


    def test_repo_map_extracts_typescript_symbol_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)
            (repo / "src").mkdir()
            (repo / "src" / "service.ts").write_text(
                "\n".join(
                    [
                        "import React from 'react';",
                        "import { createClient } from './client';",
                        "",
                        "const SECRET_BODY_MARKER = 'hidden';",
                        "",
                        "export class DashboardService {",
                        "  loadData() {",
                        "    return createClient();",
                        "  }",
                        "}",
                        "",
                        "export function buildDashboard() {",
                        "  return new DashboardService();",
                        "}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "src/service.ts"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "add service"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            repo_map = build_repository_map(repo, output_dir)

            symbols_by_path = {
                file_symbols["path"]: file_symbols
                for file_symbols in repo_map["symbols"]["files"]
            }
            service = symbols_by_path["src/service.ts"]
            self.assertEqual(service["imports"], ["react", "./client"])
            self.assertEqual(service["functions"], [{"name": "buildDashboard", "line": 12}])
            self.assertEqual(
                service["classes"],
                [
                    {
                        "name": "DashboardService",
                        "line": 6,
                        "methods": [{"name": "loadData", "line": 7}],
                    }
                ],
            )
            self.assertNotIn("SECRET_BODY_MARKER", json.dumps(repo_map["symbols"]))


    def test_task_proposal_preserves_task_artifact_contract(self):
        handoff_path = ".agentteam/generated/repo_map_handoff.json"
        proposal = {
            "milestone_id": "M21",
            "tasks": [
                {
                    "task_id": "TASK-M21-REPO-MAP",
                    "objective": "Produce repo map handoff.",
                    "read_scope": ["."],
                    "write_scope": [".agentteam/generated/"],
                    "required_role": "repo_map_agent",
                    "risk_target": "L0",
                    "depends_on": [],
                    "blockers": [],
                    "expected_output_artifacts": [handoff_path],
                },
                {
                    "task_id": "TASK-M21-IMPLEMENT",
                    "objective": "Consume repo map handoff.",
                    "read_scope": ["src/"],
                    "write_scope": ["src/"],
                    "required_role": "implementation_worker",
                    "risk_target": "L1",
                    "depends_on": ["TASK-M21-REPO-MAP"],
                    "blockers": [],
                    "input_artifacts": [handoff_path],
                },
            ],
        }

        normalized = normalize_task_proposal(proposal)

        self.assertEqual(
            normalized["tasks"][0]["expected_output_artifacts"],
            [handoff_path],
        )
        self.assertEqual(
            normalized["tasks"][1]["input_artifacts"],
            [handoff_path],
        )


    def test_artifact_lint_passes_native_runtime_tree(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        summary = lint_artifacts(ROOT)

        self.assertEqual(summary["status"], "passed")
        self.assertGreaterEqual(summary["checked_json_files"], 1)
        self.assertGreaterEqual(summary["checked_jsonl_files"], 1)
        self.assertEqual(summary["errors"], [])


    def test_artifact_lint_reports_invalid_json(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_path = tmp_path / "broken.json"
            bad_path.write_text("{bad", encoding="utf-8")

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "invalid_json")
            self.assertEqual(summary["errors"][0]["path"], "broken.json")


    def test_artifact_lint_reports_invalid_event_type(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            schema_dir = tmp_path / "schemas"
            schema_dir.mkdir()
            (schema_dir / "event.schema.json").write_text(
                json.dumps(
                    {
                        "properties": {
                            "event_type": {
                                "enum": ["known_event"],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            event = _event_record("EVT-0001", 1)
            event["event_type"] = "unknown_event"
            (tmp_path / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "invalid_event_type")
            self.assertEqual(summary["errors"][0]["event_type"], "unknown_event")


    def test_artifact_lint_reports_missing_event_fields(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "events.jsonl").write_text(
                json.dumps({"event_type": "scheduler_started", "sequence": 1}) + "\n",
                encoding="utf-8",
            )

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "missing_event_fields")
            self.assertIn("event_id", summary["errors"][0]["missing_fields"])


    def test_artifact_lint_reports_non_monotonic_event_sequence(self):
        from agentteam_runtime.artifact_lint import lint_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events = [
                _event_record("EVT-0001", 1),
                _event_record("EVT-0003", 3),
            ]
            (tmp_path / "events.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            summary = lint_artifacts(tmp_path)

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["errors"][0]["kind"], "non_monotonic_event_sequence")
            self.assertEqual(summary["errors"][0]["expected_sequence"], 2)
            self.assertEqual(summary["errors"][0]["actual_sequence"], 3)


    def test_artifact_lint_cli_prints_summary(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "m0_runtime")

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.artifact_lint",
                "--root",
                str(ROOT),
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "passed")
        self.assertGreaterEqual(summary["checked_json_files"], 1)


    def test_ignored_runtime_artifact_is_persisted_and_materialized_for_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            (repo / ".git" / "info" / "exclude").write_text(
                ".agentteam/\n",
                encoding="utf-8",
            )
            artifact_path = ".agentteam/generated/repo_map_handoff.json"
            producer = _backlog_task(
                "TASK-REPO-MAP",
                write_scope=[".agentteam/generated/"],
            )
            producer["expected_output_artifacts"] = [artifact_path]
            consumer = _backlog_task(
                "TASK-IMPLEMENT",
                write_scope=["generated/"],
                depends_on=["TASK-REPO-MAP"],
            )
            consumer["input_artifacts"] = [artifact_path]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[producer, consumer],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                max_inflight=1,
                integrate_accepted_patch=True,
            )

            scheduler.dispatch_ready()
            producer_inflight = scheduler.state["inflight_attempts"][0]
            producer_artifact = (
                Path(producer_inflight["worktree_path"]) / artifact_path
            )
            producer_artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact_text = '{"schema_version":"repo_map_handoff.v1"}\n'
            producer_artifact.write_text(artifact_text, encoding="utf-8")
            _append_runtime_result(
                producer_inflight["outbox_path"],
                producer_inflight["message_id"],
                producer_inflight["task_id"],
                producer_inflight["attempt_id"],
                producer_inflight["lease_id"],
                "completed",
                [artifact_path],
            )

            producer_result = scheduler.collect_ready_results()["results"][0]
            dispatch = scheduler.dispatch_ready()
            consumer_inflight = scheduler.state["inflight_attempts"][0]
            consumer_artifact = (
                Path(consumer_inflight["worktree_path"]) / artifact_path
            )
            manifest = json.loads(
                (output_dir / "runtime_artifacts" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0002-TASK-IMPLEMENT"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )

            self.assertEqual(producer_result["validation_status"], "accepted")
            self.assertEqual(producer_result["task_status"], "done")
            self.assertEqual(producer_result["patch_path"], None)
            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-IMPLEMENT"])
            self.assertEqual(
                consumer_artifact.read_text(encoding="utf-8"),
                artifact_text,
            )
            self.assertEqual(
                manifest["artifacts"][artifact_path]["task_id"],
                "TASK-REPO-MAP",
            )
            self.assertEqual(
                message["payload"]["materialized_input_artifacts"][0][
                    "materialization_status"
                ],
                "materialized",
            )


    def _write_runtime_artifact_attempt(self, inflight, artifact_path, content):
        worktree = Path(inflight["worktree_path"])
        artifact = worktree / artifact_path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(content, encoding="utf-8")
        changed = worktree / "generated" / "change.txt"
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text("integration candidate\n", encoding="utf-8")
        _append_runtime_result(
            inflight["outbox_path"],
            inflight["message_id"],
            inflight["task_id"],
            inflight["attempt_id"],
            inflight["lease_id"],
            "completed",
            [artifact_path, "generated/change.txt"],
        )


    def test_runtime_artifact_materialization_rejects_store_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            (repo / ".git" / "info" / "exclude").write_text(
                ".agentteam/\n",
                encoding="utf-8",
            )
            artifact_path = ".agentteam/generated/repo_map_handoff.json"
            producer = _backlog_task(
                "TASK-REPO-MAP",
                write_scope=[".agentteam/generated/"],
            )
            producer["expected_output_artifacts"] = [artifact_path]
            consumer = _backlog_task(
                "TASK-IMPLEMENT",
                write_scope=["generated/"],
                depends_on=["TASK-REPO-MAP"],
            )
            consumer["input_artifacts"] = [artifact_path]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[producer, consumer],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                max_inflight=1,
                integrate_accepted_patch=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            artifact = Path(inflight["worktree_path"]) / artifact_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('{"valid":true}\n', encoding="utf-8")
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [artifact_path],
            )
            scheduler.collect_ready_results()
            manifest = json.loads(
                (output_dir / "runtime_artifacts" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            stored = (
                output_dir
                / "runtime_artifacts"
                / "objects"
                / manifest["artifacts"][artifact_path]["sha256"]
            )
            stored.write_text('{"tampered":true}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "input artifact digest mismatch",
            ):
                scheduler.dispatch_ready()


    def test_runtime_artifact_validation_rejects_wrong_dependency_producer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            output_dir = tmp_path / "run"
            artifact_path = ".agentteam/generated/repo_map_handoff.json"
            artifact = worktree / artifact_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('{"producer":"one"}\n', encoding="utf-8")
            persist_runtime_artifacts(
                output_dir,
                worktree,
                {
                    artifact_path: hashlib.sha256(
                        artifact.read_bytes()
                    ).hexdigest()
                },
                task_id="TASK-PRODUCER",
                attempt_id="ATTEMPT-001",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "input artifact producer mismatch",
            ):
                validate_runtime_input_artifacts(
                    output_dir,
                    {artifact_path: "TASK-OTHER"},
                )


    def test_runtime_artifact_persistence_binds_audited_digest_and_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            worktree = tmp_path / "worktree"
            output_dir = tmp_path / "run"
            artifact_path = ".agentteam/generated/repo_map_handoff.json"
            artifact = worktree / artifact_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text('{"version":1}\n', encoding="utf-8")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            persist_runtime_artifacts(
                output_dir,
                worktree,
                {artifact_path: digest},
                task_id="TASK-PRODUCER",
                attempt_id="ATTEMPT-001",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "runtime artifact record is immutable",
            ):
                persist_runtime_artifacts(
                    output_dir,
                    worktree,
                    {artifact_path: digest},
                    task_id="TASK-PRODUCER",
                    attempt_id="ATTEMPT-002",
                )
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime artifact changed after audit",
            ):
                persist_runtime_artifacts(
                    output_dir,
                    worktree,
                    {artifact_path: "0" * 64},
                    task_id="TASK-OTHER",
                    attempt_id="ATTEMPT-001",
                )


    def test_static_repository_input_artifact_does_not_require_runtime_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            blueprint = repo / "blueprint.json"
            blueprint.write_text('{"task":"static input"}\n', encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", "blueprint.json"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "add blueprint"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            task = _backlog_task(
                "TASK-IMPLEMENT",
                write_scope=["generated/"],
            )
            task["input_artifacts"] = ["blueprint.json"]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[task],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
            )

            dispatch = scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            message = _read_first_jsonl(
                Path(inflight["outbox_path"]).with_name("inbox.jsonl")
            )

            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-IMPLEMENT"])
            self.assertTrue(
                (Path(inflight["worktree_path"]) / "blueprint.json").is_file()
            )
            self.assertEqual(message["payload"]["input_artifacts"], ["blueprint.json"])
            self.assertEqual(message["payload"]["materialized_input_artifacts"], [])


    def test_expected_runtime_artifact_is_an_exact_additional_write_scope(self):
        artifact_path = ".agentteam/generated/repo_map_handoff.json"
        task = {
            "write_scope": ["generated/"],
            "expected_output_artifacts": [artifact_path],
        }
        result = {
            "result_status": "completed",
            "changed_files": [artifact_path],
            "output": {},
        }

        outcome = classify_attempt_outcome(
            result,
            task,
            diff_audit={"diff_status": "matched"},
        )

        self.assertEqual(outcome["validation_status"], "accepted")
        self.assertEqual(outcome["failure_category"], None)


    def test_codex_runtime_adapter_includes_task_artifact_contract(self):
        handoff_path = ".agentteam/generated/repo_map_handoff.json"
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-implementation-worker-1",
            "message_type": "dispatch_task",
            "correlation_id": "TASK-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "TASK-001",
                "attempt_id": "ATTEMPT-001",
                "lease_id": "LEASE-001",
                "objective": "Implement using repo map handoff.",
                "read_scope": ["src/"],
                "write_scope": ["src/"],
                "input_artifacts": [handoff_path],
                "expected_output_artifacts": [handoff_path],
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("Task artifact contract:", prompt)
        self.assertIn("Read each input_artifacts path before relying on repo context.", prompt)
        self.assertIn("Write each expected_output_artifacts path when it is part of the task deliverables.", prompt)
        self.assertIn(handoff_path, prompt)

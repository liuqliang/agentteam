import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentteam_runtime import (
    CodexRuntimeAdapter,
    FakeRuntimeAdapter,
    FileSchedulerDaemon,
    FileMailboxExternalRuntimeAdapter,
    FileMailboxRuntimeAdapter,
    FileMailboxSubprocessRuntimeAdapter,
    FileMailboxWorkerProcessSupervisor,
    FileMailboxWorkerPoolSupervisor,
    FileMailboxWorker,
    ShellRuntimeAdapter,
    TwoPhaseFileScheduler,
    audit_worktree_diff,
    build_planner_context,
    build_repo_context,
    build_repository_map,
    build_runtime_observability,
    classify_attempt_outcome,
    normalize_task_proposal,
    read_integration_batches,
    read_integration_queue,
    read_scheduler_state_index,
    replay_events,
    run_file_daemon,
    run_scheduler_loop,
    run_simulation,
    verify_integration_batch,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "schemas"


class FixedClock:
    def __init__(self):
        self._ticks = iter(
            f"2026-05-31T00:00:{second:02d}Z"
            for second in range(60)
        )

    def now(self):
        return next(self._ticks)


class M0RuntimeTests(unittest.TestCase):
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

    def test_repo_map_rebuilds_cache_for_dirty_or_unversioned_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)

            first = build_repository_map(repo, output_dir)
            (repo / "scratch.py").write_text("print('untracked')\n", encoding="utf-8")
            second = build_repository_map(repo, output_dir)

            files = {entry["path"]: entry for entry in second["inventory"]["files"]}
            warnings = [warning["warning"] for warning in second["manifest"]["warnings"]]
            self.assertEqual(first["manifest"]["cache_status"], "rebuilt")
            self.assertEqual(second["manifest"]["cache_status"], "rebuilt")
            self.assertEqual(second["manifest"]["working_tree_state"], "dirty_or_unversioned")
            self.assertIn("working_tree_dirty", warnings)
            self.assertNotIn("scratch.py", files)

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

    def test_run_simulation_dispatches_ready_task_and_validates_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )

            self.assertEqual(result["task_id"], "TASK-001")
            self.assertEqual(result["attempt_id"], "ATTEMPT-001")
            self.assertEqual(result["lease_id"], "LEASE-001")
            self.assertEqual(result["message_id"], "MSG-0001")
            self.assertEqual(result["worktree_id"], "WT-ATTEMPT-001")
            self.assertEqual(result["validation_status"], "accepted")

            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            self.assertTrue(inbox.exists())
            message = json.loads(inbox.read_text(encoding="utf-8").strip())
            self.assertEqual(message["message_type"], "dispatch_task")
            self.assertEqual(message["payload"]["attempt_id"], "ATTEMPT-001")
            self.assertEqual(message["payload"]["worktree_id"], "WT-ATTEMPT-001")

    def test_run_simulation_dispatch_includes_role_prompt_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_role_prompt_contracts(
                agent_pool_path,
                role_prompt_contracts={
                    "repo_map_agent": {
                        "role_summary": "Inspect repository context before editing.",
                        "instructions": [
                            "Keep changes inside write_scope.",
                            "Report evidence in output.evidence.",
                        ],
                        "required_output_keys": ["evidence"],
                    }
                },
            )

            run_simulation(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            message = _read_first_jsonl(
                output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            )
            contract = message["payload"]["role_prompt_contract"]

            self.assertEqual(message["payload"]["agent_role"], "repo_map_agent")
            self.assertEqual(
                contract["role_summary"],
                "Inspect repository context before editing.",
            )
            self.assertEqual(contract["required_output_keys"], ["evidence"])

    def test_run_simulation_dispatch_writes_role_context_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            artifact = tmp_path / "role-context.md"
            artifact.write_text(
                "\n".join(
                    [
                        "# Role Context",
                        "Use this compact context.",
                        *["bounded line" for _ in range(20)],
                        "TAIL_SHOULD_BE_OMITTED",
                    ]
                ),
                encoding="utf-8",
            )
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_role_context_packages(
                agent_pool_path,
                role_context_packages={
                    "repo_map_agent": {
                        "context_artifacts": [str(artifact)],
                        "excerpt_chars": 80,
                        "context_notes": ["Prefer existing local helpers."],
                    }
                },
            )

            run_simulation(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            message = _read_first_jsonl(
                output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            )
            context_path = Path(message["payload"]["role_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))
            source = context["artifact_context"]["sources"][0]

            self.assertTrue(context_path.exists())
            self.assertEqual(context["context_schema_version"], "role_context.v1")
            self.assertEqual(context["agent_role"], "repo_map_agent")
            self.assertEqual(context["context_notes"], ["Prefer existing local helpers."])
            self.assertEqual(source["headings"], ["Role Context"])
            self.assertLessEqual(source["excerpt_chars"], 80)
            self.assertNotIn("TAIL_SHOULD_BE_OMITTED", json.dumps(context))

    def test_run_simulation_dispatch_includes_repo_context_path_when_project_root_is_available(self):
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

            message = _read_first_jsonl(
                output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            )
            context_path = Path(message["payload"]["repo_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))

            self.assertEqual(
                message["payload"]["repo_context_schema_version"],
                "repo_context.v1",
            )
            self.assertTrue(context_path.exists())
            self.assertEqual(context["repo_context_schema_version"], "repo_context.v1")
            self.assertEqual(context["selected_files"][0]["path"], "pkg/module.py")

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

    def test_scheduler_loop_runs_ready_tasks_until_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            state = json.loads(Path(summary["state_path"]).read_text(encoding="utf-8"))
            statuses = {
                item["task_id"]: item["backlog_status"]
                for item in state["backlog"]["items"]
            }

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(statuses["TASK-001"], "done")
            self.assertEqual(statuses["TASK-002"], "done")
            self.assertTrue((output_dir / "steps" / "STEP-0001-TASK-001").exists())
            self.assertTrue((output_dir / "steps" / "STEP-0002-TASK-002").exists())

    def test_file_daemon_tick_records_worker_registry_and_processes_one_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            daemon = FileSchedulerDaemon(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            summary = daemon.tick()

            registry = json.loads(
                (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["daemon_status"], "running")
            self.assertEqual(summary["tick_status"], "processed")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(
                summary["worker_registry_path"],
                str(output_dir / "state" / "worker_registry.json"),
            )
            self.assertEqual(registry["tick_count"], 1)
            self.assertEqual(registry["registry_status"], "active")
            self.assertEqual(
                [worker["agent_id"] for worker in registry["workers"]],
                ["agent-repo-map", "agent-worker-1"],
            )
            self.assertEqual(
                {worker["worker_status"] for worker in registry["workers"]},
                {"idle"},
            )
            self.assertTrue((output_dir / "steps" / "STEP-0001-TASK-001").exists())

    def test_file_daemon_run_until_idle_reuses_worker_registry_across_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_file_daemon(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            registry = json.loads(
                (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(summary["tick_count"], 3)
            self.assertEqual(registry["tick_count"], 3)
            self.assertEqual(registry["registry_status"], "active")

    def test_file_mailbox_worker_poll_once_writes_runtime_result_to_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=FakeRuntimeAdapter(),
                clock=FixedClock(),
            )
            summary = worker.poll_once()

            result_message = _read_first_jsonl(outbox)

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["source_message_id"], "MSG-MAILBOX-001")
            self.assertEqual(result_message["message_type"], "runtime_result")
            self.assertEqual(
                result_message["payload"]["source_message_id"],
                "MSG-MAILBOX-001",
            )
            self.assertEqual(result_message["payload"]["result_status"], "completed")
            self.assertEqual(
                result_message["payload"]["changed_files"],
                ["generated/m0_generated_repo_index.json"],
            )

    def test_file_mailbox_worker_cli_processes_one_message_in_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-SUBPROCESS-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.mailbox_worker",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--output-dir",
                    str(output_dir),
                    "--agent-id",
                    "agent-repo-map",
                    "--message-id",
                    "MSG-SUBPROCESS-001",
                    "--runtime",
                    "fake",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_message = _read_first_jsonl(outbox)

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["source_message_id"], "MSG-SUBPROCESS-001")
            self.assertNotEqual(summary["worker_pid"], os.getpid())
            self.assertEqual(completed.stderr, "")
            self.assertEqual(result_message["message_type"], "runtime_result")

    def test_file_mailbox_worker_cli_can_use_codex_delegate_from_payload_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_mailbox.py"
            target_file = "generated/mailbox_codex_delegate.json"
            _init_git_repo(repo)
            _write_fake_codex(fake_codex, changed_file=target_file)
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-CODEX-MAILBOX-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            message["payload"]["worktree_path"] = str(repo)
            _append_test_jsonl(inbox, [message])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.mailbox_worker",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--output-dir",
                    str(output_dir),
                    "--agent-id",
                    "agent-repo-map",
                    "--message-id",
                    "MSG-CODEX-MAILBOX-001",
                    "--runtime",
                    "codex",
                    "--codex-command-json",
                    json.dumps([sys.executable, str(fake_codex)]),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_message = _read_first_jsonl(outbox)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["result_status"], "completed")
            self.assertTrue((repo / target_file).exists())
            self.assertEqual(result_message["payload"]["output"]["adapter"], "codex")

    def test_scheduler_loop_can_round_trip_runtime_result_through_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FileMailboxRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    runtime_adapter=FakeRuntimeAdapter(),
                    clock=FixedClock(),
                ),
            )

            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())
            self.assertEqual(_read_first_jsonl(first_outbox)["message_type"], "runtime_result")
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxRuntimeAdapter"},
            )

    def test_scheduler_loop_can_run_mailbox_worker_as_one_shot_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FileMailboxSubprocessRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    timeout_seconds=30,
                ),
            )
            state = read_scheduler_state_index(output_dir)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxSubprocessRuntimeAdapter"},
            )

    def test_scheduler_loop_can_use_long_running_mailbox_worker_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            supervisor = FileMailboxWorkerProcessSupervisor(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                env=env,
                poll_interval_seconds=0.01,
            )

            start = supervisor.start()
            try:
                summary = run_scheduler_loop(
                    FIXTURES / "sample_agent_pool.json",
                    backlog_path,
                    output_dir,
                    clock=FixedClock(),
                    runtime_adapter=FileMailboxExternalRuntimeAdapter(
                        FIXTURES / "sample_agent_pool.json",
                        timeout_seconds=5,
                        poll_interval_seconds=0.01,
                    ),
                )
                self.assertIsNone(supervisor.process.poll())
            finally:
                stop = supervisor.stop()

            state = read_scheduler_state_index(output_dir)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            second_outbox = (
                output_dir
                / "steps"
                / "STEP-0002-TASK-002"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(start["worker_status"], "running")
            self.assertEqual(stop["worker_status"], "stopped")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertNotEqual(start["worker_pid"], os.getpid())
            self.assertTrue(first_outbox.exists())
            self.assertTrue(second_outbox.exists())
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxExternalRuntimeAdapter"},
            )

    def test_file_mailbox_worker_process_supervisor_reports_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            supervisor = FileMailboxWorkerProcessSupervisor(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
                env=env,
                poll_interval_seconds=0.01,
            )

            before = supervisor.health()
            start = supervisor.start()
            try:
                running = supervisor.health()
            finally:
                stop = supervisor.stop()
            stopped = supervisor.health()

            self.assertEqual(before["worker_status"], "not_started")
            self.assertEqual(running["worker_status"], "running")
            self.assertEqual(running["worker_pid"], start["worker_pid"])
            self.assertEqual(running["exit_code"], None)
            self.assertEqual(stop["worker_status"], "stopped")
            self.assertEqual(stopped["worker_status"], "exited")
            self.assertEqual(stopped["exit_code"], 0)

    def test_scheduler_loop_writes_canonical_event_log_for_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            events_path = Path(summary["events_path"])
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
            ]
            event_schema = json.loads((SCHEMAS / "event.schema.json").read_text(encoding="utf-8"))
            allowed_event_keys = set(event_schema["properties"].keys())
            snapshot = replay_events(events_path)

            self.assertEqual(events_path, output_dir / "events.jsonl")
            self.assertTrue(all(set(event.keys()).issubset(allowed_event_keys) for event in events))
            self.assertEqual(
                [event["sequence"] for event in events],
                list(range(1, len(events) + 1)),
            )
            self.assertEqual(events[0]["event_id"], "EVT-0001")
            self.assertEqual(
                {event["step_id"] for event in events},
                {"STEP-0001-TASK-001", "STEP-0002-TASK-002"},
            )
            self.assertTrue(
                all(event["source_event_id"].startswith("EVT-") for event in events)
            )
            self.assertEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(snapshot["tasks"]["TASK-002"]["task_status"], "done")

    def test_scheduler_loop_uses_task_scoped_lease_and_message_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            snapshot = replay_events(summary["events_path"])
            first_message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            second_message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0002-TASK-002"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )

            self.assertEqual(
                set(snapshot["leases"].keys()),
                {"TASK-001-LEASE-001", "TASK-002-LEASE-001"},
            )
            self.assertEqual(first_message["message_id"], "TASK-001-MSG-0001")
            self.assertEqual(first_message["payload"]["lease_id"], "TASK-001-LEASE-001")
            self.assertEqual(second_message["message_id"], "TASK-002-MSG-0001")
            self.assertEqual(second_message["payload"]["lease_id"], "TASK-002-LEASE-001")

    def test_scheduler_loop_writes_sqlite_state_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            db_path = Path(summary["state_db_path"])
            root_event_count = len(
                Path(summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            with sqlite3.connect(db_path) as connection:
                tasks = connection.execute(
                    "select task_id, task_status from tasks order by task_id"
                ).fetchall()
                attempts = connection.execute(
                    "select attempt_id, task_id, attempt_status from attempts order by attempt_id"
                ).fetchall()
                leases = connection.execute(
                    "select lease_id, lease_status from leases order by lease_id"
                ).fetchall()
                runtime_sessions = connection.execute(
                    """
                    select runtime_session_id, task_id, attempt_id, session_status, result_status
                    from runtime_sessions
                    order by runtime_session_id
                    """
                ).fetchall()
                event_count = connection.execute("select count(*) from events").fetchone()[0]

            self.assertTrue(db_path.exists())
            self.assertEqual(tasks, [("TASK-001", "done"), ("TASK-002", "done")])
            self.assertEqual(
                attempts,
                [
                    ("TASK-001-ATTEMPT-001", "TASK-001", "completed"),
                    ("TASK-002-ATTEMPT-001", "TASK-002", "completed"),
                ],
            )
            self.assertEqual(
                leases,
                [
                    ("TASK-001-LEASE-001", "released"),
                    ("TASK-002-LEASE-001", "released"),
                ],
            )
            self.assertEqual(
                runtime_sessions,
                [
                    (
                        "SESSION-TASK-001-ATTEMPT-001",
                        "TASK-001",
                        "TASK-001-ATTEMPT-001",
                        "stopped",
                        "completed",
                    ),
                    (
                        "SESSION-TASK-002-ATTEMPT-001",
                        "TASK-002",
                        "TASK-002-ATTEMPT-001",
                        "stopped",
                        "completed",
                    ),
                ],
            )
            self.assertEqual(event_count, root_event_count)

    def test_read_scheduler_state_index_returns_query_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            state = read_scheduler_state_index(output_dir)
            root_event_count = len(
                Path(summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            self.assertEqual(state["state_db_path"], summary["state_db_path"])
            self.assertEqual(state["events_path"], summary["events_path"])
            self.assertEqual(
                state["tasks"],
                [
                    {"task_id": "TASK-001", "task_status": "done"},
                    {"task_id": "TASK-002", "task_status": "done"},
                ],
            )
            self.assertEqual(
                state["attempts"],
                [
                    {
                        "attempt_id": "TASK-001-ATTEMPT-001",
                        "attempt_status": "completed",
                        "repo_context_path": None,
                        "task_id": "TASK-001",
                        "validation_status": "accepted",
                    },
                    {
                        "attempt_id": "TASK-002-ATTEMPT-001",
                        "attempt_status": "completed",
                        "repo_context_path": None,
                        "task_id": "TASK-002",
                        "validation_status": "accepted",
                    },
                ],
            )
            self.assertEqual(
                state["runtime_sessions"],
                [
                    {
                        "attempt_id": "TASK-001-ATTEMPT-001",
                        "changed_file_count": 1,
                        "lease_id": "TASK-001-LEASE-001",
                        "result_status": "completed",
                        "runtime_adapter": "FakeRuntimeAdapter",
                        "runtime_model": None,
                        "runtime_sandbox": None,
                        "runtime_session_id": "SESSION-TASK-001-ATTEMPT-001",
                        "runtime_timeout_seconds": None,
                        "session_status": "stopped",
                        "task_id": "TASK-001",
                    },
                    {
                        "attempt_id": "TASK-002-ATTEMPT-001",
                        "changed_file_count": 1,
                        "lease_id": "TASK-002-LEASE-001",
                        "result_status": "completed",
                        "runtime_adapter": "FakeRuntimeAdapter",
                        "runtime_model": None,
                        "runtime_sandbox": None,
                        "runtime_session_id": "SESSION-TASK-002-ATTEMPT-001",
                        "runtime_timeout_seconds": None,
                        "session_status": "stopped",
                        "task_id": "TASK-002",
                    },
                ],
            )
            self.assertEqual(state["event_count"], root_event_count)
            self.assertEqual(state["latest_event"]["event_type"], "backlog_updated")
            self.assertEqual(state["latest_event"]["task_id"], "TASK-002")

    def test_scheduler_state_index_records_runtime_session_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_runtime_config.py"
            _init_git_repo(repo)
            _write_fake_codex_arg_recorder(
                fake_codex,
                changed_file="generated/runtime_config.json",
            )
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(
                    command=[sys.executable, str(fake_codex)],
                    model="gpt-runtime-config",
                    sandbox="read-only",
                    timeout_seconds=30,
                ),
            )

            state = read_scheduler_state_index(output_dir)
            session = state["runtime_sessions"][0]

            self.assertEqual(session["runtime_adapter"], "CodexRuntimeAdapter")
            self.assertEqual(session["runtime_model"], "gpt-runtime-config")
            self.assertEqual(session["runtime_sandbox"], "read-only")
            self.assertEqual(session["runtime_timeout_seconds"], 30)

    def test_scheduler_core_uses_agent_runtime_profile_without_cli_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_core_profile.py"
            target_file = "generated/core_profile.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_runtime_profile(
                agent_pool_path,
                runtime_profile={
                    "adapter": "codex",
                    "command": [sys.executable, str(fake_codex)],
                    "model": "core-profile-model",
                    "sandbox": "read-only",
                    "timeout_seconds": 30,
                },
            )
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)

            run_scheduler_loop(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
            )

            state = read_scheduler_state_index(output_dir)
            session = state["runtime_sessions"][0]
            recorded_path = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "worktrees"
                / "WT-TASK-001-ATTEMPT-001"
                / target_file
            )
            self.assertTrue(recorded_path.exists())
            recorded = json.loads(recorded_path.read_text(encoding="utf-8"))

            self.assertEqual(session["runtime_adapter"], "CodexRuntimeAdapter")
            self.assertEqual(session["runtime_model"], "core-profile-model")
            self.assertEqual(session["runtime_sandbox"], "read-only")
            self.assertEqual(session["runtime_timeout_seconds"], 30)
            self.assertEqual(recorded["model"], "core-profile-model")
            self.assertEqual(recorded["sandbox"], "read-only")

    def test_scheduler_core_uses_role_runtime_profile_when_agent_profile_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_role_profile.py"
            target_file = "generated/role_profile.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_role_runtime_profiles(
                agent_pool_path,
                role_runtime_profiles={
                    "repo_map_agent": {
                        "adapter": "codex",
                        "command": [sys.executable, str(fake_codex)],
                        "model": "role-profile-model",
                        "sandbox": "read-only",
                        "timeout_seconds": 45,
                    }
                },
            )
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)

            run_scheduler_loop(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
            )

            state = read_scheduler_state_index(output_dir)
            session = state["runtime_sessions"][0]
            recorded_path = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "worktrees"
                / "WT-TASK-001-ATTEMPT-001"
                / target_file
            )
            self.assertTrue(recorded_path.exists())
            recorded = json.loads(recorded_path.read_text(encoding="utf-8"))

            self.assertEqual(session["runtime_adapter"], "CodexRuntimeAdapter")
            self.assertEqual(session["runtime_model"], "role-profile-model")
            self.assertEqual(session["runtime_sandbox"], "read-only")
            self.assertEqual(session["runtime_timeout_seconds"], 45)
            self.assertEqual(recorded["model"], "role-profile-model")
            self.assertEqual(recorded["sandbox"], "read-only")

    def test_read_scheduler_state_index_rebuilds_stale_sqlite_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            root_event_count = len(
                Path(summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            with sqlite3.connect(summary["state_db_path"]) as connection:
                connection.execute("delete from tasks where task_id = ?", ("TASK-002",))
                connection.execute(
                    "delete from events where sequence = (select max(sequence) from events)"
                )

            state = read_scheduler_state_index(output_dir)

            self.assertEqual(
                state["tasks"],
                [
                    {"task_id": "TASK-001", "task_status": "done"},
                    {"task_id": "TASK-002", "task_status": "done"},
                ],
            )
            self.assertEqual(state["event_count"], root_event_count)
            self.assertEqual(state["latest_event"]["task_id"], "TASK-002")

    def test_read_scheduler_state_index_rebuilds_missing_runtime_session_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            db_path = output_dir / "state" / "scheduler_state.sqlite"
            with sqlite3.connect(db_path) as connection:
                connection.execute("drop table runtime_sessions")

            state = read_scheduler_state_index(output_dir)

            self.assertEqual(len(state["runtime_sessions"]), 2)
            self.assertEqual(
                state["runtime_sessions"][0]["runtime_session_id"],
                "SESSION-TASK-001-ATTEMPT-001",
            )

    def test_scheduler_loop_respects_done_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task(
                        "TASK-001",
                        write_scope=["generated/task-001/"],
                        status="done",
                    ),
                    _backlog_task(
                        "TASK-002",
                        write_scope=["generated/task-002/"],
                        depends_on=["TASK-001"],
                    ),
                    _backlog_task(
                        "TASK-003",
                        write_scope=["generated/task-003/"],
                        depends_on=["TASK-MISSING"],
                    ),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )

            state = json.loads(Path(summary["state_path"]).read_text(encoding="utf-8"))
            statuses = {
                item["task_id"]: item["backlog_status"]
                for item in state["backlog"]["items"]
            }

            self.assertEqual(summary["processed_task_ids"], ["TASK-002"])
            self.assertEqual(statuses["TASK-001"], "done")
            self.assertEqual(statuses["TASK-002"], "done")
            self.assertEqual(statuses["TASK-003"], "ready")

    def test_scheduler_loop_resumes_from_persisted_state(self):
        class RecordingRuntimeAdapter:
            def __init__(self):
                self.task_ids = []

            def run(self, message, worktree_path=None):
                self.task_ids.append(message["payload"]["task_id"])
                return {
                    "result_status": "completed",
                    "changed_files": message["payload"]["write_scope"],
                    "output": {"adapter": "recording"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            adapter = RecordingRuntimeAdapter()
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            first_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=adapter,
                max_steps=1,
            )
            second_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=adapter,
            )

            self.assertEqual(first_summary["scheduler_status"], "max_steps_reached")
            self.assertEqual(second_summary["scheduler_status"], "idle")
            self.assertEqual(second_summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(adapter.task_ids, ["TASK-001", "TASK-002"])

    def test_scheduler_loop_uses_task_scoped_worktree_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            branches = [step["result"]["branch"] for step in summary["steps"]]
            worktree_ids = [step["result"]["worktree_id"] for step in summary["steps"]]

            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                branches,
                ["agentteam/TASK-001-ATTEMPT-001", "agentteam/TASK-002-ATTEMPT-001"],
            )
            self.assertEqual(
                worktree_ids,
                ["WT-TASK-001-ATTEMPT-001", "WT-TASK-002-ATTEMPT-001"],
            )

    def test_replay_reconstructs_done_task_only_after_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "completed")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["validation_status"], "accepted")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["worktree_id"], "WT-ATTEMPT-001")
            self.assertEqual(snapshot["leases"]["LEASE-001"]["lease_status"], "released")

    def test_run_simulation_records_runtime_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )
            events = [
                json.loads(line)
                for line in Path(result["events_path"]).read_text(encoding="utf-8").splitlines()
            ]
            session_events = [
                event
                for event in events
                if event["event_type"].startswith("runtime_session_")
            ]
            snapshot = replay_events(result["events_path"])

            self.assertEqual(result["runtime_session_id"], "SESSION-ATTEMPT-001")
            self.assertEqual(result["runtime_session_status"], "stopped")
            self.assertEqual(
                [event["event_type"] for event in session_events],
                [
                    "runtime_session_started",
                    "runtime_session_observed",
                    "runtime_session_stopped",
                ],
            )
            self.assertTrue(
                all(
                    event["payload"]["runtime_session_id"] == "SESSION-ATTEMPT-001"
                    and event["payload"]["task_id"] == "TASK-001"
                    and event["payload"]["attempt_id"] == "ATTEMPT-001"
                    and event["payload"]["lease_id"] == "LEASE-001"
                    for event in session_events
                )
            )
            self.assertEqual(
                snapshot["runtime_sessions"]["SESSION-ATTEMPT-001"]["session_status"],
                "stopped",
            )
            self.assertEqual(
                snapshot["runtime_sessions"]["SESSION-ATTEMPT-001"]["result_status"],
                "completed",
            )

    def test_emitted_types_are_allowed_by_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                FIXTURES / "sample_backlog.json",
                output_dir,
                clock=FixedClock(),
            )

            event_schema = json.loads((SCHEMAS / "event.schema.json").read_text(encoding="utf-8"))
            allowed_events = set(event_schema["properties"]["event_type"]["enum"])
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue({event["event_type"] for event in events}.issubset(allowed_events))

            message_schema = json.loads(
                (SCHEMAS / "mailbox_message.schema.json").read_text(encoding="utf-8")
            )
            allowed_messages = set(message_schema["properties"]["message_type"]["enum"])
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            messages = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines()]
            self.assertTrue({message["message_type"] for message in messages}.issubset(allowed_messages))

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

    def test_cli_runs_simulation_and_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(FIXTURES / "sample_backlog.json"),
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(summary["task_id"], "TASK-001")
            self.assertTrue((output_dir / "events.jsonl").exists())

    def test_cli_can_run_scheduler_loop_until_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--run-until-idle",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = json.loads(Path(summary["state_path"]).read_text(encoding="utf-8"))
            statuses = {
                item["task_id"]: item["backlog_status"]
                for item in state["backlog"]["items"]
            }

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(statuses["TASK-001"], "done")
            self.assertEqual(statuses["TASK-002"], "done")
            self.assertEqual(summary["snapshot"]["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(summary["snapshot"]["tasks"]["TASK-002"]["task_status"], "done")
            self.assertEqual(
                set(summary["snapshot"]["leases"].keys()),
                {"TASK-001-LEASE-001", "TASK-002-LEASE-001"},
            )

    def test_cli_can_run_file_daemon_until_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue((output_dir / "state" / "worker_registry.json").exists())

    def test_cli_can_run_file_daemon_with_mailbox_worker_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-mailbox-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())

    def test_cli_can_run_file_daemon_with_mailbox_subprocess_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-mailbox-subprocess-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxSubprocessRuntimeAdapter"},
            )

    def test_cli_can_run_file_daemon_with_long_running_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
            self.assertEqual(summary["worker_process"]["stderr"], "")
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxExternalRuntimeAdapter"},
            )

    def test_cli_can_run_file_daemon_with_long_running_codex_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_long_worker.py"
            target_file = "generated/long_worker_codex_delegate.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                    "--runtime",
                    "codex",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            worktree_path = Path(
                summary["snapshot"]["attempts"]["TASK-001-ATTEMPT-001"]["worktree_path"]
            )

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
            self.assertEqual(summary["worker_process"]["worker_runtime"], "codex")
            self.assertEqual(summary["worker_process"]["stderr"], "")
            self.assertTrue((worktree_path / target_file).exists())
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-TASK-001-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "FileMailboxExternalRuntimeAdapter",
            )

    def test_cli_long_running_mailbox_worker_accepts_agent_id_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "custom_agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_id(agent_pool_path, "agent-custom-map")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                    "--daemon-long-running-worker-agent-id",
                    "agent-custom-map",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            custom_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-custom-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_process"]["worker_agent_id"], "agent-custom-map")
            self.assertTrue(custom_outbox.exists())

    def test_file_mailbox_worker_pool_supervisor_starts_and_stops_all_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            try:
                self.assertEqual(start["pool_status"], "running")
                self.assertEqual(start["worker_count"], 2)
                self.assertEqual(
                    {worker["worker_agent_id"] for worker in start["workers"]},
                    {"agent-repo-map", "agent-doc-map"},
                )
                self.assertTrue(Path(start["process_registry_path"]).exists())
                self.assertTrue(
                    all(worker["worker_pid"] != os.getpid() for worker in start["workers"])
                )
            finally:
                stop = pool.stop()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(stop["pool_status"], "stopped")
            self.assertEqual(stop["worker_count"], 2)
            self.assertEqual(registry["registry_status"], "stopped")
            self.assertEqual(
                {worker["worker_agent_id"] for worker in stop["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(
                all(worker["worker_status"] == "stopped" for worker in stop["workers"])
            )

    def test_file_mailbox_worker_pool_supervisor_uses_role_runtime_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_worker_profile.py"
            _write_agent_pool_with_role_runtime_profiles(
                agent_pool_path,
                role_runtime_profiles={
                    "repo_map_agent": {
                        "adapter": "codex",
                        "command": [sys.executable, str(fake_codex)],
                        "model": "worker-role-profile-model",
                        "sandbox": "read-only",
                        "timeout_seconds": 45,
                    }
                },
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            try:
                self.assertEqual(start["pool_status"], "running")
                self.assertEqual(start["workers"][0]["worker_runtime"], "codex")
                self.assertEqual(
                    pool.workers[0].codex_command,
                    [sys.executable, str(fake_codex)],
                )
                self.assertEqual(pool.workers[0].codex_model, "worker-role-profile-model")
                self.assertEqual(pool.workers[0].codex_sandbox, "read-only")
                self.assertEqual(pool.workers[0].codex_timeout_seconds, 45)
            finally:
                stop = pool.stop()

            self.assertEqual(stop["workers"][0]["worker_runtime"], "codex")

    def test_file_mailbox_worker_pool_supervisor_resumes_running_workers_from_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            resumed_pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )
            try:
                resumed = resumed_pool.resume_from_registry()
                health = resumed_pool.health_check()
                stop = resumed_pool.stop()
            finally:
                if pool.workers:
                    process = pool.workers[0].process
                    if process:
                        try:
                            if process.poll() is None:
                                pool.stop()
                        except ChildProcessError:
                            pass
                        for stream in (process.stdout, process.stderr):
                            if stream:
                                stream.close()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(resumed["pool_status"], "running")
            self.assertEqual(
                resumed["workers"][0]["worker_pid"],
                start["workers"][0]["worker_pid"],
            )
            self.assertEqual(health["workers"][0]["worker_status"], "running")
            self.assertEqual(stop["workers"][0]["worker_status"], "stopped")
            self.assertEqual(registry["registry_status"], "stopped")

    def test_file_mailbox_worker_pool_supervisor_restarts_exited_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            first_pid = start["workers"][0]["worker_pid"]
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            degraded = pool.health_check()
            restarted = pool.restart_exited_workers()
            try:
                recovered = pool.health_check()
            finally:
                stop = pool.stop()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(degraded["pool_status"], "degraded")
            self.assertEqual(degraded["workers"][0]["worker_status"], "exited")
            self.assertEqual(restarted["restarted_count"], 1)
            self.assertEqual(restarted["workers"][0]["restart_status"], "restarted")
            self.assertNotEqual(
                restarted["workers"][0]["new_worker"]["worker_pid"],
                first_pid,
            )
            self.assertEqual(recovered["pool_status"], "running")
            self.assertEqual(recovered["workers"][0]["worker_status"], "running")
            self.assertEqual(registry["workers"][0]["restart_count"], 1)

    def test_file_mailbox_worker_pool_quarantines_after_restart_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
                max_restart_count=1,
            )

            pool.start()
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            first_restart = pool.restart_exited_workers()
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            quarantined = pool.restart_exited_workers()
            try:
                health = pool.health_check()
                registry = json.loads(
                    Path(health["process_registry_path"]).read_text(encoding="utf-8")
                )
            finally:
                pool.stop()

            self.assertEqual(first_restart["restarted_count"], 1)
            self.assertEqual(quarantined["restarted_count"], 0)
            self.assertEqual(quarantined["workers"][0]["restart_status"], "quarantined")
            self.assertEqual(health["workers"][0]["worker_status"], "quarantined")
            self.assertEqual(
                health["workers"][0]["quarantine_reason"],
                "restart_budget_exceeded",
            )
            self.assertEqual(registry["workers"][0]["worker_status"], "quarantined")
            self.assertEqual(registry["workers"][0]["restart_count"], 1)

    def test_two_phase_scheduler_does_not_double_book_same_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_inflight=2,
            )

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_status"], "dispatched")
            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(dispatch["inflight_count"], 1)

    def test_two_phase_scheduler_skips_unavailable_agent_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-unhealthy", "repo_map_agent"),
                    ("agent-healthy", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                unavailable_agent_ids=["agent-unhealthy"],
            )

            dispatch = scheduler.dispatch_ready()

            self.assertEqual(dispatch["dispatch_status"], "dispatched")
            self.assertEqual(
                scheduler.state["inflight_attempts"][0]["agent_id"],
                "agent-healthy",
            )

    def test_two_phase_scheduler_dispatch_includes_role_prompt_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_role_prompt_contracts(
                agent_pool_path,
                role_prompt_contracts={
                    "repo_map_agent": {
                        "role_summary": "Implement bounded repository edits.",
                        "instructions": ["Inspect read_scope before writing."],
                        "required_output_keys": ["evidence"],
                    }
                },
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
            )

            scheduler.dispatch_ready()

            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            contract = message["payload"]["role_prompt_contract"]

            self.assertEqual(message["payload"]["agent_role"], "repo_map_agent")
            self.assertEqual(
                contract["role_summary"],
                "Implement bounded repository edits.",
            )
            self.assertEqual(
                contract["instructions"],
                ["Inspect read_scope before writing."],
            )

    def test_two_phase_scheduler_dispatch_writes_role_context_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            artifact = tmp_path / "role-context.md"
            artifact.write_text(
                "# Role Context\n\nTwo-phase context body.\n\n## Boundary\n",
                encoding="utf-8",
            )
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_role_context_packages(
                agent_pool_path,
                role_context_packages={
                    "repo_map_agent": {
                        "context_artifacts": [str(artifact)],
                        "excerpt_chars": 120,
                    }
                },
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
            )

            scheduler.dispatch_ready()

            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            context_path = Path(message["payload"]["role_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))

            self.assertTrue(context_path.exists())
            self.assertEqual(context["context_schema_version"], "role_context.v1")
            self.assertEqual(context["agent_role"], "repo_map_agent")
            self.assertEqual(
                context["artifact_context"]["sources"][0]["headings"],
                ["Role Context", "Boundary"],
            )

    def test_two_phase_scheduler_dispatch_includes_repo_context_path_when_project_root_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
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
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
            )

            scheduler.dispatch_ready()

            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            context_path = Path(message["payload"]["repo_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))

            self.assertEqual(
                message["payload"]["repo_context_schema_version"],
                "repo_context.v1",
            )
            self.assertTrue(context_path.exists())
            self.assertEqual(context["selected_files"][0]["path"], "pkg/module.py")

    def test_two_phase_scheduler_records_reassignment_event_for_unavailable_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-unhealthy", "repo_map_agent"),
                    ("agent-healthy", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                unavailable_agent_ids=["agent-unhealthy"],
            )

            scheduler.dispatch_ready()
            event_lines = (output_dir / "events.jsonl").read_text(
                encoding="utf-8",
            ).splitlines()
            events = [
                json.loads(line)
                for line in event_lines
            ]
            reassignments = [
                event
                for event in events
                if event["event_type"] == "task_reassigned"
            ]
            self.assertEqual(len(reassignments), 1)
            reassignment = reassignments[0]

            self.assertEqual(reassignment["payload"]["task_id"], "TASK-001")
            self.assertEqual(
                reassignment["payload"]["attempt_id"],
                "TASK-001-ATTEMPT-001",
            )
            self.assertEqual(
                reassignment["payload"]["required_role"],
                "repo_map_agent",
            )
            self.assertEqual(
                reassignment["payload"]["selected_agent_id"],
                "agent-healthy",
            )
            self.assertEqual(
                reassignment["payload"]["unavailable_agent_ids"],
                ["agent-unhealthy"],
            )
            self.assertEqual(
                reassignment["payload"]["reassignment_reason"],
                "agent_unavailable",
            )
            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertIn(
                "reassignment",
                snapshot["attempts"]["TASK-001-ATTEMPT-001"],
            )
            self.assertEqual(
                snapshot["attempts"]["TASK-001-ATTEMPT-001"]["reassignment"],
                {
                    "reassignment_reason": "agent_unavailable",
                    "required_role": "repo_map_agent",
                    "unavailable_agent_ids": ["agent-unhealthy"],
                    "selected_agent_id": "agent-healthy",
                },
            )

    def test_two_phase_scheduler_retries_retryable_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=2,
            )

            first_dispatch = scheduler.dispatch_ready()
            first_inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                first_inflight["outbox_path"],
                first_inflight["message_id"],
                first_inflight["task_id"],
                first_inflight["attempt_id"],
                first_inflight["lease_id"],
                "failed",
                [],
            )

            first_collect = scheduler.collect_ready_results()
            second_dispatch = scheduler.dispatch_ready()
            second_inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                second_inflight["outbox_path"],
                second_inflight["message_id"],
                second_inflight["task_id"],
                second_inflight["attempt_id"],
                second_inflight["lease_id"],
                "completed",
                ["generated/retry.json"],
            )
            second_collect = scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(first_dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(first_collect["collected_task_ids"], ["TASK-001"])
            self.assertEqual(second_dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(second_inflight["attempt_id"], "TASK-001-ATTEMPT-002")
            self.assertEqual(second_collect["collected_task_ids"], ["TASK-001"])
            self.assertIn("recovery_routed", {event["event_type"] for event in events})
            self.assertEqual(
                [step["step_status"] for step in scheduler.state["steps"]],
                ["retry_routed", "processed"],
            )
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done"},
            )

    def test_two_phase_scheduler_dispatches_planner_task_when_auto_decompose_is_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M22",
            )

            dispatch = scheduler.dispatch_ready()
            planner_task = scheduler.state["backlog"]["items"][0]
            context_path = Path(planner_task["planner_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))
            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-DECOMPOSE-M22-001"
                / "mailboxes"
                / "agent-planner"
                / "inbox.jsonl"
            )

            self.assertEqual(dispatch["dispatch_status"], "dispatched")
            self.assertEqual(dispatch["dispatched_task_ids"], ["DECOMPOSE-M22-001"])
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["task_kind"],
                "decompose_backlog",
            )
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["required_role"],
                "task_planner",
            )
            self.assertTrue(context_path.exists())
            self.assertEqual(context["milestone_id"], "M22")
            self.assertEqual(context["allowed_write_scopes"], ["generated/"])
            self.assertEqual(message["payload"]["planner_context_path"], str(context_path))

    def test_two_phase_scheduler_writes_selected_artifacts_into_planner_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            artifact = tmp_path / "design.md"
            artifact.write_text(
                "# Design\n\nSelected artifact body.\n\n## Boundary\nKeep context bounded.\n",
                encoding="utf-8",
            )
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M24",
                decomposition_context_artifact_paths=[artifact],
                decomposition_context_excerpt_chars=80,
            )

            scheduler.dispatch_ready()
            planner_task = scheduler.state["backlog"]["items"][0]
            context = json.loads(
                Path(planner_task["planner_context_path"]).read_text(encoding="utf-8")
            )
            source = context["artifact_context"]["sources"][0]

            self.assertEqual(source["path"], str(artifact))
            self.assertEqual(source["headings"], ["Design", "Boundary"])
            self.assertLessEqual(source["excerpt_chars"], 80)

    def test_two_phase_scheduler_applies_planner_task_proposal_to_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M21",
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M21-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M21",
                        "tasks": [
                            {
                                "task_id": "TASK-M21-001",
                                "objective": "Run generated worker task.",
                                "read_scope": ["."],
                                "write_scope": ["generated/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            collected = scheduler.collect_ready_results()

            self.assertEqual(
                collected["results"][0]["decomposition_status"],
                "applied",
            )
            self.assertEqual(
                collected["results"][0]["generated_task_ids"],
                ["TASK-M21-001"],
            )
            self.assertEqual(
                [item["task_id"] for item in scheduler.state["backlog"]["items"]],
                ["DECOMPOSE-M21-001", "TASK-M21-001"],
            )

    def test_two_phase_scheduler_records_decomposition_lineage_and_milestone_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M26",
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M26-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M26",
                        "tasks": [
                            {
                                "task_id": "TASK-M26-001",
                                "objective": "Run first generated wave task.",
                                "read_scope": ["."],
                                "write_scope": ["generated/wave-1/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            scheduler.collect_ready_results()
            generated_task = scheduler._task_by_id("TASK-M26-001")
            milestone = scheduler.state["milestones"]["M26"]

            self.assertEqual(
                generated_task["generated_by_decomposition_task_id"],
                "DECOMPOSE-M26-001",
            )
            self.assertEqual(generated_task["decomposition_wave"], 1)
            self.assertEqual(milestone["milestone_status"], "active")
            self.assertEqual(milestone["decomposition_status"], "batch_active")
            self.assertEqual(milestone["decomposition_wave_count"], 1)
            self.assertEqual(milestone["generated_task_ids"], ["TASK-M26-001"])

    def test_two_phase_scheduler_opens_next_decomposition_wave_after_generated_batch_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M26",
                decomposition_max_waves=2,
            )

            scheduler.dispatch_ready()
            first_decompose = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                first_decompose["outbox_path"],
                first_decompose["message_id"],
                "DECOMPOSE-M26-001",
                first_decompose["attempt_id"],
                first_decompose["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M26",
                        "tasks": [
                            {
                                "task_id": "TASK-M26-WAVE-1",
                                "objective": "Complete first generated batch.",
                                "read_scope": ["."],
                                "write_scope": ["generated/wave-1/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )
            scheduler.collect_ready_results()

            scheduler.dispatch_ready()
            generated_inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                generated_inflight["outbox_path"],
                generated_inflight["message_id"],
                generated_inflight["task_id"],
                generated_inflight["attempt_id"],
                generated_inflight["lease_id"],
                "completed",
                ["generated/wave-1/result.json"],
            )
            scheduler.collect_ready_results()

            second_dispatch = scheduler.dispatch_ready()

            self.assertEqual(
                second_dispatch["dispatched_task_ids"],
                ["DECOMPOSE-M26-002"],
            )
            self.assertEqual(
                scheduler.state["milestones"]["M26"]["decomposition_wave_count"],
                2,
            )

    def test_two_phase_scheduler_marks_milestone_completed_when_max_waves_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M26",
                decomposition_max_waves=1,
            )

            scheduler.dispatch_ready()
            decompose = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                decompose["outbox_path"],
                decompose["message_id"],
                "DECOMPOSE-M26-001",
                decompose["attempt_id"],
                decompose["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M26",
                        "tasks": [
                            {
                                "task_id": "TASK-M26-FINAL",
                                "objective": "Complete final generated batch.",
                                "read_scope": ["."],
                                "write_scope": ["generated/final/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )
            scheduler.collect_ready_results()
            scheduler.dispatch_ready()
            generated = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                generated["outbox_path"],
                generated["message_id"],
                generated["task_id"],
                generated["attempt_id"],
                generated["lease_id"],
                "completed",
                ["generated/final/result.json"],
            )
            scheduler.collect_ready_results()

            final_dispatch = scheduler.dispatch_ready()
            milestone = scheduler.state["milestones"]["M26"]

            self.assertEqual(final_dispatch["dispatch_status"], "idle")
            self.assertEqual(
                [
                    task["task_id"]
                    for task in scheduler.state["backlog"]["items"]
                    if task.get("task_kind") == "decompose_backlog"
                ],
                ["DECOMPOSE-M26-001"],
            )
            self.assertEqual(milestone["milestone_status"], "completed")
            self.assertEqual(milestone["decomposition_status"], "max_waves_reached")
            self.assertEqual(milestone["terminal_reason"], "max_waves_reached")

    def test_two_phase_scheduler_rejects_planner_proposal_outside_context_write_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M22",
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M22-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M22",
                        "tasks": [
                            {
                                "task_id": "TASK-M22-001",
                                "objective": "Try to write outside context allowance.",
                                "read_scope": ["."],
                                "write_scope": ["src/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            collected = scheduler.collect_ready_results()

            self.assertEqual(
                collected["results"][0]["decomposition_status"],
                "rejected",
            )
            self.assertEqual(
                collected["results"][0]["failure_category"],
                "invalid_task_proposal",
            )
            self.assertEqual(len(scheduler.state["backlog"]["items"]), 1)

    def test_two_phase_scheduler_records_proposal_quality_rejection_error_in_validation_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M25",
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M25-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M25",
                        "tasks": [
                            {
                                "task_id": "TASK-M25-SELF-001",
                                "objective": "Invalid self-dependent task.",
                                "read_scope": ["."],
                                "write_scope": ["generated/"],
                                "required_role": "repo_map_agent",
                                "risk_target": "L0",
                                "depends_on": ["TASK-M25-SELF-001"],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            collected = scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            validation_event = next(
                event
                for event in events
                if event["event_type"] == "validation_rejected"
            )

            self.assertEqual(
                collected["results"][0]["decomposition_status"],
                "rejected",
            )
            self.assertIn(
                "self dependency",
                collected["results"][0]["decomposition_error"],
            )
            self.assertIn(
                "self dependency",
                validation_event["payload"]["decomposition_error"],
            )

    def test_two_phase_scheduler_collects_expired_inflight_as_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                lease_timeout_seconds=0,
            )

            scheduler.dispatch_ready()
            collected = scheduler.collect_ready_results()
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(collected["collect_status"], "collected")
            self.assertEqual(collected["collected_task_ids"], ["TASK-001"])
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(scheduler.state["steps"][0]["failure_category"], "timeout")
            self.assertTrue(scheduler.state["steps"][0]["result"]["retryable"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "running"},
            )
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["blockers"],
                ["timeout"],
            )

    def test_two_phase_scheduler_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/two_phase_commit.json').exists()",
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "two_phase_commit.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"attempt_id": inflight["attempt_id"]}),
                encoding="utf-8",
            )
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/two_phase_commit.json"],
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            integration_worktree = Path(result["integration_worktree_path"])
            queue = read_integration_queue(output_dir)
            queue_item = queue["items"][0]
            snapshot = replay_events(output_dir / "events.jsonl")
            snapshot_item = snapshot["integration_queue"][
                "TASK-001:TASK-001-ATTEMPT-001"
            ]

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["diff_audit"]["diff_status"], "matched")
            self.assertTrue(Path(result["patch_path"]).exists())
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertNotEqual(result["integration_commit_sha"], None)
            self.assertEqual(result["integration_queue_status"], "committed")
            self.assertEqual(queue_item["queue_status"], "committed")
            self.assertEqual(queue_item["integration_commit_sha"], result["integration_commit_sha"])
            self.assertEqual(snapshot_item["queue_status"], "committed")
            self.assertTrue(
                (integration_worktree / "generated" / "two_phase_commit.json").exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(
                snapshot["attempts"]["TASK-001-ATTEMPT-001"][
                    "integration_commit_status"
                ],
                "committed",
            )

    def test_two_phase_scheduler_dispatches_multiple_tasks_before_collecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task(
                        "TASK-002",
                        write_scope=["generated/task-002/"],
                        required_role="aux_role_1",
                    ),
                ],
            )
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_inflight=2,
            )

            pool.start()
            try:
                dispatch = scheduler.dispatch_ready()
                self.assertEqual(dispatch["dispatch_status"], "dispatched")
                self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001", "TASK-002"])
                self.assertEqual(dispatch["inflight_count"], 2)
                self.assertEqual(scheduler.summary()["processed_task_ids"], [])

                collected = None
                for _ in range(50):
                    collected = scheduler.collect_ready_results()
                    if collected["collected_count"] == 2:
                        break
                    time.sleep(0.02)
            finally:
                pool.stop()

            state = read_scheduler_state_index(output_dir)
            self.assertEqual(collected["collected_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                scheduler.summary()["processed_task_ids"],
                ["TASK-001", "TASK-002"],
            )
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done", "TASK-002": "done"},
            )

    def test_cli_can_run_file_daemon_with_static_long_running_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-long-running-worker-pool",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            process_registry = json.loads(
                Path(summary["worker_pool"]["process_registry_path"]).read_text(
                    encoding="utf-8"
                )
            )
            repo_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
            self.assertEqual(summary["worker_pool"]["worker_count"], 2)
            self.assertEqual(process_registry["registry_status"], "stopped")
            self.assertEqual(
                {worker["worker_agent_id"] for worker in summary["worker_pool"]["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(repo_outbox.exists())

    def test_cli_can_run_two_phase_scheduler_with_static_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task(
                        "TASK-002",
                        write_scope=["generated/task-002/"],
                        required_role="aux_role_1",
                    ),
                ],
            )
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--max-inflight",
                    "2",
                    "--max-attempts",
                    "2",
                    "--lease-timeout-seconds",
                    "900",
                    "--worker-max-restart-count",
                    "1",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertCountEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["inflight_count"], 0)
            self.assertEqual(summary["max_attempts"], 2)
            self.assertEqual(summary["lease_timeout_seconds"], 900)
            self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
            self.assertEqual(summary["worker_pool"]["worker_count"], 2)
            self.assertEqual(summary["worker_pool_health"]["pool_status"], "running")
            self.assertEqual(summary["worker_pool_health"]["max_restart_count"], 1)
            self.assertGreaterEqual(len(summary["worker_pool_supervision"]), 1)
            self.assertIn("restart_count", summary["worker_pool_health"]["workers"][0])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done", "TASK-002": "done"},
            )

    def test_cli_two_phase_worker_pool_can_auto_decompose_with_fake_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M21",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--max-steps",
                    "10",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertIn("DECOMPOSE-M21-001", summary["processed_task_ids"])
            self.assertIn("TASK-M21-GENERATED-001", summary["processed_task_ids"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {
                    "DECOMPOSE-M21-001": "done",
                    "TASK-M21-GENERATED-001": "done",
                },
            )

    def test_cli_two_phase_worker_pool_can_auto_decompose_with_fake_codex_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            fake_codex = tmp_path / "fake_codex_planner_and_worker.py"
            _init_git_repo(repo)
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            _write_fake_codex_planner_and_worker(fake_codex)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M23",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--runtime",
                    "codex",
                    "--max-steps",
                    "10",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertIn("DECOMPOSE-M23-001", summary["processed_task_ids"])
            self.assertIn("TASK-M23-CODEX-001", summary["processed_task_ids"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {
                    "DECOMPOSE-M23-001": "done",
                    "TASK-M23-CODEX-001": "done",
                },
            )
            self.assertEqual(_git_status_short(repo), "")

    def test_cli_two_phase_worker_pool_accepts_planner_context_artifact_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            artifact = tmp_path / "roadmap.md"
            artifact.write_text(
                "\n".join(
                    [
                        "# Roadmap",
                        "Selected CLI artifact.",
                        "## M24",
                        *["bounded context line" for _ in range(20)],
                        "CLI_TAIL_MARKER_SHOULD_NOT_BE_EMBEDDED",
                    ]
                ),
                encoding="utf-8",
            )
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M24",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--planner-context-artifact",
                    str(artifact),
                    "--planner-context-excerpt-chars",
                    "80",
                    "--max-steps",
                    "10",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            context = json.loads(
                (
                    output_dir
                    / "planner_contexts"
                    / "DECOMPOSE-M24-001.json"
                ).read_text(encoding="utf-8")
            )
            source = context["artifact_context"]["sources"][0]

            self.assertEqual(source["path"], str(artifact))
            self.assertEqual(source["headings"], ["Roadmap", "M24"])
            self.assertLessEqual(source["excerpt_chars"], 80)
            self.assertNotIn(
                "CLI_TAIL_MARKER_SHOULD_NOT_BE_EMBEDDED",
                json.dumps(context["artifact_context"], sort_keys=True),
            )

    def test_cli_two_phase_worker_pool_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--max-inflight",
                    "1",
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib; assert pathlib.Path('generated/m0_generated_repo_index.json').exists()",
                        ]
                    ),
                    "--commit-verified-integration",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = summary["steps"][0]["result"]
            integration_worktree = Path(result["integration_worktree_path"])

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertTrue(
                (
                    integration_worktree
                    / "generated"
                    / "m0_generated_repo_index.json"
                ).exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)

    def test_cli_can_show_state_index_without_agent_pool_or_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            run_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            state_db_path = output_dir / "state" / "scheduler_state.sqlite"
            state_db_path.unlink()
            root_event_count = len(
                Path(run_summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--output-dir",
                    str(output_dir),
                    "--show-state-index",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["event_count"], root_event_count)
            self.assertEqual(summary["tasks"][0]["task_id"], "TASK-001")
            self.assertEqual(summary["tasks"][1]["task_status"], "done")
            self.assertTrue(state_db_path.exists())

    def test_cli_can_show_runtime_observability_without_agent_pool_or_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            run_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            root_event_count = len(
                Path(run_summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--output-dir",
                    str(output_dir),
                    "--show-runtime-observability",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["observability_status"], "ready")
            self.assertEqual(summary["event_count"], root_event_count)
            self.assertEqual(summary["task_counts"], {"done": 2})
            self.assertEqual(summary["lease_counts"], {"released": 2})
            self.assertEqual(summary["runtime_session_counts"], {"stopped": 2})
            self.assertEqual(summary["integration_queue_counts"], {})
            self.assertEqual(summary["latest_failures"], [])
            self.assertEqual(summary["latest_event"]["event_type"], "backlog_updated")

    def test_runtime_observability_supports_drilldown_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )

            run_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            root_event_count = len(
                Path(run_summary["events_path"]).read_text(encoding="utf-8").splitlines()
            )

            backlog_view = build_runtime_observability(output_dir, view="backlog")
            events_view = build_runtime_observability(output_dir, view="events")
            sessions_view = build_runtime_observability(output_dir, view="sessions")
            workers_view = build_runtime_observability(output_dir, view="workers")

            self.assertEqual(backlog_view["view"], "backlog")
            self.assertEqual(
                backlog_view["tasks"],
                [
                    {"task_id": "TASK-001", "task_status": "done"},
                    {"task_id": "TASK-002", "task_status": "done"},
                ],
            )
            self.assertEqual(events_view["event_count"], root_event_count)
            self.assertEqual(events_view["events"][-1]["event_type"], "backlog_updated")
            self.assertEqual(sessions_view["runtime_sessions"][0]["session_status"], "stopped")
            self.assertEqual(workers_view["workers"], [])

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

    def test_cli_can_show_runtime_observability_event_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                    _backlog_task("TASK-002", write_scope=["generated/task-002/"]),
                ],
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            run_summary = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            root_event_count = len(
                Path(run_summary["events_path"]).read_text(encoding="utf-8").splitlines()
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
                    "events",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["view"], "events")
            self.assertEqual(summary["event_count"], root_event_count)
            self.assertEqual(summary["events"][-1]["event_type"], "backlog_updated")

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

    def test_cli_can_create_git_worktree_when_project_root_is_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(Path(summary["worktree_path"]).exists())
            self.assertTrue((Path(summary["worktree_path"]) / "generated").is_dir())

    def test_cli_can_run_shell_runtime_adapter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_shell_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "cli_shell_result.json").exists()
            )

    def test_cli_can_apply_accepted_patch_to_integration_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_integration_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_integration_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--integrate-accepted-patch",
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            integration_worktree = Path(summary["integration_worktree_path"])

            self.assertEqual(summary["integration_status"], "applied")
            self.assertTrue(
                (integration_worktree / "generated" / "cli_integration_result.json").exists()
            )

    def test_cli_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_commit_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_commit_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib; assert pathlib.Path('generated/cli_commit_result.json').exists()",
                        ]
                    ),
                    "--commit-verified-integration",
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            integration_worktree = Path(summary["integration_worktree_path"])

            self.assertEqual(summary["integration_verification_status"], "passed")
            self.assertEqual(summary["integration_commit_status"], "committed")
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(integration_worktree), "")

    def test_project_root_creates_real_git_worktree_for_writable_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertTrue(worktree_path.exists())
            completed = subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--is-inside-work-tree"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(completed.stdout.strip(), "true")
            self.assertTrue((worktree_path / "generated" / "m0_generated_repo_index.json").exists())

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["worktree_path"],
                str(worktree_path),
            )

    def test_out_of_scope_runtime_result_is_rejected(self):
        class OutOfScopeRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["outside/generated.txt"],
                    "output": {"note": "intentionally outside write scope"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                tmp_path / "run",
                clock=FixedClock(),
                runtime_adapter=OutOfScopeRuntimeAdapter(),
            )

            self.assertEqual(result["validation_status"], "rejected")
            snapshot = replay_events(tmp_path / "run" / "events.jsonl")
            self.assertNotEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["validation_status"],
                "rejected",
            )

    def test_attempt_outcome_classifies_scope_violation_as_non_retryable(self):
        task = {"write_scope": ["generated/"]}
        result = {
            "result_status": "completed",
            "changed_files": ["outside.txt"],
            "output": {},
        }

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(outcome["failure_category"], "scope_violation")
        self.assertFalse(outcome["retryable"])

    def test_attempt_outcome_classifies_timeout_as_retryable(self):
        task = {"write_scope": ["generated/"]}
        result = {"result_status": "timed_out", "changed_files": [], "output": {}}

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(outcome["failure_category"], "timeout")
        self.assertTrue(outcome["retryable"])

    def test_worktree_diff_audit_detects_declared_file_missing_from_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)

            audit = audit_worktree_diff(repo, ["generated/missing.json"])

            self.assertEqual(audit["diff_status"], "mismatch")
            self.assertEqual(audit["declared_changed_files"], ["generated/missing.json"])
            self.assertEqual(audit["actual_changed_files"], [])
            self.assertEqual(audit["missing_declared_files"], ["generated/missing.json"])
            self.assertEqual(audit["undeclared_changed_files"], [])

    def test_worktree_diff_audit_matches_declared_file_in_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)
            target = repo / "generated" / "actual.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"created": True}), encoding="utf-8")

            audit = audit_worktree_diff(repo, ["generated/actual.json"])

            self.assertEqual(audit["diff_status"], "matched")
            self.assertEqual(audit["declared_changed_files"], ["generated/actual.json"])
            self.assertEqual(audit["actual_changed_files"], ["generated/actual.json"])
            self.assertEqual(audit["missing_declared_files"], [])
            self.assertEqual(audit["undeclared_changed_files"], [])

    def test_shell_runtime_adapter_executes_command_in_worktree_and_parses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/shell_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue((worktree_path / "generated" / "shell_result.json").exists())

    def test_worktree_attempt_writes_patch_artifact_for_actual_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "patch_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/patch_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            patch_path = Path(result["patch_path"])

            self.assertTrue(patch_path.exists())
            self.assertEqual(result["attempts"][0]["patch_path"], str(patch_path))
            self.assertIn("generated/patch_result.json", patch_path.read_text(encoding="utf-8"))

    def test_accepted_patch_is_queued_without_auto_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "queued_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/queued_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            queue = read_integration_queue(output_dir)
            item = queue["items"][0]
            snapshot = replay_events(output_dir / "events.jsonl")
            snapshot_item = snapshot["integration_queue"]["TASK-001:ATTEMPT-001"]

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["integration_status"], "not_requested")
            self.assertEqual(result["integration_queue_status"], "pending")
            self.assertEqual(result["integration_queue_item_id"], "TASK-001:ATTEMPT-001")
            self.assertEqual(item["queue_status"], "pending")
            self.assertEqual(item["patch_path"], result["patch_path"])
            self.assertEqual(item["integration_status"], "not_requested")
            self.assertEqual(snapshot_item["queue_status"], "pending")
            self.assertEqual(snapshot_item["patch_path"], result["patch_path"])

    def test_integration_batch_verifies_two_queued_patches_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script_a = tmp_path / "queued_worker_a.py"
            script_b = tmp_path / "queued_worker_b.py"
            _init_git_repo(repo)
            _write_success_worker(script_a, "generated/a.json")
            _write_success_worker(script_b, "generated/b.json")

            backlog_a = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-A", write_scope=["generated/"])],
            )
            result_a = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_a,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script_a)]),
                attempt_id_prefix="A",
            )
            backlog_b = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-B", write_scope=["generated/"])],
            )
            result_b = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_b,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script_b)]),
                attempt_id_prefix="B",
            )

            batch = verify_integration_batch(
                repo,
                output_dir,
                "BATCH-001",
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib; "
                        "assert pathlib.Path('generated/a.json').exists(); "
                        "assert pathlib.Path('generated/b.json').exists()"
                    ),
                ],
            )
            registry = read_integration_batches(output_dir)
            batch_worktree = Path(batch["batch_worktree_path"])

            self.assertEqual(result_a["integration_queue_status"], "pending")
            self.assertEqual(result_b["integration_queue_status"], "pending")
            self.assertEqual(batch["batch_status"], "verified")
            self.assertEqual(batch["verification_status"], "passed")
            self.assertEqual(
                batch["queue_item_ids"],
                ["TASK-A:A-ATTEMPT-001", "TASK-B:B-ATTEMPT-001"],
            )
            self.assertTrue((batch_worktree / "generated" / "a.json").exists())
            self.assertTrue((batch_worktree / "generated" / "b.json").exists())
            self.assertEqual(registry["items"][0]["batch_status"], "verified")
            self.assertEqual(
                registry["items"][0]["applied_queue_item_ids"],
                ["TASK-A:A-ATTEMPT-001", "TASK-B:B-ATTEMPT-001"],
            )

    def test_verified_integration_batch_can_merge_back_to_source_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "merge_batch_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            _write_success_worker(script, "generated/merge_batch.json")
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-MERGE", write_scope=["generated/"])],
            )
            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            batch = verify_integration_batch(
                repo,
                output_dir,
                "BATCH-MERGE",
                [
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/merge_batch.json').exists()",
                ],
                merge_verified_batch=True,
            )
            registry = read_integration_batches(output_dir)

            self.assertEqual(batch["batch_status"], "verified")
            self.assertEqual(batch["merge_status"], "merged")
            self.assertNotEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertTrue((repo / "generated" / "merge_batch.json").exists())
            self.assertEqual(_git_status_short(repo), "")
            self.assertEqual(registry["items"][0]["merge_status"], "merged")
            self.assertEqual(registry["items"][0]["merge_commit_sha"], _git_rev_parse(repo, "HEAD"))

    def test_accepted_patch_applies_to_integration_worktree_without_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "integration_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_branch"], "agentteam/integration/TASK-001")
            self.assertTrue(
                (integration_worktree / "generated" / "integration_result.json").exists()
            )
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_status"],
                "applied",
            )

    def test_integration_verification_command_passes_in_integration_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "verify_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/integration_result.json').exists()",
                ],
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_verification_exit_code"], 0)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_verification_status"],
                "passed",
            )

    def test_integration_verification_command_failure_is_recorded_without_rejecting_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "verify_fail_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(result["integration_verification_exit_code"], 7)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_verification_status"],
                "failed",
            )

    def test_verified_integration_patch_can_be_committed_to_integration_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/commit_result.json').exists()",
                ],
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertIsNotNone(result["integration_commit_sha"])
            self.assertEqual(result["integration_commit_reason"], None)
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(integration_worktree), "")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_commit_status"],
                "committed",
            )

    def test_integration_commit_is_skipped_when_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_skip_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_skip_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(result["integration_commit_reason"], "verification_failed")
            self.assertEqual(result["integration_commit_sha"], None)
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertNotEqual(_git_status_short(integration_worktree), "")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_commit_status"],
                "skipped",
            )

    def test_integration_commit_is_skipped_without_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_no_verify_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_no_verify_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])

            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(result["integration_commit_reason"], "verification_not_requested")
            self.assertEqual(result["integration_commit_sha"], None)
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)

    def test_shell_runtime_adapter_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "fail_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            script.write_text(
                "import sys\nsys.stderr.write('worker failed intentionally')\nsys.exit(17)\n",
                encoding="utf-8",
            )

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "failed")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["validation_status"], "rejected")

    def test_retryable_runtime_failure_can_be_retried_and_accepted(self):
        class RetryOnceRuntimeAdapter:
            def __init__(self):
                self.attempt_ids = []

            def run(self, message, worktree_path=None):
                self.attempt_ids.append(message["payload"]["attempt_id"])
                if len(self.attempt_ids) == 1:
                    return {
                        "result_status": "failed",
                        "changed_files": [],
                        "output": {"error": "transient"},
                    }
                target = Path(worktree_path) / "generated" / "retry_result.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"attempt_id": message["payload"]["attempt_id"]}),
                    encoding="utf-8",
                )
                return {
                    "result_status": "completed",
                    "changed_files": ["generated/retry_result.json"],
                    "output": {"adapter": "retry-once"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            adapter = RetryOnceRuntimeAdapter()
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=adapter,
                max_attempts=2,
            )

            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(adapter.attempt_ids, ["ATTEMPT-001", "ATTEMPT-002"])
            self.assertEqual(result["attempt_id"], "ATTEMPT-002")
            self.assertEqual(result["attempt_count"], 2)
            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["failure_category"], None)
            self.assertEqual(result["attempts"][0]["failure_category"], "runtime_error")
            self.assertIn("recovery_routed", {event["event_type"] for event in events})
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["validation_status"],
                "rejected",
            )
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-002"]["validation_status"],
                "accepted",
            )
            self.assertTrue((Path(result["worktree_path"]) / "generated" / "retry_result.json").exists())

    def test_declared_changed_file_without_worktree_diff_is_rejected(self):
        class PhantomRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["generated/phantom.json"],
                    "output": {"adapter": "phantom"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=PhantomRuntimeAdapter(),
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(result["failure_category"], "diff_mismatch")
            self.assertEqual(
                result["diff_audit"]["missing_declared_files"],
                ["generated/phantom.json"],
            )
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["failure_category"],
                "diff_mismatch",
            )

    def test_accepted_attempt_can_remove_git_worktree_when_cleanup_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
                cleanup_accepted_worktrees=True,
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue(result["worktree_removed"])
            self.assertFalse(Path(result["worktree_path"]).exists())
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["worktree_status"],
                "removed",
            )

    def test_codex_runtime_adapter_reads_last_message_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue((worktree_path / "generated" / "codex_result.json").exists())

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

    def test_codex_runtime_adapter_includes_role_prompt_contract(self):
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
                "agent_role": "repo_map_agent",
                "role_prompt_contract": {
                    "role_summary": "Implement bounded repository edits.",
                    "instructions": ["Inspect read_scope before writing."],
                    "required_output_keys": ["evidence"],
                },
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("Role prompt contract:", prompt)
        self.assertIn("Implement bounded repository edits.", prompt)
        self.assertIn("Inspect read_scope before writing.", prompt)
        self.assertIn("required_output_keys", prompt)

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

    def test_codex_runtime_adapter_runs_planner_with_fallback_worktree_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_planner.py"
            _init_git_repo(repo)
            _write_fake_codex_planner(fake_codex)

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                fallback_worktree_path=repo,
            ).run(_planner_message(tmp_path), worktree_path=None)

            self.assertEqual(result["result_status"], "completed")
            self.assertEqual(result["changed_files"], [])
            self.assertEqual(
                result["output"]["task_proposal"]["tasks"][0]["task_id"],
                "TASK-M23-CODEX-001",
            )
            self.assertEqual(_git_status_short(repo), "")

    def test_codex_runtime_adapter_rejects_dirty_fallback_worktree_after_planner_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_dirty_planner.py"
            _init_git_repo(repo)
            _write_fake_codex_planner(fake_codex, dirty_file="generated/dirty.json")

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                fallback_worktree_path=repo,
            ).run(_planner_message(tmp_path), worktree_path=None)

            self.assertEqual(result["result_status"], "failed")
            self.assertEqual(
                result["output"]["error"],
                "fallback_worktree_modified",
            )
            self.assertIn("generated/dirty.json", result["output"]["changed_files"])

    def test_codex_runtime_adapter_missing_last_message_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_no_result.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            fake_codex.write_text("import sys\nsys.stdin.read()\nprint('no result file')\n", encoding="utf-8")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "failed")

    def test_codex_runtime_adapter_default_exec_flags_match_current_cli(self):
        command = CodexRuntimeAdapter(command=["codex", "exec"])._build_command(
            "/tmp/worktree",
            "/tmp/result.json",
        )

        self.assertIn("-C", command)
        self.assertIn("-s", command)
        self.assertIn("--output-last-message", command)
        self.assertNotIn("-a", command)
        self.assertNotIn("--ask-for-approval", command)

    def test_cli_can_run_codex_runtime_adapter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_cli.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_cli_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "codex_cli_result.json").exists()
            )

    def test_cli_can_select_codex_runtime_with_command_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_runtime.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_runtime_result.json")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--runtime",
                    "codex",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "CodexRuntimeAdapter",
            )
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "codex_runtime_result.json").exists()
            )

    def test_cli_rejects_codex_runtime_without_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--runtime",
                    "codex",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("--project-root is required when --runtime codex is set", completed.stderr)

    def test_cli_passes_codex_runtime_options_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_options.py"
            target_file = "generated/codex_runtime_options.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--runtime",
                    "codex",
                    "--codex-model",
                    "gpt-test-model",
                    "--codex-sandbox",
                    "read-only",
                    "--codex-timeout-seconds",
                    "30",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            recorded = json.loads(
                (Path(summary["worktree_path"]) / target_file).read_text(encoding="utf-8")
            )

            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(recorded["model"], "gpt-test-model")
            self.assertEqual(recorded["sandbox"], "read-only")

    def test_cli_uses_agent_runtime_profile_for_codex_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_profile.py"
            target_file = "generated/codex_agent_profile.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_runtime_profile(
                agent_pool_path,
                runtime_profile={
                    "adapter": "codex",
                    "model": "agent-profile-model",
                    "sandbox": "read-only",
                    "timeout_seconds": 30,
                },
            )
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            recorded = json.loads(
                (Path(summary["worktree_path"]) / target_file).read_text(encoding="utf-8")
            )

            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "CodexRuntimeAdapter",
            )
            self.assertEqual(recorded["model"], "agent-profile-model")
            self.assertEqual(recorded["sandbox"], "read-only")


def _init_git_repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "config", "user.email", "agentteam@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "AgentTeam Test"], cwd=path, check=True)
    (path / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial fixture"],
        cwd=path,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_rev_parse(repo, ref):
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _git_status_short(repo):
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _planner_message(tmp_path):
    context_path = Path(tmp_path) / "planner_contexts" / "DECOMPOSE-M23-001.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps(
            {
                "context_schema_version": "planner_context.v1",
                "milestone_id": "M23",
                "default_worker_role": "repo_map_agent",
                "allowed_read_scopes": ["."],
                "allowed_write_scopes": ["generated/"],
                "available_agent_roles": ["repo_map_agent", "task_planner"],
                "proposal_contract": {
                    "schema_version": "task_proposal.v1",
                    "required_fields": [
                        "task_id",
                        "objective",
                        "read_scope",
                        "write_scope",
                        "required_role",
                        "risk_target",
                    ],
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
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
            "default_worker_role": "repo_map_agent",
            "planner_context_path": str(context_path),
            "objective": "Generate bounded backlog tasks.",
            "read_scope": ["."],
            "write_scope": [],
        },
    }


def _read_first_jsonl(path):
    return json.loads(Path(path).read_text(encoding="utf-8").splitlines()[0])


def _append_test_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True))
            stream.write("\n")


def _mailbox_dispatch_message(message_id, agent_id, write_scope):
    return {
        "message_id": message_id,
        "from_agent": "agent-scheduler",
        "to_agent": agent_id,
        "message_type": "dispatch_task",
        "correlation_id": f"TASK-MAILBOX:{message_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "lease_expires_at": "2026-06-03T00:15:00Z",
        "payload": {
            "task_id": "TASK-MAILBOX",
            "attempt_id": "ATTEMPT-MAILBOX-001",
            "lease_id": "LEASE-MAILBOX-001",
            "worktree_id": "WT-MAILBOX-001",
            "worktree_path": None,
            "branch": None,
            "objective": "Exercise file mailbox worker runtime.",
            "read_scope": ["."],
            "write_scope": write_scope,
        },
    }


def _write_backlog(tmp_path, write_scope, tasks=None):
    backlog = {
        "backlog_id": "BL-TEST",
        "items": [_backlog_task("TASK-001", write_scope=write_scope)] if tasks is None else tasks,
    }
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps(backlog), encoding="utf-8")
    return path


def _write_agent_pool_with_runtime_profile(path, runtime_profile):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-02T00:00:00Z",
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "runtime_profile": runtime_profile,
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool), encoding="utf-8")


def _write_agent_pool_with_role_runtime_profiles(path, role_runtime_profiles):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "role_runtime_profiles": role_runtime_profiles,
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_role_prompt_contracts(path, role_prompt_contracts):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "role_prompt_contracts": role_prompt_contracts,
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_role_context_packages(path, role_context_packages):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "role_context_packages": role_context_packages,
        "agents": [
            {
                "agent_id": "agent-repo-map",
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": "mailboxes/agent-repo-map/inbox.jsonl",
                "outbox_path": "mailboxes/agent-repo-map/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_id(path, agent_id):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_ids(path, agent_ids):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": "repo_map_agent" if index == 0 else f"aux_role_{index}",
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for index, agent_id in enumerate(agent_ids)
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _write_agent_pool_with_agent_roles(path, agent_roles):
    agent_pool = {
        "pool_id": "test-agent-pool",
        "scheduler_agent_id": "agent-scheduler",
        "updated_at": "2026-06-03T00:00:00Z",
        "agents": [
            {
                "agent_id": agent_id,
                "role": role,
                "status": "idle",
                "model_profile": "small-tooling",
                "runtime_adapter": "codex",
                "subscriptions": ["repo_index_stale"],
                "inbox_path": f"mailboxes/{agent_id}/inbox.jsonl",
                "outbox_path": f"mailboxes/{agent_id}/outbox.jsonl",
                "lease": {
                    "lease_id": None,
                    "task_id": None,
                    "expires_at": None,
                },
                "owned_artifacts": [],
                "last_event_id": None,
                "memory_summary_path": None,
            }
            for agent_id, role in agent_roles
        ],
    }
    path.write_text(json.dumps(agent_pool, sort_keys=True), encoding="utf-8")


def _append_runtime_result(
    outbox_path,
    source_message_id,
    task_id,
    attempt_id,
    lease_id,
    result_status,
    changed_files,
):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-repo-map",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": {"test": "m18"},
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def _append_runtime_result_with_output(
    outbox_path,
    source_message_id,
    task_id,
    attempt_id,
    lease_id,
    result_status,
    changed_files,
    output,
):
    record = {
        "message_id": f"RESULT-{source_message_id}",
        "from_agent": "agent-planner",
        "to_agent": "agent-scheduler",
        "message_type": "runtime_result",
        "correlation_id": f"{task_id}:{attempt_id}",
        "created_at": "2026-06-03T00:00:00Z",
        "payload": {
            "source_message_id": source_message_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "lease_id": lease_id,
            "result_status": result_status,
            "changed_files": changed_files,
            "output": output,
        },
    }
    outbox_path = Path(outbox_path)
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    with outbox_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True))
        stream.write("\n")


def _backlog_task(
    task_id,
    write_scope,
    status="ready",
    depends_on=None,
    blockers=None,
    required_role="repo_map_agent",
):
    return {
        "task_id": task_id,
        "milestone_id": "M0",
        "objective": f"Create generated repo index for {task_id}.",
        "backlog_status": status,
        "risk_target": "L0",
        "depends_on": list(depends_on or []),
        "read_scope": ["."],
        "write_scope": write_scope,
        "required_role": required_role,
        "blockers": list(blockers or []),
    }


def _event_record(event_id, sequence):
    return {
        "actor": "agent-scheduler",
        "correlation_id": "RUN-TEST",
        "event_id": event_id,
        "event_type": "scheduler_started",
        "idempotency_key": f"scheduler-start:{sequence}",
        "payload": {"pool_id": "test"},
        "sequence": sequence,
        "target_agent_id": None,
        "time": f"2026-05-31T00:00:{sequence:02d}Z",
    }


def _write_success_worker(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "message = json.load(sys.stdin)",
                f"target = pathlib.Path({changed_file!r})",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'attempt_id': message['payload']['attempt_id']}), encoding='utf-8')",
                "print(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'shell'}",
                "}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({'saw_prompt': 'dispatch_task' in prompt}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'prompt_contains_contract': 'changed_files' in prompt}",
                "}), encoding='utf-8')",
                "print(json.dumps({'event': 'fake_codex_done'}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex_planner(path, dirty_file=None):
    lines = [
        "import json",
        "import pathlib",
        "import sys",
        "args = sys.argv[1:]",
        "prompt = sys.stdin.read()",
        "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
        "worktree = pathlib.Path(args[args.index('-C') + 1])",
        "if 'AgentTeam planner' not in prompt or 'task_proposal' not in prompt:",
        "    sys.exit(7)",
    ]
    if dirty_file:
        lines.extend(
            [
                f"dirty = worktree / {dirty_file!r}",
                "dirty.parent.mkdir(parents=True, exist_ok=True)",
                "dirty.write_text('dirty planner change\\n', encoding='utf-8')",
            ]
        )
    lines.extend(
        [
            "output_path.parent.mkdir(parents=True, exist_ok=True)",
            "output_path.write_text(json.dumps({",
            "    'result_status': 'completed',",
            "    'changed_files': [],",
            "    'output': {",
            "        'adapter': 'codex',",
            "        'task_proposal': {",
            "            'milestone_id': 'M23',",
            "            'tasks': [{",
            "                'task_id': 'TASK-M23-CODEX-001',",
            "                'objective': 'Run generated Codex planner worker task.',",
            "                'read_scope': ['.'],",
            "                'write_scope': ['generated/'],",
            "                'required_role': 'repo_map_agent',",
            "                'risk_target': 'L0',",
            "                'depends_on': [],",
            "                'blockers': [],",
            "            }],",
            "        },",
            "    },",
            "}), encoding='utf-8')",
            "print(json.dumps({'event': 'fake_codex_planner_done'}))",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_fake_codex_planner_and_worker(path):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "prompt = sys.stdin.read()",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "if 'AgentTeam planner' in prompt:",
                "    if 'task_proposal' not in prompt:",
                "        sys.exit(7)",
                "    output_path.write_text(json.dumps({",
                "        'result_status': 'completed',",
                "        'changed_files': [],",
                "        'output': {",
                "            'adapter': 'codex',",
                "            'task_proposal': {",
                "                'milestone_id': 'M23',",
                "                'tasks': [{",
                "                    'task_id': 'TASK-M23-CODEX-001',",
                "                    'objective': 'Run generated Codex planner worker task.',",
                "                    'read_scope': ['.'],",
                "                    'write_scope': ['generated/'],",
                "                    'required_role': 'repo_map_agent',",
                "                    'risk_target': 'L0',",
                "                    'depends_on': [],",
                "                    'blockers': [],",
                "                }],",
                "            },",
                "        },",
                "    }), encoding='utf-8')",
                "else:",
                "    target = worktree / 'generated' / 'codex_generated_worker.json'",
                "    target.parent.mkdir(parents=True, exist_ok=True)",
                "    target.write_text(json.dumps({'generated_by': 'fake_codex_worker'}), encoding='utf-8')",
                "    output_path.write_text(json.dumps({",
                "        'result_status': 'completed',",
                "        'changed_files': ['generated/codex_generated_worker.json'],",
                "        'output': {'adapter': 'codex'},",
                "    }), encoding='utf-8')",
                "print(json.dumps({'event': 'fake_codex_planner_and_worker_done'}))",
            ]
        ),
        encoding="utf-8",
    )


def _write_fake_codex_arg_recorder(path, changed_file):
    path.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                "worktree = pathlib.Path(args[args.index('-C') + 1])",
                "sandbox = args[args.index('-s') + 1]",
                "model = args[args.index('-m') + 1] if '-m' in args else None",
                f"target = worktree / {changed_file!r}",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text(json.dumps({",
                "    'argv': args,",
                "    'model': model,",
                "    'sandbox': sandbox,",
                "}), encoding='utf-8')",
                "output_path.parent.mkdir(parents=True, exist_ok=True)",
                "output_path.write_text(json.dumps({",
                "    'result_status': 'completed',",
                f"    'changed_files': [{changed_file!r}],",
                "    'output': {'adapter': 'codex', 'mode': 'fake-options'}",
                "}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()

try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ProjectionsMixin:
    def test_blueprint_freeze_rejects_each_drifted_materialized_artifact(self):
        for artifact_name in (
            "taskpack.yaml",
            "agent_pool.json",
            "backlog.json",
            "verification.json",
            "README.md",
        ):
            with self.subTest(artifact_name=artifact_name):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    repo = tmp_path / "repo"
                    _init_repo(repo)
                    blueprint_path, _blueprint = _blueprint_fixture(repo)
                    materialized = (
                        taskpack_module.materialize_taskpack_blueprint(
                            repo,
                            blueprint_path,
                            tmp_path / "drafts",
                        )
                    )
                    artifact_path = (
                        Path(materialized["taskpack_dir"]) / artifact_name
                    )
                    artifact_path.write_text(
                        artifact_path.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        TaskpackValidationError,
                        (
                            "blueprint-materialized taskpack artifact changed "
                            f"before freeze: {re.escape(artifact_name)}"
                        ),
                    ):
                        freeze_taskpack(
                            materialized["taskpack_dir"],
                            tmp_path / "frozen",
                        )

                    self.assertFalse(
                        (
                            tmp_path
                            / "frozen"
                            / "example-blueprint"
                        ).exists()
                    )


    def test_blueprint_accepts_only_unique_ancestor_generated_input_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            blueprint_path, blueprint = _blueprint_fixture(repo)
            generated_path = ".agentteam/generated/generated_analysis.json"
            blueprint["tasks"][0]["expected_output_artifacts"] = [generated_path]
            blueprint["tasks"][1]["input_artifacts"].append(generated_path)
            _write_json(repo / blueprint_path, blueprint)

            result = taskpack_module.materialize_taskpack_blueprint(
                repo,
                blueprint_path,
                tmp_path / "unused",
                dry_run=True,
            )
            self.assertEqual(result["validation_status"], "accepted")

            blueprint["tasks"][1]["depends_on"] = []
            _write_json(repo / generated_path, {"stale": True})
            _write_json(repo / blueprint_path, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "not produced by exactly one ancestor task",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "missing-edge",
                    dry_run=True,
                )

            blueprint["tasks"][1]["depends_on"] = ["T-1"]
            blueprint["tasks"][2]["expected_output_artifacts"] = [generated_path]
            _write_json(repo / blueprint_path, blueprint)
            with self.assertRaisesRegex(
                TaskpackValidationError,
                "expected_output_artifacts must have one producer",
            ):
                taskpack_module.materialize_taskpack_blueprint(
                    repo,
                    blueprint_path,
                    tmp_path / "duplicate-producer",
                    dry_run=True,
                )


    def _projection_parity_payload(self, command, summary):
        if command == "status":
            return {
                "latest_run": summary["latest_run"],
                "run_status": summary["run_status"],
                "overall_status": summary["overall_status"],
                "tasks": summary["tasks"],
                "manual_gates": summary["manual_gates"],
                "permission_requests": summary["permission_requests"],
            }
        if command == "logs":
            return {
                "latest_run": summary["latest_run"],
                "event_count": summary["event_count"],
                "returned_count": summary["returned_count"],
                "events": [
                    {
                        "event_id": event.get("event_id"),
                        "event_type": event.get("event_type"),
                        "sequence": event.get("sequence"),
                        "payload": event.get("payload"),
                    }
                    for event in summary["events"]
                ],
            }
        if command == "report":
            return {
                "run_id": summary["run_id"],
                "run_status": summary["run_status"],
                "task_count": summary["task_count"],
                "blocked_count": summary["blocked_count"],
                "token_usage": summary["token_usage"],
                "task_reports": summary["operator_report"]["task_reports"],
            }
        if command == "taskpack_list":
            return {
                "frozen_count": summary["frozen_count"],
                "taskpacks": [
                    {
                        "taskpack_id": item["taskpack_id"],
                        "goal": item.get("goal"),
                        "run_status": item["run_status"],
                        "run_dir": item.get("run_dir"),
                    }
                    for item in summary["taskpacks"]
                ],
            }
        raise AssertionError(f"unknown projection parity command: {command}")


    def test_project_projection_db_rebuild_indexes_runs_taskpacks_events_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "task_id": "optimize-pipeline",
                    "result": {
                        "attempt_id": "optimize-pipeline-ATTEMPT-001",
                        "evidence_level": "L2",
                        "evidence_status": "incomplete",
                        "trace_carrier": [{"type": "event", "path": "EVT-0001"}],
                        "missing_evidence": ["review_result"],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            summary = rebuild_project_projection_db(work_root)

            self.assertEqual(summary["db_status"], "rebuilt")
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["taskpacks"], 1)
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["tasks"], 1)
            self.assertEqual(summary["evidence"], {"incomplete": 1})
            db_path = Path(summary["db_path"])
            with sqlite3.connect(db_path) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                run_rows = connection.execute(
                    "select run_id, event_count, latest_event_type from runs"
                ).fetchall()
                evidence_rows = connection.execute(
                    "select run_id, task_id, attempt_id, evidence_level, evidence_status, missing_evidence_json from evidence_summaries"
                ).fetchall()

            self.assertTrue(
                {
                    "schema_info",
                    "runs",
                    "taskpacks",
                    "events",
                    "tasks",
                    "evidence_summaries",
                }.issubset(table_names)
            )
            self.assertEqual(run_rows, [("projection-run", 1, "run_completed")])
            self.assertEqual(
                evidence_rows,
                [
                    (
                        "projection-run",
                        "optimize-pipeline",
                        "optimize-pipeline-ATTEMPT-001",
                        "L2",
                        "incomplete",
                        '["review_result"]',
                    )
                ],
            )


    def test_project_projection_db_check_detects_stale_event_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            rebuild_project_projection_db(work_root)

            passed = check_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            failed = check_project_projection_db(work_root)

            self.assertEqual(passed["check_status"], "passed")
            self.assertEqual(failed["check_status"], "failed")
            self.assertIn("events", failed["mismatches"])
            self.assertEqual(failed["expected"]["events"], 2)
            self.assertEqual(failed["actual"]["events"], 1)


    def test_project_projection_db_projects_follow_up_lineage_and_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            attempt_id = "optimize-pipeline-ATTEMPT-001"
            patch_path = run_dir / "steps" / "STEP-0001-optimize-pipeline" / "result.patch"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
            verification_addition = {
                "label": "projection-readthrough",
                "command": ["python3", "-m", "unittest", "tests.test_projection"],
                "reason": "cover the DB projection query surface",
            }
            verification_addition_result = {
                **verification_addition,
                "verification_addition_status": "passed",
                "verification_addition_exit_code": 0,
                "verification_addition_stdout": "ok\n",
                "verification_addition_stderr": "",
                "verification_addition_rejection_reason": None,
            }
            runtime_output = {
                "operator_summary": {
                    "what_changed": "投影 follow-up lineage。",
                    "verification_summary": "focused projection test passed",
                    "measured_result": "worker verification_additions preserved",
                    "merge_recommendation": "人工审阅后合并。",
                    "next_steps": ["补充 DB 投影 readthrough 测试。"],
                },
                "verification_additions": [verification_addition],
            }
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "step_id": "STEP-0001-optimize-pipeline",
                    "task_id": "optimize-pipeline",
                    "result": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": attempt_id,
                        "lease_id": "LEASE-001",
                        "changed_files": ["agentteam_runtime/projection_db.py"],
                        "runtime_output": runtime_output,
                        "validation_status": "accepted",
                        "failure_category": None,
                        "patch_path": str(patch_path),
                        "integration_queue_status": "verified",
                        "integration_queue_item_id": f"optimize-pipeline:{attempt_id}",
                        "integration_status": "applied",
                        "integration_worktree_path": str(run_dir / "integration" / "optimize-pipeline"),
                        "integration_verification_status": "passed",
                        "integration_verification_exit_code": 0,
                        "integration_verification_stdout": "primary ok\n",
                        "integration_verification_stderr": "",
                        "integration_verification_additions_status": "passed",
                        "integration_verification_additions": [verification_addition_result],
                        "integration_commit_status": "not_requested",
                        "evidence_level": "L2",
                        "evidence_status": "complete",
                        "trace_carrier": [{"type": "command", "command": "focused", "result": "passed"}],
                        "missing_evidence": [],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(
                run_dir / "codex_results" / f"codex_result_{attempt_id}.json",
                {
                    "result_status": "completed",
                    "changed_files": ["agentteam_runtime/projection_db.py"],
                    "output": runtime_output,
                },
            )
            _write_json(
                run_dir / "state" / "integration_queue.json",
                {
                    "queue_schema_version": "integration_queue.v1",
                    "items": [
                        {
                            "queue_item_id": f"optimize-pipeline:{attempt_id}",
                            "queue_status": "verified",
                            "task_id": "optimize-pipeline",
                            "attempt_id": attempt_id,
                            "lease_id": "LEASE-001",
                            "patch_path": str(patch_path),
                            "integration_status": "applied",
                            "integration_verification_status": "passed",
                            "integration_verification_exit_code": 0,
                            "integration_verification_additions_status": "passed",
                            "integration_verification_additions": [verification_addition_result],
                            "integration_commit_status": "not_requested",
                        }
                    ],
                },
            )
            events = _read_jsonl(run_dir / "events.jsonl")
            events.append(
                {
                    "event_id": "EVT-0002",
                    "event_type": "integration_verified",
                    "sequence": 2,
                    "payload": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": attempt_id,
                        "lease_id": "LEASE-001",
                        "integration_verification_status": "passed",
                        "integration_verification_exit_code": 0,
                        "integration_verification_stdout": "primary ok\n",
                        "integration_verification_stderr": "",
                        "integration_verification_additions_status": "passed",
                        "integration_verification_additions": [verification_addition_result],
                    },
                }
            )
            _write_jsonl(run_dir / "events.jsonl", events)
            goal_memory_path = work_root / "pursue" / "projection-goal-memory.json"
            _write_json(
                goal_memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(goal_memory_path),
                    "latest_taskpack_id": "projection-run",
                    "latest_run_ids": ["projection-run"],
                    "follow_up_queue": [
                        {
                            "objective": "补充 DB 投影 readthrough 测试。",
                            "source_taskpack_id": "projection-run",
                            "source_report_path": str(run_dir / "reports" / "final_report.md"),
                            "source_result_status": "completed",
                            "source_run_outcome": "completed_with_review_required",
                            "source_evidence_paths": [
                                {"type": "report", "path": str(run_dir / "reports" / "final_report.md")}
                            ],
                            "stop_reason": "review_gate_required",
                            "suggested_verification": "python3 -m unittest tests.test_projection",
                        }
                    ],
                },
            )

            summary = rebuild_project_projection_db(work_root)
            lineage = projection_db.read_projected_follow_up_lineage(work_root)

            self.assertEqual(summary["follow_up_items"], 1)
            self.assertEqual(summary["worker_results"], 1)
            self.assertEqual(summary["integration_outcomes"], 1)
            self.assertEqual(summary["worker_verification_additions"], 2)
            self.assertEqual(lineage["projection_source"], "db")
            self.assertEqual(lineage["projection_status"], "fresh")
            self.assertEqual(
                lineage["follow_up_items"][0]["objective"],
                "补充 DB 投影 readthrough 测试。",
            )
            self.assertTrue(lineage["follow_up_items"][0]["selected_next_goal"])
            self.assertEqual(
                lineage["follow_up_items"][0]["goal_memory_path"],
                str(goal_memory_path.resolve()),
            )
            self.assertEqual(lineage["worker_results"][0]["result_status"], "completed")
            self.assertEqual(
                lineage["worker_results"][0]["verification_additions"][0]["command"],
                ["python3", "-m", "unittest", "tests.test_projection"],
            )
            self.assertEqual(
                lineage["integration_outcomes"][0]["integration_verification_status"],
                "passed",
            )
            self.assertEqual(
                lineage["integration_outcomes"][0]["verification_additions"][0][
                    "verification_addition_status"
                ],
                "passed",
            )
            with sqlite3.connect(summary["db_path"]) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
            self.assertTrue(
                {
                    "follow_up_items",
                    "worker_results",
                    "integration_outcomes",
                    "worker_verification_additions",
                }.issubset(table_names)
            )


    def test_project_projection_db_check_detects_artifact_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            report_path = run_dir / "reports" / "final_report.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text("alpha\n", encoding="utf-8")
            rebuild_project_projection_db(work_root)

            report_path.write_text("bravo\n", encoding="utf-8")
            failed = check_project_projection_db(work_root)

            self.assertEqual(failed["check_status"], "failed")
            self.assertIn("artifact_digest", failed["mismatches"])


    def test_project_artifact_retention_plan_reads_rebuildable_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1", "notes": ["small"]},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1", "selected_files": ["a.py"]},
            )
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention planning fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            plan = projection_db.read_projected_artifact_retention_plan(work_root, limit=1)

            self.assertEqual(plan["projection_source"], "db")
            self.assertEqual(plan["plan_status"], "ready")
            self.assertFalse(plan["deletion_enabled"])
            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual(len(plan["candidates"]), 1)
            self.assertEqual(plan["candidates"][0]["retention_policy"], "rebuildable")
            self.assertIn(
                plan["candidates"][0]["artifact_type"],
                {"role_context", "repo_context"},
            )
            self.assertGreaterEqual(plan["retention_policies"]["authoritative"], 1)
            self.assertGreaterEqual(plan["retention_policies"]["rebuildable"], 2)


    def test_projected_artifact_retention_plan_validates_candidate_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1", "notes": ["small"]},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1", "selected_files": ["a.py"]},
            )
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention planning fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            plan = projection_db.read_projected_artifact_retention_plan(work_root, limit=1)

            self.assertEqual(plan["validation_status"], "passed")
            self.assertEqual(plan["validated_candidate_count"], 2)
            self.assertEqual(plan["invalid_candidate_count"], 0)
            self.assertEqual(plan["invalid_candidates"], [])
            self.assertEqual(plan["candidates"][0]["validation"]["status"], "passed")
            self.assertTrue(plan["candidates"][0]["validation"]["sha256_matches"])
            self.assertTrue(plan["candidates"][0]["validation"]["size_matches"])


    def test_project_artifact_retention_plan_returns_none_without_fresh_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )

            plan = projection_db.read_projected_artifact_retention_plan(work_root)

            self.assertIsNone(plan)


    def test_agentteam_cli_projection_readthrough_matches_fresh_db_after_stale_and_corrupt_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "projection-parity-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "projection-parity-project")
            _write_completed_operator_run(run_dir)
            _write_json(
                run_dir / "reports" / "final_report.json",
                {"run_id": "projection-parity-run"},
            )
            _write_json(
                work_root / "frozen" / "projection-parity-run" / "taskpack.yaml",
                {
                    "taskpack_id": "projection-parity-run",
                    "goal": "Projection parity fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {
                                "task_id": "optimize-pipeline",
                                "task_status": "done",
                            },
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
            rebuild_project_projection_db(work_root)

            expected_fresh = {
                "status": self._run_agentteam_json("status", "--project-root", str(repo)),
                "logs": self._run_agentteam_json(
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "2",
                ),
                "taskpack_list": self._run_agentteam_json(
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ),
                "report": self._run_agentteam_json("report", "--project-root", str(repo)),
            }
            for summary in expected_fresh.values():
                self.assertProjectionFreshMetadata(summary)
            expected = {
                "status": self._projection_parity_payload(
                    "status",
                    expected_fresh["status"],
                ),
                "logs": self._projection_parity_payload(
                    "logs",
                    expected_fresh["logs"],
                ),
                "report": self._projection_parity_payload(
                    "report",
                    expected_fresh["report"],
                ),
                "taskpack_list": self._projection_parity_payload(
                    "taskpack_list",
                    expected_fresh["taskpack_list"],
                ),
            }

            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1"},
            )

            stale = {
                "status": self._run_agentteam_json("status", "--project-root", str(repo)),
                "logs": self._run_agentteam_json(
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "2",
                ),
                "taskpack_list": self._run_agentteam_json(
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ),
                "report": self._run_agentteam_json("report", "--project-root", str(repo)),
            }
            for command, summary in stale.items():
                if command == "status":
                    self.assertProjectionFallbackHealthMetadata(summary, "stale")
                    self.assertEqual(summary.get("projection_next_action"), "run agentteam db rebuild")
                    self.assertIn(
                        "agentteam report --taskpack projection-parity-run",
                        summary.get("next_action") or "",
                    )
                else:
                    self.assertProjectionFallbackMetadata(summary, "stale")
                self.assertEqual(
                    self._projection_parity_payload(command, summary),
                    expected[command],
                )

            rebuild_project_projection_db(work_root)
            (work_root / "agentteam.db").write_text("not sqlite", encoding="utf-8")
            corrupt = {
                "status": self._run_agentteam_json("status", "--project-root", str(repo)),
                "logs": self._run_agentteam_json(
                    "logs",
                    "--project-root",
                    str(repo),
                    "--lines",
                    "2",
                ),
                "taskpack_list": self._run_agentteam_json(
                    "taskpack",
                    "list",
                    "--project-root",
                    str(repo),
                ),
                "report": self._run_agentteam_json("report", "--project-root", str(repo)),
            }
            for command, summary in corrupt.items():
                if command == "status":
                    self.assertProjectionFallbackHealthMetadata(summary, "corrupt")
                    self.assertEqual(summary.get("projection_next_action"), "run agentteam db rebuild")
                    self.assertIn(
                        "agentteam report --taskpack projection-parity-run",
                        summary.get("next_action") or "",
                    )
                else:
                    self.assertProjectionFallbackMetadata(summary, "corrupt")
                self.assertEqual(
                    self._projection_parity_payload(command, summary),
                    expected[command],
                )


    def test_agentteam_cli_gc_dry_run_explains_projected_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "projection-run"})
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
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
            artifact_projection = summary["artifact_projection"]
            self.assertEqual(artifact_projection["projection_source"], "db")
            self.assertEqual(artifact_projection["check_status"], "passed")
            self.assertGreaterEqual(artifact_projection["total_artifacts"], 3)
            self.assertGreater(artifact_projection["total_bytes"], 0)
            self.assertGreaterEqual(
                artifact_projection["retention_policies"]["authoritative"],
                1,
            )
            self.assertGreaterEqual(
                artifact_projection["retention_policies"]["rebuildable"],
                1,
            )
            self.assertIn("report", artifact_projection["artifact_types"])
            self.assertIn("repo_context", artifact_projection["artifact_types"])
            explanation_policies = {
                item["retention_policy"]
                for item in artifact_projection["dry_run_explanations"]
            }
            self.assertIn("authoritative", explanation_policies)
            self.assertIn("rebuildable", explanation_policies)


    def test_agentteam_cli_gc_artifacts_json_lists_rebuildable_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1"},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention planning fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                    "--artifact-limit",
                    "1",
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
            plan = summary["artifact_retention_plan"]
            self.assertEqual(plan["projection_source"], "db")
            self.assertEqual(plan["plan_status"], "ready")
            self.assertFalse(plan["deletion_enabled"])
            self.assertEqual(plan["candidate_count"], 2)
            self.assertEqual(len(plan["candidates"]), 1)
            self.assertEqual(plan["candidates"][0]["retention_policy"], "rebuildable")


    def test_agentteam_cli_gc_delete_artifacts_deletes_only_validated_rebuildable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            report_path = run_dir / "reports" / "final_report.json"
            event_path = run_dir / "events.jsonl"
            repo_context = run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json"
            role_context = run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json"
            taskpack_path = work_root / "frozen" / "retention-run" / "taskpack.json"
            _write_json(report_path, {"run_id": "retention-run"})
            _write_json(repo_context, {"repo_context_schema_version": "repo_context.v1"})
            _write_json(role_context, {"context_schema_version": "role_context.v1"})
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention delete fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                    "--delete-artifacts",
                    "--artifact-limit",
                    "10",
                    "--force",
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
            deletion = summary["artifact_retention_plan"]["artifact_deletion"]
            self.assertEqual(deletion["deletion_status"], "completed")
            self.assertEqual(deletion["deleted_count"], 2)
            self.assertFalse(repo_context.exists())
            self.assertFalse(role_context.exists())
            self.assertTrue(report_path.exists())
            self.assertTrue(event_path.exists())
            self.assertTrue(taskpack_path.exists())
            self.assertEqual(summary["artifact_retention_plan"]["next_action"], "run agentteam db rebuild")


    def test_agentteam_cli_gc_delete_artifacts_blocks_when_candidate_validation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            repo_context = run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json"
            _write_json(repo_context, {"repo_context_schema_version": "repo_context.v1"})
            _write_json(
                work_root / "frozen" / "retention-run" / "taskpack.json",
                {
                    "taskpack_id": "retention-run",
                    "goal": "Retention delete fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)
            _write_json(repo_context, {"repo_context_schema_version": "repo_context.v1", "changed": True})

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                    "--delete-artifacts",
                    "--force",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            error = json.loads(completed.stdout or completed.stderr)
            self.assertEqual(error["error"], "artifact deletion blocked")
            self.assertEqual(error["artifact_deletion_status"], "blocked")
            self.assertTrue(repo_context.exists())


    def test_agentteam_cli_start_versions_trace_artifacts_without_worktrees(self):
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
                    "artifact-project",
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
                    "Create a versioned trace snapshot.",
                    "--taskpack-id",
                    "trace-artifacts",
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
            snapshot = summary["artifact_snapshot"]
            artifacts_root = work_root / "artifacts"
            self.assertEqual(snapshot["snapshot_status"], "committed")
            self.assertEqual(snapshot["artifacts_root"], str(artifacts_root.resolve()))
            self.assertTrue((artifacts_root / ".git").exists())
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "reports" / "final_report.md").exists())
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "reports" / "final_report.json").exists())
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "events.jsonl").exists())
            state_snapshot = artifacts_root / "runs" / "trace-artifacts" / "state"
            self.assertTrue(
                (state_snapshot / "two_phase_scheduler_state.json").exists()
                or (state_snapshot / "scheduler_state.json").exists()
            )
            self.assertTrue((artifacts_root / "runs" / "trace-artifacts" / "taskpack" / "taskpack.yaml").exists())
            tracked_completed = subprocess.run(
                ["git", "-C", str(artifacts_root), "ls-files"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(tracked_completed.returncode, 0, tracked_completed.stderr)
            tracked_files = tracked_completed.stdout.splitlines()
            self.assertIn("runs/trace-artifacts/reports/final_report.md", tracked_files)
            self.assertIn("runs/trace-artifacts/taskpack/taskpack.yaml", tracked_files)
            self.assertFalse(any(path.startswith("runs/trace-artifacts/worktrees/") for path in tracked_files))
            self.assertFalse(any(path.startswith("runs/trace-artifacts/integration/") for path in tracked_files))
            self.assertTrue(snapshot["commit_sha"])
            self.assertIn("artifact_trace:", completed.stderr)


    def test_agentteam_cli_status_can_replay_fresh_projection_db_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-db-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)

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
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(summary["latest_run"], "status-db-run")
            self.assertEqual(summary["tasks"]["done"], 1)
            self.assertEqual(summary["tasks"]["blocked"], 0)

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

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("projection_source: db", text_completed.stdout)
            self.assertIn("projection_status: fresh", text_completed.stdout)


    def test_agentteam_cli_status_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-db-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {
                                "task_id": "optimize-pipeline",
                                "task_status": "done",
                            },
                        },
                        sort_keys=True,
                    )
                    + "\n"
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
            self.assertProjectionFallbackHealthMetadata(summary, "stale")
            self.assertEqual(summary["projection_next_action"], "run agentteam db rebuild")
            self.assertIn("agentteam report --taskpack status-db-run", summary["next_action"])
            self.assertIn("agentteam paths --taskpack status-db-run", summary["next_action"])
            self.assertEqual(summary["latest_run"], "status-db-run")


    def test_agentteam_cli_status_prioritizes_integration_guidance_over_projection_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "status-actionable-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "status-actionable-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {
                                "task_id": "optimize-pipeline",
                                "task_status": "done",
                            },
                        },
                        sort_keys=True,
                    )
                    + "\n"
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
            self.assertEqual(summary["projection_warning"], "projection_db_unavailable")
            self.assertIn("projection_next_action", summary)
            self.assertEqual(summary["projection_next_action"], "run agentteam db rebuild")
            self.assertIn("agentteam report --taskpack status-actionable-run", summary["next_action"])
            self.assertIn("agentteam paths --taskpack status-actionable-run", summary["next_action"])
            self.assertIn("integration baseline", summary["operator_hint"])
            self.assertNotEqual(summary["next_action"], summary["projection_next_action"])

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

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("projection_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("projection_next_action: run agentteam db rebuild", text_completed.stdout)
            self.assertIn(
                "next_action: agentteam report --taskpack status-actionable-run",
                text_completed.stdout,
            )


    def test_agentteam_cli_logs_can_read_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)

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
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["events"][0]["event_type"], "run_completed")


    def test_agentteam_cli_logs_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)
            rebuild_project_projection_db(work_root)
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "event_id": "EVT-0002",
                            "event_type": "backlog_updated",
                            "sequence": 2,
                            "time": "2026-06-12T00:00:01Z",
                            "payload": {"task_id": "optimize-pipeline"},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

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
            self.assertProjectionFallbackMetadata(summary, "stale")
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(summary["events"][0]["event_type"], "backlog_updated")


    def test_agentteam_cli_logs_falls_back_when_projection_db_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "logs-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "logs-project")
            _write_completed_operator_run(run_dir)
            work_root.mkdir(parents=True, exist_ok=True)
            (work_root / "agentteam.db").write_text("not sqlite", encoding="utf-8")

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
            self.assertProjectionFallbackMetadata(summary, "corrupt")
            self.assertEqual(summary["event_count"], 1)


    def test_agentteam_cli_taskpack_list_can_read_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "list-db-project")
            _write_completed_operator_run(work_root / "runs" / "listed-db")
            _write_json(
                work_root / "frozen" / "listed-db" / "taskpack.json",
                {
                    "taskpack_id": "listed-db",
                    "goal": "List through projection DB.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "taskpack",
                    "list",
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

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            summary = json.loads(list_completed.stdout)
            self.assertProjectionFreshMetadata(summary)
            self.assertEqual(summary["frozen_count"], 1)
            self.assertEqual(summary["taskpacks"][0]["taskpack_id"], "listed-db")
            self.assertEqual(summary["taskpacks"][0]["run_status"], "idle")


    def test_agentteam_cli_taskpack_list_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "list-db-project")
            _write_json(
                work_root / "frozen" / "listed-db" / "taskpack.json",
                {
                    "taskpack_id": "listed-db",
                    "goal": "List through projection DB.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)
            _write_json(
                work_root / "frozen" / "listed-after-rebuild" / "taskpack.json",
                {
                    "taskpack_id": "listed-after-rebuild",
                    "goal": "Force stale projection.",
                    "validation": {"status": "accepted"},
                },
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
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            summary = json.loads(list_completed.stdout)
            self.assertProjectionFallbackMetadata(summary, "stale")
            self.assertEqual(summary["frozen_count"], 2)
            self.assertEqual(
                {item["taskpack_id"] for item in summary["taskpacks"]},
                {"listed-db", "listed-after-rebuild"},
            )


    def test_semantic_feedback_proposal_helper_writes_review_artifact(self):
        from agentteam_runtime.semantic_feedback import write_semantic_feedback_proposal

        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            proposal = write_semantic_feedback_proposal(
                work_root=work_root,
                proposal_id="design-gap-1",
                source_report={
                    "run_id": "first-pass",
                    "run_dir": "/tmp/work/runs/first-pass",
                    "report_path": "/tmp/work/runs/first-pass/reports/final_report.md",
                },
                target_artifacts=["design/system.md"],
                summary="实现证据显示系统边界需要补充。",
                rationale="worker 在实现阶段发现设计文档没有说明 review gate 的归属。",
                created_by="test",
                created_at="2026-06-14T00:00:00Z",
            )

            proposal_path = Path(proposal["proposal_path"])
            self.assertTrue(proposal_path.exists())
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["proposal_schema_version"], "semantic_feedback_proposal.v1")
            self.assertEqual(payload["proposal_status"], "pending_review")
            self.assertEqual(payload["proposal_id"], "design-gap-1")
            self.assertEqual(payload["source_taskpack_id"], "first-pass")
            self.assertEqual(payload["source_report_path"], "/tmp/work/runs/first-pass/reports/final_report.md")
            self.assertEqual(payload["target_artifacts"], ["design/system.md"])
            self.assertEqual(payload["summary"], "实现证据显示系统边界需要补充。")
            self.assertEqual(payload["rationale"], "worker 在实现阶段发现设计文档没有说明 review gate 的归属。")
            self.assertIn("does not mutate", payload["authority_boundary"])


    def test_agentteam_cli_feedback_propose_writes_pending_review_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "feedback-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "first-pass")
            report_path = run_dir / "reports" / "final_report.md"

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "feedback",
                    "propose",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                    "--proposal-id",
                    "design-gap-1",
                    "--target-artifact",
                    "design/system.md",
                    "--summary",
                    "实现证据显示系统边界需要补充。",
                    "--rationale",
                    "worker 在实现阶段发现设计文档没有说明 review gate 的归属。",
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
            self.assertEqual(summary["proposal_status"], "pending_review")
            self.assertEqual(summary["source_taskpack_id"], "first-pass")
            proposal_path = Path(summary["proposal_path"])
            self.assertTrue(proposal_path.exists())
            self.assertIn(str(work_root / "semantic_feedback"), str(proposal_path))
            payload = json.loads(proposal_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["target_artifacts"], ["design/system.md"])
            self.assertTrue(report_path.exists())


    def test_codex_taskpack_author_prompt_uses_direct_artifact_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "direct-author"
            author_context_dir = tmp_path / "drafts" / ".direct-author-author"
            _init_repo(repo)

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
            )

            self.assertIn("Direct artifact-production protocol:", prompt)
            self.assertIn("Do not read skill docs", prompt)
            self.assertIn("write the five required taskpack files before optional exploration", prompt)


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
            self.assertEqual(loaded["taskpack"]["semantic_contract_version"], "task_semantics.v1")
            self.assertEqual(loaded["taskpack"]["project_root"], str(repo.resolve()))
            self.assertEqual(loaded["taskpack"]["original_goal"], "Improve fixture behavior without broad writes.")
            task = _implementation_item(loaded["backlog"])
            self.assertIn("goal_alignment", task)
            self.assertIn("required_deliverables", task)
            self.assertIn("verification_summary", task["required_deliverables"])
            self.assertEqual(loaded["verification"]["command"], ["python3", "-m", "unittest", "discover"])
            self.assertEqual(task["write_scope"], ["src/"])


    def test_validate_taskpack_rejects_invalid_artifact_fields_without_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Reject malformed task artifact fields.",
                draft_root=drafts,
                taskpack_id="malformed-artifact-fields",
                write_scope=["src/"],
            )
            backlog_path = Path(result["taskpack_dir"]) / "backlog.json"
            backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
            repo_map_item = backlog["items"][0]
            implementation_item = _implementation_item(backlog)
            repo_map_item["expected_output_artifacts"] = ["../outside.json"]
            implementation_item["input_artifacts"] = ".agentteam/generated/repo_map_handoff.json"
            backlog_path.write_text(json.dumps(backlog), encoding="utf-8")

            with self.assertRaises(TaskpackValidationError) as raised:
                validate_taskpack(result["taskpack_dir"])

            message = str(raised.exception)
            self.assertIn("expected_output_artifacts", message)
            self.assertIn("input_artifacts", message)


    def test_pre04_08b_launcher_supports_versioned_artifact_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            repo = tmp_path / "repo"
            _init_repo(repo)
            release = _pre04_release_fixture(
                work_root,
                "release-1",
                _git_head(repo),
                runtime_source=Path(__file__).resolve().parents[1],
            )
            older = publish_implementation_run(
                work_root,
                project_key="pre04-versioned",
                run_id="older-run",
                taskpack_id="run-1",
                release_identity=release,
            )
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04-versioned",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                    one_shot=True,
                ),
            )
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Exercise versioned immutable runtime binding.",
                draft_root=work_root / "drafts" / "v2",
                taskpack_id="run-1",
                write_scope=["src/"],
            )
            _set_taskpack_runtime_backend(
                draft["taskpack_dir"],
                "fake",
            )
            taskpack_path = Path(draft["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(
                taskpack_path.read_text(encoding="utf-8")
            )
            taskpack["context"] = {
                "runtime_release_id": release["release_id"],
                "runtime_release_source_commit": release["source_commit"],
                "git_object_format": release["git_object_format"],
            }
            _write_json(taskpack_path, taskpack)
            frozen_result = freeze_taskpack(
                draft["taskpack_dir"],
                work_root / "frozen" / "v2",
            )
            frozen = Path(frozen_result["frozen_taskpack_dir"])
            flat_frozen = Path(
                freeze_taskpack(
                    draft["taskpack_dir"],
                    work_root / "frozen",
                )["frozen_taskpack_dir"]
            )
            run_root = work_root / "runs" / "v2"
            launcher = runpy.run_path(
                str(Path(__file__).resolve().parents[4] / "agentteam")
            )

            for mismatched_frozen, mismatched_run_root in (
                (frozen, work_root / "runs"),
                (frozen, work_root / "runs" / "v3"),
                (flat_frozen, work_root / "runs" / "v2"),
            ):
                with self.assertRaises(launcher["LauncherError"]):
                    launcher["_initial_run_selection"](
                        [
                            "run",
                            str(mismatched_frozen),
                            "--run-root",
                            str(mismatched_run_root),
                        ]
                    )
            selection = launcher["_initial_run_selection"](
                [
                    "run",
                    str(frozen),
                    "--run-root",
                    str(run_root),
                ]
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [
                    str(Path(__file__).resolve().parents[4] / "agentteam"),
                    "run",
                    str(frozen),
                    "--run-root",
                    str(run_root),
                    "--one-shot",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(selection["work_root"], str(work_root))
            self.assertEqual(selection["release"]["release_id"], "release-1")
            self.assertEqual(
                selection["run_dir"],
                str(run_root / "run-1"),
            )
            bound = validate_run_binding(
                run_root / "run-1",
                expected_project_key="pre04-versioned",
            )
            self.assertEqual(bound["binding"]["release_id"], "release-1")
            latest = select_latest_implementation_run(
                work_root,
                expected_project_key="pre04-versioned",
            )
            self.assertEqual(
                validate_run_binding(
                    older["run_dir"],
                    expected_project_key="pre04-versioned",
                )["identity"]["creation_sequence"],
                1,
            )
            self.assertEqual(latest["identity"]["creation_sequence"], 2)
            self.assertEqual(
                latest["run_dir"],
                str(run_root / "run-1"),
            )
            explicit_selection = launcher["_existing_run_selection"](
                [
                    "continue",
                    "--project-root",
                    str(repo),
                    "--run-dir",
                    str(run_root / "run-1"),
                ],
                "continue",
            )
            taskpack_selection = launcher["_existing_run_selection"](
                [
                    "continue",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "run-1",
                ],
                "continue",
            )
            latest_selection = launcher["_existing_run_selection"](
                [
                    "continue",
                    "--project-root",
                    str(repo),
                ],
                "continue",
            )
            for resume_selection in (
                explicit_selection,
                taskpack_selection,
                latest_selection,
            ):
                self.assertEqual(
                    resume_selection["run_dir"],
                    str(run_root / "run-1"),
                )
                self.assertEqual(
                    resume_selection["frozen_taskpack_dir"],
                    str(frozen),
                )

            for resume_args in (
                [
                    "--run-dir",
                    str(run_root / "run-1"),
                ],
                [
                    "--taskpack",
                    "run-1",
                ],
                [],
            ):
                continued = subprocess.run(
                    [
                        str(Path(__file__).resolve().parents[4] / "agentteam"),
                        "continue",
                        "--project-root",
                        str(repo),
                        *resume_args,
                        "--one-shot",
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(continued.returncode, 0, continued.stderr)
                continue_result = json.loads(continued.stdout)
                self.assertEqual(
                    continue_result["paths"]["run_dir"],
                    str(run_root / "run-1"),
                )
                self.assertEqual(
                    continue_result["paths"]["frozen_taskpack_dir"],
                    str(frozen),
                )
            with self.assertRaises(launcher["LauncherError"]):
                launcher["_initial_run_selection"](
                    [
                        "run",
                        str(frozen),
                        "--run-root",
                        str(tmp_path / "other-work" / "runs" / "v2"),
                    ]
                )

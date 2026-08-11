try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class ReportingMixin:
    def test_project_projection_db_check_reports_readthrough_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            taskpack_path = work_root / "frozen" / "projection-run" / "taskpack.json"
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "projection-run",
                    "goal": "Projection freshness fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            missing = check_project_projection_db(work_root)
            self.assertEqual(missing.get("projection_source"), "files")
            self.assertEqual(missing.get("projection_status"), "missing")
            self.assertEqual(missing.get("projection_warning"), "projection_db_unavailable")
            self.assertEqual(missing.get("next_action"), "run agentteam db rebuild")
            self.assertEqual(missing.get("operator_hint"), "agentteam db rebuild")
            self.assertEqual(missing.get("projection_db_path"), missing["db_path"])
            self.assertIn("db_missing", missing["mismatches"])

            rebuild_project_projection_db(work_root)
            fresh = check_project_projection_db(work_root)
            self.assertEqual(fresh.get("projection_source"), "db")
            self.assertEqual(fresh.get("projection_status"), "fresh")
            self.assertEqual(fresh.get("projection_db_path"), fresh["db_path"])
            self.assertNotIn("projection_warning", fresh)
            self.assertEqual(fresh["mismatches"], [])

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
            stale_event = check_project_projection_db(work_root)
            self.assertEqual(stale_event.get("projection_source"), "files")
            self.assertEqual(stale_event.get("projection_status"), "stale")
            self.assertEqual(stale_event.get("projection_warning"), "projection_db_unavailable")
            self.assertEqual(stale_event.get("next_action"), "run agentteam db rebuild")
            self.assertIn("events", stale_event["mismatches"])

            rebuild_project_projection_db(work_root)
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "projection-run",
                    "goal": "Projection freshness fixture changed.",
                    "validation": {"status": "accepted"},
                },
            )
            stale_taskpack = check_project_projection_db(work_root)
            self.assertEqual(stale_taskpack.get("projection_source"), "files")
            self.assertEqual(stale_taskpack.get("projection_status"), "stale")
            self.assertIn("artifact_digest", stale_taskpack["mismatches"])

            rebuild_project_projection_db(work_root)
            db_path = Path(fresh["db_path"])
            db_path.write_text("not sqlite", encoding="utf-8")
            corrupt = check_project_projection_db(work_root)
            self.assertEqual(corrupt.get("projection_source"), "files")
            self.assertEqual(corrupt.get("projection_status"), "corrupt")
            self.assertEqual(corrupt.get("projection_warning"), "projection_db_unavailable")
            self.assertIn("db_unreadable", corrupt["mismatches"])


    def test_projected_readers_can_report_normalized_fallback_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "projection-run")
            _write_json(
                work_root / "frozen" / "projection-run" / "taskpack.json",
                {
                    "taskpack_id": "projection-run",
                    "goal": "Projection reader fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            taskpacks = projection_db.read_projected_taskpacks(work_root)
            events = projection_db.read_projected_run_events(work_root, "projection-run")
            self.assertEqual(taskpacks["projection_source"], "db")
            self.assertEqual(taskpacks.get("projection_status"), "fresh")
            self.assertEqual(events["projection_source"], "db")
            self.assertEqual(events.get("projection_status"), "fresh")
            self.assertEqual([event["event_id"] for event in events["events"]], ["EVT-0001"])

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

            self.assertIsNone(projection_db.read_projected_taskpacks(work_root))
            self.assertIsNone(projection_db.read_projected_run_events(work_root, "projection-run"))
            try:
                taskpack_fallback = projection_db.read_projected_taskpacks(
                    work_root,
                    include_fallback_status=True,
                )
                event_fallback = projection_db.read_projected_run_events(
                    work_root,
                    "projection-run",
                    include_fallback_status=True,
                )
            except TypeError as exc:
                self.fail(f"projection readers should expose fallback status: {exc}")

            self.assertEqual(taskpack_fallback["projection_source"], "files")
            self.assertEqual(taskpack_fallback["projection_status"], "stale")
            self.assertEqual(taskpack_fallback["projection_warning"], "projection_db_unavailable")
            self.assertEqual(taskpack_fallback["next_action"], "run agentteam db rebuild")
            self.assertNotIn("taskpacks", taskpack_fallback)
            self.assertEqual(event_fallback["projection_source"], "files")
            self.assertEqual(event_fallback["projection_status"], "stale")
            self.assertNotIn("events", event_fallback)

            Path(taskpack_fallback["db_path"]).unlink()
            missing_fallback = projection_db.read_projected_taskpacks(
                work_root,
                include_fallback_status=True,
            )
            self.assertEqual(missing_fallback["projection_status"], "missing")

            Path(missing_fallback["db_path"]).write_text("not sqlite", encoding="utf-8")
            corrupt_fallback = projection_db.read_projected_run_events(
                work_root,
                "projection-run",
                include_fallback_status=True,
            )
            self.assertEqual(corrupt_fallback["projection_status"], "corrupt")
            self.assertIn("db_unreadable", corrupt_fallback["check"]["mismatches"])


    def test_project_projection_db_rebuild_indexes_artifacts_and_run_stats(self):
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
                        "task_id": "optimize-pipeline",
                        "attempt_id": "optimize-pipeline-ATTEMPT-001",
                        "evidence_level": "L2",
                        "evidence_status": "complete",
                        "trace_carrier": [{"type": "file", "path": "reports/final_report.md"}],
                        "missing_evidence": [],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "projection-run"})
            report_markdown = run_dir / "reports" / "final_report.md"
            report_markdown.parent.mkdir(parents=True, exist_ok=True)
            report_markdown.write_text("completed report\n", encoding="utf-8")
            patch_path = run_dir / "steps" / "STEP-0001-optimize-pipeline" / "result.patch"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text("diff --git a/a b/a\n", encoding="utf-8")
            _write_json(
                run_dir / "role_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"context_schema_version": "role_context.v1"},
            )
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )
            taskpack_path = work_root / "frozen" / "projection-run" / "taskpack.json"
            _write_json(
                taskpack_path,
                {
                    "taskpack_id": "projection-run",
                    "goal": "Project projection fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            summary = rebuild_project_projection_db(work_root)

            self.assertGreaterEqual(summary["artifacts"], 7)
            self.assertEqual(summary["run_stats"], 1)
            self.assertGreater(summary["artifact_bytes"], 0)
            db_path = Path(summary["db_path"])
            expected_report_sha = hashlib.sha256(
                report_markdown.read_bytes()
            ).hexdigest()
            with sqlite3.connect(db_path) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type='table'"
                    )
                }
                artifact_types = {
                    row[0]
                    for row in connection.execute(
                        "select distinct artifact_type from artifacts"
                    ).fetchall()
                }
                report_row = connection.execute(
                    """
                    select size_bytes, sha256, retention_policy
                    from artifacts
                    where path = ?
                    """,
                    (str(report_markdown.resolve()),),
                ).fetchone()
                stats_row = connection.execute(
                    """
                    select run_id, input_tokens, output_tokens, total_tokens,
                           artifact_count, artifact_bytes
                    from run_stats
                    where run_id = ?
                    """,
                    ("projection-run",),
                ).fetchone()
                evidence_row = connection.execute(
                    """
                    select content_size_bytes, content_sha256, source_path
                    from evidence_summaries
                    where run_id = ? and task_id = ?
                    """,
                    ("projection-run", "optimize-pipeline"),
                ).fetchone()

            self.assertTrue({"artifacts", "run_stats"}.issubset(table_names))
            self.assertTrue(
                {
                    "event_log",
                    "report",
                    "patch",
                    "taskpack",
                    "state_snapshot",
                    "role_context",
                    "repo_context",
                }.issubset(artifact_types)
            )
            self.assertEqual(
                report_row,
                (len("completed report\n".encode("utf-8")), expected_report_sha, "authoritative"),
            )
            self.assertEqual(stats_row[:4], ("projection-run", 1200, 300, 1500))
            self.assertGreaterEqual(stats_row[4], 7)
            self.assertGreater(stats_row[5], 0)
            self.assertGreater(evidence_row[0], 0)
            self.assertEqual(len(evidence_row[1]), 64)
            self.assertEqual(evidence_row[2], str(state_path.resolve()))


    def test_project_stats_reads_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            state_path = run_dir / "state" / "two_phase_scheduler_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["steps"] = [
                {
                    "task_id": "optimize-pipeline",
                    "result": {
                        "task_id": "optimize-pipeline",
                        "attempt_id": "optimize-pipeline-ATTEMPT-001",
                        "evidence_level": "L2",
                        "evidence_status": "complete",
                        "trace_carrier": [],
                        "missing_evidence": [],
                    },
                }
            ]
            _write_json(state_path, state)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            stats = projection_db.build_project_stats(work_root)

            self.assertEqual(stats["projection_source"], "db")
            self.assertEqual(stats["check_status"], "passed")
            self.assertEqual(stats["runs"], 1)
            self.assertEqual(stats["taskpacks"], 1)
            self.assertEqual(stats["events"], 1)
            self.assertEqual(stats["tasks"], 1)
            self.assertEqual(stats["evidence"], {"complete": 1})
            self.assertGreaterEqual(stats["artifacts"]["total_count"], 4)
            self.assertGreater(stats["artifacts"]["total_bytes"], 0)
            self.assertEqual(stats["token_usage"]["total_tokens"], 1500)


    def test_project_stats_falls_back_to_files_when_projection_db_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            stats = projection_db.build_project_stats(work_root)

            self.assertEqual(stats["projection_source"], "files")
            self.assertEqual(stats["check_status"], "failed")
            self.assertEqual(stats["runs"], 1)
            self.assertEqual(stats["taskpacks"], 1)
            self.assertEqual(stats["events"], 1)
            self.assertEqual(stats["tasks"], 1)
            self.assertEqual(stats["projection_warning"], "projection_db_unavailable")
            self.assertEqual(stats["next_action"], "run agentteam db rebuild")
            self.assertGreaterEqual(stats["artifacts"]["total_count"], 4)
            self.assertEqual(stats["token_usage"]["total_tokens"], 1500)


    def test_agentteam_cli_stats_json_reads_fresh_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "stats-cli")
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats CLI fixture.",
                    "validation": {"status": "accepted"},
                },
            )
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stats",
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
            self.assertEqual(summary["project"], "stats-cli")
            self.assertEqual(summary["projection_source"], "db")
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["taskpacks"], 1)
            self.assertEqual(summary["token_usage"]["total_tokens"], 1500)


    def test_agentteam_cli_stats_json_falls_back_to_files_without_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "stats-cli")
            run_dir = _write_completed_operator_run(work_root / "runs" / "stats-run")
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "stats-run"})
            _write_json(
                work_root / "frozen" / "stats-run" / "taskpack.json",
                {
                    "taskpack_id": "stats-run",
                    "goal": "Project stats CLI fixture.",
                    "validation": {"status": "accepted"},
                },
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "stats",
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
            self.assertEqual(summary["projection_source"], "files")
            self.assertEqual(summary["check_status"], "failed")
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["taskpacks"], 1)
            self.assertEqual(summary["token_usage"]["total_tokens"], 1500)


    def test_completion_summary_reports_evidence_gaps_when_worker_omits_fields(self):
        summary = build_completion_summary(
            run_id="gap-run",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-001",
                    "status": "implementation completed",
                }
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertIn("No changed files were reported.", summary["evidence_gaps"])
        self.assertIn("No verification evidence was reported.", summary["evidence_gaps"])
        self.assertIn("Evidence gaps:", lines)
        self.assertIn("- No verification evidence was reported.", lines)


    def test_completion_summary_does_not_treat_explicit_no_change_task_as_missing_files(self):
        summary = build_completion_summary(
            run_id="audit-run",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "AUDIT-001",
                    "status": "investigation completed",
                    "work_type": "code_investigation",
                    "what_changed": ["Reviewed the run and found no safe in-repo code change."],
                    "changed_files": [],
                    "verification": ["read-only audit completed"],
                    "integration": "not_applicable",
                    "no_code_changes_required": True,
                }
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertNotIn("No changed files were reported.", summary["evidence_gaps"])
        self.assertEqual(summary["changed_files_note"], "No source files changed; this task was completed as a no-change investigation.")
        self.assertEqual(summary["changed_files_note_zh"], "未修改源文件；该任务是无需代码变更的调查任务。")
        self.assertIn("Changed files: No source files changed; this task was completed as a no-change investigation.", lines)
        self.assertIn(
            "涉及文件：未修改源文件；该任务是无需代码变更的调查任务。",
            summary["chinese_operator_brief"],
        )


    def test_completion_summary_reports_evidence_status_counts(self):
        summary = build_completion_summary(
            run_id="evidence-run",
            run_status="completed",
            task_count=2,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-001",
                    "status": "implementation completed",
                    "what_changed": ["Changed one file."],
                    "changed_files": ["src/a.py"],
                    "verification": ["unit: passed"],
                    "integration": "passed",
                    "evidence_status": "complete",
                },
                {
                    "task_id": "TASK-002",
                    "status": "implementation completed, integration blocked",
                    "what_changed": ["Changed another file."],
                    "changed_files": ["src/b.py"],
                    "verification": ["unit: passed"],
                    "integration": "blocked",
                    "evidence_status": "incomplete",
                    "missing_evidence": ["review_result"],
                },
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertEqual(
            summary["evidence_status_counts"],
            {"complete": 1, "incomplete": 1, "blocked": 0, "escalated": 0},
        )
        self.assertIn("Evidence status:", lines)
        self.assertIn("- incomplete: 1", lines)


    def test_completion_summary_builds_deterministic_chinese_operator_brief(self):
        summary = build_completion_summary(
            run_id="brief-run",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-001",
                    "status": "implementation completed",
                    "what_changed": ["Optimized the gesture scoring pipeline."],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch before merging.",
                    "next_steps": ["Run full validation."],
                }
            ],
        )
        lines = []

        extend_completion_summary_lines(lines, summary)

        self.assertEqual(
            summary["chinese_operator_brief"],
            [
                "本次运行已完成，共 1 个任务，0 个阻塞。",
                "主要变更：Optimized the gesture scoring pipeline.",
                "涉及文件：gesture_recognition/sim_eval.py",
                "验证情况：unit_tests: passed",
                "集成状态：已通过",
                "合并建议：Review accepted patch before merging.",
                "下一步：Run full validation.",
                (
                    "下一步原因：The run completed with a recommended next implementation step."
                    "；相关下一步：Run full validation."
                ),
            ],
        )
        self.assertIn("中文简报:", lines)
        self.assertIn("- 本次运行已完成，共 1 个任务，0 个阻塞。", lines)


    def test_completion_summary_includes_chinese_operator_digest(self):
        summary = build_completion_summary(
            run_id="taskpack-7",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "optimize-pipeline",
                    "status": "implementation completed",
                    "what_changed": ["优化了手势评分流水线的数据窗口复制。"],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "measured_result": ["算法窗口复制阶段耗时下降 2%。"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch before merging.",
                    "next_steps": ["在比赛 QEMU 环境复测端到端延迟。"],
                }
            ],
            integration_baseline={"branch": "agentteam/run/taskpack-7/integration"},
        )

        self.assertEqual(
            summary["operator_digest"],
            [
                "做了什么：优化了手势评分流水线的数据窗口复制。",
                "涉及文件：gesture_recognition/sim_eval.py",
                "验证结果：unit_tests: passed",
                "实际结果：算法窗口复制阶段耗时下降 2%。",
                "风险：存在 review gate；source merge、push、release activation 仍需 operator 审阅。",
                "合并建议：Review accepted patch before merging.",
                "下一步：在比赛 QEMU 环境复测端到端延迟。",
                (
                    "下一步原因：Accepted changes have an integration baseline and the worker "
                    "recommended a next step.；相关下一步：在比赛 QEMU 环境复测端到端延迟。"
                ),
            ],
        )
        lines = []
        extend_completion_summary_lines(lines, summary)
        self.assertIn("中文工作汇报:", lines)
        self.assertIn("- 做了什么：优化了手势评分流水线的数据窗口复制。", lines)


    def test_completion_summary_aggregates_multiple_tasks_in_operator_digest(self):
        summary = build_completion_summary(
            run_id="multi-task-run",
            run_status="completed",
            task_count=2,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-A",
                    "status": "implementation completed",
                    "what_changed": ["实现任务 A 的代码路径。"],
                    "changed_files": ["src/a.py"],
                    "verification": ["unit-a: passed"],
                    "measured_result": ["A 延迟下降 2%。"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch A.",
                    "next_steps": ["继续验证 A。"],
                },
                {
                    "task_id": "TASK-B",
                    "status": "implementation completed",
                    "what_changed": ["补充任务 B 的验证入口。"],
                    "changed_files": ["src/b.py"],
                    "verification": ["unit-b: passed"],
                    "measured_result": ["B 报告覆盖两个任务。"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch B.",
                    "next_steps": ["继续验证 B。"],
                },
            ],
        )

        self.assertEqual(
            summary["operator_digest"],
            [
                "做了什么：实现任务 A 的代码路径。；补充任务 B 的验证入口。",
                "涉及文件：src/a.py；src/b.py",
                "验证结果：unit-a: passed；unit-b: passed",
                "实际结果：A 延迟下降 2%。；B 报告覆盖两个任务。",
                "合并建议：Review accepted patch A.；Review accepted patch B.",
                "下一步：继续验证 A。；继续验证 B。",
                (
                    "下一步原因：The run completed with a recommended next implementation step."
                    "；相关下一步：继续验证 A。"
                ),
            ],
        )


    def test_completion_summary_includes_follow_up_recommendation(self):
        summary = build_completion_summary(
            run_id="taskpack-7",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "optimize-pipeline",
                    "status": "implementation completed",
                    "what_changed": ["优化了手势评分流水线。"],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "integration": "passed",
                    "merge_recommendation": "Review accepted patch before merging.",
                    "next_steps": ["在比赛 QEMU 环境复测端到端延迟。"],
                }
            ],
            integration_baseline={"branch": "agentteam/run/taskpack-7/integration"},
        )

        recommendation = summary["follow_up_recommendation"]
        self.assertEqual(recommendation["action"], "integrate_then_next")
        self.assertEqual(recommendation["integrate_command"], "agentteam integrate --taskpack taskpack-7")
        self.assertEqual(
            recommendation["next_command"],
            'agentteam next --from-taskpack taskpack-7 --goal "在比赛 QEMU 环境复测端到端延迟。"',
        )
        lines = []
        extend_completion_summary_lines(lines, summary)
        self.assertIn("Follow-up recommendation:", lines)
        self.assertIn("- action: integrate_then_next", lines)


    def test_run_status_summary_reports_evidence_counts_from_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = work_root / "runs" / "evidence-run"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "backlog": {"items": []},
                        "inflight_attempts": [],
                        "steps": [
                            {
                                "task_id": "TASK-001",
                                "result": {
                                    "evidence_status": "complete",
                                    "evidence_level": "L1",
                                },
                            },
                            {
                                "task_id": "TASK-002",
                                "result": {
                                    "evidence_status": "incomplete",
                                    "evidence_level": "L2",
                                },
                            },
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            summary = _build_run_status_summary(profile, run_dir)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                _write_status_text(summary)

            self.assertEqual(
                summary["evidence"],
                {"complete": 1, "incomplete": 1, "blocked": 0, "escalated": 0},
            )
            self.assertIn(
                "evidence: complete=1, incomplete=1",
                stdout.getvalue(),
            )


    def test_run_status_summary_preserves_token_usage_source_and_unavailable_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            reported_run_dir = work_root / "runs" / "reported-run"
            reported_state_dir = reported_run_dir / "state"
            reported_state_dir.mkdir(parents=True)
            _write_json(
                reported_state_dir / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "backlog": {"items": []},
                    "inflight_attempts": [],
                    "steps": [
                        {
                            "task_id": "TASK-001",
                            "result": {
                                "token_usage": {
                                    "usage_source": "codex_jsonl",
                                    "input_tokens": 1200,
                                    "output_tokens": 300,
                                    "total_tokens": 1500,
                                },
                            },
                        }
                    ],
                },
            )
            missing_run_dir = work_root / "runs" / "missing-run"
            missing_state_dir = missing_run_dir / "state"
            missing_state_dir.mkdir(parents=True)
            _write_json(
                missing_state_dir / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "backlog": {"items": []},
                    "inflight_attempts": [],
                    "steps": [{"task_id": "TASK-002", "result": {}}],
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            reported = _build_run_status_summary(profile, reported_run_dir)
            missing = _build_run_status_summary(profile, missing_run_dir)

            self.assertEqual(reported["token_usage"]["usage_status"], "reported")
            self.assertEqual(reported["token_usage"]["usage_source"], "codex_jsonl")
            self.assertEqual(reported["token_usage"]["usage_sources"], ["codex_jsonl"])
            self.assertEqual(
                missing["token_usage"]["unavailable_reason"],
                "missing_runtime_token_usage",
            )


    def test_execution_result_text_reports_followup_work_summary(self):
        result = {
            "status": "completed",
            "taskpack_id": "follow-up-run",
            "follow_up": {
                "source_taskpack_id": "previous-run",
                "source_report_path": "/tmp/previous-report.md",
            },
            "report": {
                "report_path": "/tmp/follow-up-report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["Optimized the gesture scoring pipeline."],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["local benchmark: passed"],
                    "integration": "passed",
                    "integration_recommendation": "Run `agentteam integrate --taskpack follow-up-run`.",
                    "next_steps": ["Run the full competition validation package."],
                    "evidence_gaps": [],
                },
            },
            "paths": {"run_dir": "/tmp/follow-up-run"},
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            _write_execution_result_text(result)

        output = stdout.getvalue()
        self.assertIn("source_taskpack_id: previous-run", output)
        self.assertIn("work_report: changed=Optimized the gesture scoring pipeline.", output)
        self.assertIn("files=gesture_recognition/sim_eval.py", output)
        self.assertIn("verification=local benchmark: passed", output)
        self.assertIn("integration=passed", output)
        self.assertIn("recommendation: merge=Run `agentteam integrate --taskpack follow-up-run`.", output)
        self.assertIn("next=Run the full competition validation package.", output)


    def test_execution_result_text_reports_repo_map_handoff_reuse(self):
        result = {
            "status": "completed",
            "taskpack_id": "follow-up-run",
            "follow_up": {"source_taskpack_id": "previous-run"},
            "repo_map_handoff_reuse": {
                "status": "applied",
                "handoff_path": taskpack_module.REPO_MAP_HANDOFF_PATH,
                "removed_task_count": 1,
            },
            "report": {
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {},
            },
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            _write_execution_result_text(result)

        output = stdout.getvalue()
        self.assertIn(
            "repo_map_handoff_reuse: status=applied; path=.agentteam/generated/repo_map_handoff.json",
            output,
        )
        self.assertIn("removed_repo_map_tasks=1", output)


    def test_execution_result_text_aggregates_multiple_task_summary_fields(self):
        result = {
            "status": "completed",
            "taskpack_id": "multi-task-run",
            "report": {
                "report_path": "/tmp/multi-task-report.md",
                "run_status": "completed",
                "task_count": 2,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["实现任务 A 的代码路径。", "补充任务 B 的验证入口。"],
                    "changed_files": ["src/a.py", "src/b.py"],
                    "verification": ["unit-a: passed", "unit-b: passed"],
                    "integration": "passed",
                    "integration_recommendation": "Run `agentteam integrate --taskpack multi-task-run`.",
                    "next_steps": ["继续验证 A。", "继续验证 B。"],
                    "evidence_gaps": [],
                },
            },
            "paths": {"run_dir": "/tmp/multi-task-run"},
        }
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            _write_execution_result_text(result)

        output = stdout.getvalue()
        self.assertIn(
            "work_report: changed=实现任务 A 的代码路径。；补充任务 B 的验证入口。",
            output,
        )
        self.assertIn("files=src/a.py；src/b.py", output)
        self.assertIn("verification=unit-a: passed；unit-b: passed", output)
        self.assertIn("next=继续验证 A。；继续验证 B。", output)


    def test_agentteam_cli_report_json_includes_fresh_projection_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "report-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "report-db-project")
            _write_completed_operator_run(run_dir)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "report-db-run"})
            rebuild_project_projection_db(work_root)

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
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
            self.assertEqual(
                summary["projection_run"]["report_path"],
                str((run_dir / "reports" / "final_report.json").resolve()),
            )


    def test_agentteam_cli_report_json_falls_back_when_projection_db_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "report-db-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "report-db-project")
            _write_completed_operator_run(run_dir)
            _write_json(run_dir / "reports" / "final_report.json", {"run_id": "report-db-run"})
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
                    "report",
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
            self.assertProjectionFallbackMetadata(summary, "stale")
            self.assertEqual(summary["projection_check_status"], "failed")
            self.assertNotIn("check_status", summary)
            self.assertIsNone(summary["projection_run"])

            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
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
            self.assertIn("projection_source: files", text_completed.stdout)
            self.assertIn("projection_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("next_action: run agentteam db rebuild", text_completed.stdout)


    def test_agentteam_cli_report_renders_operator_summary_and_writes_report_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_completed_operator_run(Path(tmp) / "runs" / "taskpack-7")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--run-dir",
                    str(run_dir),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("AgentTeam Run Report", completed.stdout)
            self.assertIn("Run: taskpack-7", completed.stdout)
            self.assertIn("Status: completed", completed.stdout)
            self.assertIn("Token usage: total=1500 input=1200 output=300 reported=1/1", completed.stdout)
            self.assertIn("## Operator Summary", completed.stdout)
            self.assertIn("What changed: Scanned the repository", completed.stdout)
            self.assertIn("Integration: passed", completed.stdout)
            self.assertIn("中文简报:", completed.stdout)
            self.assertIn("本次运行已完成，共 1 个任务，0 个阻塞。", completed.stdout)
            self.assertIn("Integration recommendation: Review the final report, then run `agentteam integrate --taskpack taskpack-7` from a clean target repository if these changes should land.", completed.stdout)
            self.assertIn("Review gate:", completed.stdout)
            self.assertIn("report_command: agentteam report --taskpack taskpack-7", completed.stdout)
            self.assertIn("paths_command: agentteam paths --taskpack taskpack-7", completed.stdout)
            self.assertIn("diff_command: git -C ", completed.stdout)
            self.assertIn("integrate_command: agentteam integrate --taskpack taskpack-7", completed.stdout)
            self.assertIn("Scanned the repository", completed.stdout)
            self.assertIn("gesture_recognition/sim_eval.py", completed.stdout)
            report_path = run_dir / "reports" / "final_report.md"
            self.assertTrue(report_path.exists())
            self.assertIn("Run: taskpack-7", report_path.read_text(encoding="utf-8"))
            self.assertIn("Tokens: total=1500 input=1200 output=300", report_path.read_text(encoding="utf-8"))
            report_json = json.loads((run_dir / "reports" / "final_report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                report_json["completion_summary"]["what_changed"],
                ["Scanned the repository and implemented one evidence-backed optimization."],
            )
            self.assertEqual(report_json["completion_summary"]["integration"], "passed")
            self.assertEqual(
                report_json["completion_summary"]["review_gate"]["status"],
                "review_gate_required",
            )
            self.assertEqual(
                report_json["completion_summary"]["review_gate"]["integrate_command"],
                "agentteam integrate --taskpack taskpack-7",
            )
            self.assertEqual(
                report_json["completion_summary"]["next_steps"],
                ["Run the full competition validation package."],
            )
            self.assertEqual(
                report_json["completion_summary"]["chinese_operator_brief"][0],
                "本次运行已完成，共 1 个任务，0 个阻塞。",
            )
            artifacts_root = Path(tmp) / "artifacts"
            self.assertTrue((artifacts_root / ".git").exists())
            self.assertTrue((artifacts_root / "runs" / "taskpack-7" / "reports" / "final_report.md").exists())
            tracked_completed = subprocess.run(
                ["git", "-C", str(artifacts_root), "ls-files"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(tracked_completed.returncode, 0, tracked_completed.stderr)
            self.assertIn(
                "runs/taskpack-7/reports/final_report.md",
                tracked_completed.stdout.splitlines(),
            )


    def test_legacy_only_report_remains_readable_and_is_not_benchmark_counted(self):
        from agentteam_runtime.operator_report import (
            build_run_completion_report,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_completed_operator_run(
                Path(tmp) / "runs" / "legacy-only"
            )
            report = build_run_completion_report(
                run_dir,
                project="legacy-project",
                write_files=False,
            )
            markdown = render_run_completion_report(report)

        self.assertIsNone(report["model_invocation_usage"])
        self.assertEqual(report["token_usage"]["total_tokens"], 1500)
        self.assertEqual(
            report["legacy_task_token_usage"],
            {
                "accounting_scope": "legacy_task_results",
                "benchmark_counted": False,
                "usage": report["token_usage"],
            },
        )
        self.assertIn(
            "Token usage: total=1500 input=1200 output=300 reported=1/1 "
            "(legacy task-result aggregate; not benchmark-counted)",
            markdown,
        )


    def test_concise_report_lines_include_chinese_operator_brief(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["Optimized the gesture scoring pipeline."],
                    "changed_files": ["gesture_recognition/sim_eval.py"],
                    "verification": ["unit_tests: passed"],
                    "integration": "passed",
                    "chinese_operator_brief": [
                        "本次运行已完成，共 1 个任务，0 个阻塞。",
                        "主要变更：Optimized the gesture scoring pipeline.",
                    ],
                },
                "operator_report": {"task_reports": []},
            }
        )

        self.assertIn("中文简报: 本次运行已完成，共 1 个任务，0 个阻塞。", lines)
        self.assertIn("中文简报: 主要变更：Optimized the gesture scoring pipeline.", lines)


    def test_concise_report_lines_include_chinese_operator_digest(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "operator_digest": [
                        "做了什么：优化了手势评分流水线的数据窗口复制。",
                        "验证结果：unit_tests: passed",
                    ],
                },
                "operator_report": {"task_reports": []},
            }
        )

        self.assertIn("中文工作汇报: 做了什么：优化了手势评分流水线的数据窗口复制。", lines)
        self.assertIn("中文工作汇报: 验证结果：unit_tests: passed", lines)


    def test_concise_report_lines_include_agentteam_target_review_gate(self):
        lines = concise_report_lines(
            {
                "report_path": "/tmp/final_report.md",
                "run_status": "completed",
                "task_count": 1,
                "blocked_count": 0,
                "completion_summary": {
                    "what_changed": ["已实现 AgentTeam 目标仓库策略。"],
                    "integration": "passed",
                },
                "operator_report": {
                    "task_reports": [
                        {
                            "task_id": "TASK-AGENTTEAM-001",
                            "status": "implementation completed",
                            "agentteam_target_review_required": True,
                        }
                    ]
                },
            }
        )

        self.assertIn(
            "agentteam_target_review: source merge, push, and release activation require operator review",
            lines,
        )


    def test_agentteam_cli_doctor_reports_profile_and_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "doctor-project")

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "doctor",
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
            self.assertEqual(summary["doctor_status"], "passed")
            check_names = [check["name"] for check in summary["checks"]]
            self.assertIn("profile", check_names)
            self.assertIn("git_repository", check_names)
            self.assertIn("verification_profile", check_names)
            self.assertIn("inotify_capacity", check_names)


    def test_agentteam_cli_gc_artifacts_json_reports_unavailable_without_projection_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "gc-artifact-project")
            run_dir = _write_completed_operator_run(work_root / "runs" / "retention-run")
            _write_json(
                run_dir / "repo_contexts" / "optimize-pipeline-ATTEMPT-001-implementation.json",
                {"repo_context_schema_version": "repo_context.v1"},
            )

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
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
            self.assertProjectionFallbackMetadata(summary["artifact_projection"], "missing")
            plan = summary["artifact_retention_plan"]
            self.assertProjectionFallbackMetadata(plan, "missing")
            self.assertEqual(plan["plan_status"], "unavailable")
            self.assertFalse(plan["deletion_enabled"])
            self.assertEqual(plan["candidate_count"], 0)

            text_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "gc",
                    "--project-root",
                    str(repo),
                    "--artifacts",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("artifact_projection: files", text_completed.stdout)
            self.assertIn("artifact_projection_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("artifact_projection_next_action: run agentteam db rebuild", text_completed.stdout)
            self.assertIn("artifact_retention_plan_warning: projection_db_unavailable", text_completed.stdout)
            self.assertIn("artifact_retention_plan_next_action: run agentteam db rebuild", text_completed.stdout)


    def test_agentteam_cli_paths_reports_run_artifact_and_baseline_locations(self):
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
                    "paths-project",
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
                    "Create a run for paths.",
                    "--taskpack-id",
                    "paths-run",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(start_completed.returncode, 0, start_completed.stderr)

            json_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "paths",
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
                    "paths",
                    "--project-root",
                    str(repo),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            cwd_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "paths",
                    "--json",
                ],
                cwd=repo,
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(json_completed.returncode, 0, json_completed.stderr)
            summary = json.loads(json_completed.stdout)
            run_dir = work_root / "runs" / "paths-run"
            baseline_worktree = run_dir / "integration-baseline"
            self.assertEqual(summary["project"], "paths-project")
            self.assertEqual(summary["project_root"], str(repo.resolve()))
            self.assertEqual(summary["work_root"], str(work_root.resolve()))
            self.assertEqual(summary["latest_run"], "paths-run")
            self.assertEqual(summary["run_dir"], str(run_dir.resolve()))
            self.assertEqual(summary["artifacts_root"], str((work_root / "artifacts").resolve()))
            self.assertEqual(summary["final_report"], str((run_dir / "reports" / "final_report.md").resolve()))
            self.assertEqual(
                summary["integration_baseline"]["branch"],
                "agentteam/run/paths-run/integration",
            )
            self.assertEqual(
                summary["integration_baseline"]["worktree_path"],
                str(baseline_worktree.resolve()),
            )
            self.assertTrue(summary["integration_baseline"]["worktree_exists"])
            self.assertEqual(text_completed.returncode, 0, text_completed.stderr)
            self.assertIn("project: paths-project\n", text_completed.stdout)
            self.assertIn("latest_run: paths-run\n", text_completed.stdout)
            self.assertIn("integration_baseline_branch: agentteam/run/paths-run/integration\n", text_completed.stdout)
            self.assertIn(str(baseline_worktree.resolve()), text_completed.stdout)
            diff_base = (
                summary["integration_baseline"].get("base_sha")
                or summary["integration_baseline"]["head_sha"]
            )
            diff_head = (
                summary["integration_baseline"]["head_sha"]
                if summary["integration_baseline"].get("base_sha")
                else "HEAD"
            )
            expected_review_diff = (
                f"git -C {baseline_worktree.resolve()} diff --stat {diff_base}..{diff_head}"
            )
            self.assertEqual(
                summary["review_commands"],
                {
                    "report": "agentteam report --taskpack paths-run",
                    "paths": "agentteam paths --taskpack paths-run",
                    "diff": expected_review_diff,
                    "integrate": "agentteam integrate --taskpack paths-run",
                },
            )
            self.assertEqual(
                summary["read_only_review_commands"],
                {
                    "report": "agentteam report --taskpack paths-run",
                    "paths": "agentteam paths --taskpack paths-run",
                    "diff": expected_review_diff,
                },
            )
            self.assertEqual(
                summary["accept_command"],
                "agentteam integrate --taskpack paths-run",
            )
            self.assertIn("review_report: agentteam report --taskpack paths-run\n", text_completed.stdout)
            self.assertIn(
                f"review_diff: {expected_review_diff}\n",
                text_completed.stdout,
            )
            self.assertIn("review_integrate: agentteam integrate --taskpack paths-run\n", text_completed.stdout)
            self.assertEqual(cwd_completed.returncode, 0, cwd_completed.stderr)
            cwd_summary = json.loads(cwd_completed.stdout)
            self.assertEqual(cwd_summary["project_root"], str(repo.resolve()))


    def test_agentteam_cli_integrate_rebase_conflict_returns_blocked_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "conflict-integrate-project")
            _start_fake_agentteam_run_for_test(
                repo,
                "Create a run for conflict integration.",
                "conflict-integrate-run",
            )
            baseline_worktree = work_root / "runs" / "conflict-integrate-run" / "integration-baseline"
            (baseline_worktree / "README.md").write_text("# fixture\n\nbaseline change\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=baseline_worktree, check=True)
            subprocess.run(
                ["git", "commit", "-m", "agentteam conflicting baseline"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            (repo / "README.md").write_text("# fixture\n\nmain change\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "main conflicting change"],
                cwd=repo,
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

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "integrate",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "conflict-integrate-run",
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
            self.assertEqual(summary["integrate_status"], "blocked")
            self.assertEqual(summary["rebase_status"], "conflict")
            self.assertEqual(summary["merge_status"], "not_merged")
            self.assertEqual(summary["conflicted_files"], ["README.md"])
            after_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(after_head, before_head)
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# fixture\n\nmain change\n")
            baseline_status = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=baseline_worktree,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(baseline_status, "")


    def test_agentteam_cli_status_and_report_show_integration_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "baseline-visible"
            baseline_worktree = run_dir / "integration-baseline"
            _init_repo(repo)
            baseline_worktree.mkdir(parents=True)
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "baseline-visible-project",
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
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": "agentteam/run/baseline-visible/integration",
                        "integration_baseline_worktree_path": str(baseline_worktree.resolve()),
                        "integration_baseline_head_sha": "abc123",
                    },
                },
            )

            status_json = subprocess.run(
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
            status_text = subprocess.run(
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
            report_json = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
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
            report_text = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
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

            self.assertEqual(status_json.returncode, 0, status_json.stderr)
            status_summary = json.loads(status_json.stdout)
            self.assertEqual(
                status_summary["integration_baseline"]["branch"],
                "agentteam/run/baseline-visible/integration",
            )
            self.assertEqual(status_summary["integration_baseline"]["head_sha"], "abc123")
            self.assertEqual(status_text.returncode, 0, status_text.stderr)
            self.assertIn(
                "integration_baseline_branch: agentteam/run/baseline-visible/integration\n",
                status_text.stdout,
            )
            self.assertEqual(report_json.returncode, 0, report_json.stderr)
            report_summary = json.loads(report_json.stdout)
            self.assertEqual(
                report_summary["integration_baseline"]["branch"],
                "agentteam/run/baseline-visible/integration",
            )
            self.assertEqual(report_text.returncode, 0, report_text.stderr)
            self.assertIn("Integration baseline: agentteam/run/baseline-visible/integration", report_text.stdout)
            self.assertIn("Baseline head: abc123", report_text.stdout)


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
                        "steps": [
                            {
                                "task_id": "completed-task",
                                "result": {
                                    "task_id": "completed-task",
                                    "attempt_id": "ATTEMPT-000",
                                    "runtime_output": {
                                        "usage": {
                                            "input_tokens": 700,
                                            "output_tokens": 200,
                                            "total_tokens": 900,
                                        }
                                    },
                                },
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
            self.assertIn("overall_status: max_ticks_reached", status_completed.stdout)
            self.assertIn("run_status: max_ticks_reached", status_completed.stdout)
            self.assertIn("tokens: total=900 input=700 output=200 reported=1/1", status_completed.stdout)
            self.assertIn("inflight: 1", status_completed.stdout)
            self.assertIn("workers: 1 stopped, 0 running, 0 quarantined", status_completed.stdout)
            self.assertIn(
                "last_worker: implementation-worker-1 stopped exit_code=-15 stopped_by=terminated",
                status_completed.stdout,
            )


    def test_agentteam_cli_status_reports_active_authoring_over_idle_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "previous-run"
            author_dir = work_root / "drafts" / ".follow-up-author"
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
            _write_completed_operator_run(run_dir)
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "follow-up",
                    "pid": os.getpid(),
                    "started_at": "2026-06-11T00:00:00Z",
                    "updated_at": "2026-06-11T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
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
            self.assertIn("latest_run: previous-run", status_completed.stdout)
            self.assertIn("overall_status: authoring", status_completed.stdout)
            self.assertIn("run_status: idle", status_completed.stdout)
            self.assertIn("active_phase: authoring", status_completed.stdout)
            self.assertIn("active_authoring: follow-up", status_completed.stdout)


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
            events = _read_jsonl(run_dir / "events.jsonl")
            stale_events = [
                event for event in events
                if event.get("event_type") == "run_stale_detected"
            ]
            self.assertEqual(len(stale_events), 1)
            self.assertEqual(stale_events[0]["payload"]["liveness_status"], "running-stale")
            self.assertEqual(stale_events[0]["payload"]["run_id"], "stale-run")


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


    def test_agentteam_cli_status_and_report_surface_latest_pursue_recap(self):
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
                    "pursue-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--verification-command-json",
                    json.dumps(["python3", "-c", "print('ok')"]),
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(init_completed.returncode, 0, init_completed.stderr)
            pursue_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "pursue",
                    "--project-root",
                    str(repo),
                    "--goal",
                    "持续优化这个仓库的准确率和延迟。",
                    "--taskpack-id",
                    "pursue-loop",
                    "--max-rounds",
                    "1",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(pursue_completed.returncode, 0, pursue_completed.stderr)

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
            self.assertIn("pursue: pursue-loop stopped because review_gate_required", status_completed.stdout)
            self.assertIn("pursue_rounds: 1/1", status_completed.stdout)
            self.assertIn("pursue_latest_taskpack: pursue-loop", status_completed.stdout)
            self.assertIn("pursue_next_action: agentteam report --taskpack pursue-loop", status_completed.stdout)

            report_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "report",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "pursue-loop",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(report_completed.returncode, 0, report_completed.stderr)
            self.assertIn("## Pursue Recap", report_completed.stdout)
            self.assertIn("- Stop reason: review_gate_required", report_completed.stdout)
            self.assertIn("- Latest taskpack: pursue-loop", report_completed.stdout)
            self.assertIn("- Next action: agentteam report --taskpack pursue-loop", report_completed.stdout)


    def test_pursue_next_goal_uses_report_next_step(self):
        self.assertEqual(
            _pursue_next_goal(
                "持续优化原始目标。",
                {"completion_summary": {"next_steps": ["继续验证最慢模块。"]}},
            ),
            "继续验证最慢模块。",
        )
        self.assertIn(
            "持续优化原始目标。",
            _pursue_next_goal("持续优化原始目标。", {"completion_summary": {}}),
        )


    def test_pursue_follow_up_queue_summary_selects_next_goal(self):
        from agentteam_runtime.agentteam import _pursue_follow_up_queue_summary

        summary = _pursue_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续验证最慢模块。"],
                    "follow_up_recommendation": {
                        "action": "next",
                        "next_command": 'agentteam next --from-taskpack first-pass --goal "补充准确率基准。"',
                    },
                },
            },
            goal_memory={
                "memory_path": "/tmp/work/pursue/first-pass-goal-memory.json",
                "follow_up_queue": [
                    {
                        "objective": "检查端到端延迟。",
                        "source_taskpack_id": "first-pass",
                        "source_report_path": "/tmp/first-pass/reports/final_report.md",
                    }
                ],
            },
            source_run_dir=Path("/tmp/work/runs/first-pass"),
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertEqual(summary["source_taskpack_id"], "first-pass")
        self.assertEqual(summary["next_goal"], "继续验证最慢模块。")
        self.assertIn("agentteam next --from-taskpack first-pass", summary["next_command"])
        self.assertEqual(
            [item["objective"] for item in summary["items"]],
            ["继续验证最慢模块。", "补充准确率基准。", "检查端到端延迟。"],
        )


    def test_follow_up_queue_summary_merges_report_and_goal_memory(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续验证最慢模块。", "补充准确率基准。"],
                    "follow_up_recommendation": {
                        "action": "next",
                        "next_command": 'agentteam next --from-taskpack first-pass --goal "继续验证最慢模块。"',
                    },
                },
            },
            goal_memory={
                "follow_up_queue": [
                    {
                        "objective": "补充准确率基准。",
                        "source_taskpack_id": "first-pass",
                        "source_report_path": "/tmp/first-pass/reports/final_report.md",
                    },
                    {
                        "objective": "检查端到端延迟。",
                        "source_taskpack_id": "first-pass",
                        "source_report_path": "/tmp/first-pass/reports/final_report.md",
                    },
                ]
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertEqual(summary["source_taskpack_id"], "first-pass")
        self.assertEqual(
            [item["objective"] for item in summary["items"]],
            ["继续验证最慢模块。", "补充准确率基准。", "检查端到端延迟。"],
        )
        self.assertEqual(summary["next_goal"], "继续验证最慢模块。")
        self.assertEqual(
            summary["next_command"],
            'agentteam next --from-taskpack first-pass --goal "继续验证最慢模块。"',
        )


    def test_follow_up_queue_summary_skips_generic_next_step_for_concrete_recommendation(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续优化"],
                    "changed_files": ["agentteam_runtime/follow_up_queue.py"],
                    "verification": ["python3 -m unittest test_taskpack.FollowUpQueueSpecificity passed"],
                    "measured_results": ["generic next_steps no longer selected verbatim"],
                    "follow_up_recommendation": {
                        "action": "next",
                        "next_command": (
                            'agentteam next --from-taskpack first-pass --goal '
                            '"补充 follow_up_queue next_goal 具体化回归测试。"'
                        ),
                    },
                },
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertEqual(
            [item["objective"] for item in summary["items"]],
            ["补充 follow_up_queue next_goal 具体化回归测试。"],
        )
        self.assertEqual(summary["next_goal"], "补充 follow_up_queue next_goal 具体化回归测试。")
        self.assertEqual(
            summary["next_command"],
            'agentteam next --from-taskpack first-pass --goal "补充 follow_up_queue next_goal 具体化回归测试。"',
        )


    def test_compact_pursue_queue_summary_preserves_missing_selected_item(self):
        compact = agentteam_module._compact_pursue_queue_summary(
            {
                "queue_status": "no_auto_dispatchable_items",
                "source_taskpack_id": "pursue-loop-r2",
                "item_count": 1,
                "next_goal": None,
                "next_command": None,
                "selected_item": None,
                "items": [
                    {
                        "objective": "由 operator 审阅并决定是否集成本测试变更。",
                        "source": "report.next_steps",
                    }
                ],
            }
        )

        self.assertEqual(compact["queue_status"], "no_auto_dispatchable_items")
        self.assertIsNone(compact["next_goal"])
        self.assertNotIn("selected_item", compact)


    def test_follow_up_queue_summary_enriches_generic_next_step_with_report_evidence(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "first-pass",
                "report_path": "/tmp/first-pass/reports/final_report.md",
                "completion_summary": {
                    "next_steps": ["继续优化"],
                    "changed_files": ["agentteam_runtime/follow_up_queue.py"],
                    "verification": ["python3 -m unittest test_taskpack.FollowUpQueueSpecificity passed"],
                    "measured_results": ["generic next_steps no longer selected verbatim"],
                },
            },
            source_taskpack_id="first-pass",
            source_run_dir="/tmp/first-pass",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "ready")
        self.assertNotEqual(summary["next_goal"], "继续优化")
        self.assertIn("继续优化", summary["next_goal"])
        self.assertIn("基于上一轮证据", summary["next_goal"])
        self.assertIn("changed_files=agentteam_runtime/follow_up_queue.py", summary["next_goal"])
        self.assertIn(
            "verification=python3 -m unittest test_taskpack.FollowUpQueueSpecificity passed",
            summary["next_goal"],
        )
        self.assertIn(
            "measured_result=generic next_steps no longer selected verbatim",
            summary["next_goal"],
        )
        self.assertIn(summary["next_goal"], summary["next_command"])


    def test_operator_report_loads_pursue_goal_memory_round_recap(self):
        from agentteam_runtime.operator_report import (
            find_pursue_recap_for_run,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            run_dir = work_root / "runs" / "pursue-loop"
            run_dir.mkdir(parents=True)
            report_path = str(run_dir / "reports" / "final_report.md")
            memory_path = work_root / "pursue" / "pursue-loop-goal-memory.json"
            _write_json(
                memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(memory_path),
                    "latest_round_recap": {
                        "taskpack_id": "pursue-loop",
                        "result_status": "completed",
                        "run_outcome": "completed_with_review_required",
                        "stop_reason": "review_gate_required",
                        "recommended_next_step": "继续实现 queue recap 展示。",
                        "evidence_paths": [{"type": "report", "path": report_path}],
                        "blockers": ["需要 operator review。"],
                        "token_usage": {
                            "usage_status": "unavailable",
                            "reported_attempt_count": 0,
                            "unreported_attempt_count": 1,
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                        },
                    },
                },
            )
            _write_json(
                work_root / "pursue" / "pursue-loop.json",
                {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 1,
                    "max_rounds": 2,
                    "stop_reason": "review_gate_required",
                    "latest_taskpack_id": "pursue-loop",
                    "latest_report_path": report_path,
                    "goal_memory_path": str(memory_path),
                    "operator_next_action": "agentteam report --taskpack pursue-loop",
                    "runs": [{"taskpack_id": "pursue-loop"}],
                },
            )

            recap = find_pursue_recap_for_run(run_dir)
            markdown = render_run_completion_report(
                {
                    "project": "agentteam",
                    "run_id": "pursue-loop",
                    "run_dir": str(run_dir),
                    "run_status": "completed",
                    "run_outcome": "completed_with_review_required",
                    "scheduler_status": "idle",
                    "task_count": 0,
                    "blocked_count": 0,
                    "token_usage": {},
                    "pursue_recap": recap,
                    "integration_baseline": {},
                    "completion_summary": {},
                    "operator_report": {},
                }
            )

        self.assertEqual(recap["latest_round_recap"]["recommended_next_step"], "继续实现 queue recap 展示。")
        self.assertEqual(recap["latest_round_recap"]["token_usage"]["usage_status"], "unavailable")
        self.assertIn("- Latest result: completed_with_review_required", markdown)
        self.assertIn(f"- Evidence: {report_path}", markdown)
        self.assertIn("- Token usage: unavailable", markdown)
        self.assertIn("- Recommended next step: 继续实现 queue recap 展示。", markdown)


    def test_operator_report_renders_chinese_digest_with_stop_reason_and_tokens(self):
        from agentteam_runtime.operator_report import render_run_completion_report

        markdown = render_run_completion_report(
            {
                "project": "agentteam",
                "run_id": "pursue-loop",
                "run_dir": "/tmp/agentteam-work/runs/pursue-loop",
                "run_status": "completed",
                "run_outcome": "completed_with_review_required",
                "scheduler_status": "idle",
                "task_count": 1,
                "blocked_count": 0,
                "token_usage": {
                    "usage_status": "unavailable",
                    "reported_attempt_count": 0,
                    "unreported_attempt_count": 1,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                },
                "pursue_recap": {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 2,
                    "max_rounds": 4,
                    "stop_reason": "review_gate_required",
                    "operator_next_action": "agentteam report --taskpack pursue-loop",
                    "latest_round_recap": {
                        "recommended_next_step": "继续执行 bounded dogfood 验证。",
                    },
                },
                "integration_baseline": {},
                "completion_summary": {
                    "operator_digest": [
                        "为什么：为了让 operator 在多轮结束后看清本轮取舍。",
                        "做了什么：补充中文汇报字段映射。",
                        "涉及文件：agentteam_runtime/operator_report.py",
                        "验证结果：test_taskpack: passed",
                        "风险：真实 Feishu webhook 仍需 operator 环境验证。",
                        "下一步：运行 2 轮 AgentTeam-as-target dogfood。",
                    ],
                },
                "operator_report": {"task_reports": []},
            }
        )

        self.assertIn("## 中文工作汇报", markdown)
        self.assertIn("- 当前轮次：2/4", markdown)
        self.assertIn("- 停止原因：review_gate_required", markdown)
        self.assertIn("- 下一步：继续执行 bounded dogfood 验证。", markdown)
        self.assertIn("- 为什么：为了让 operator 在多轮结束后看清本轮取舍。", markdown)
        self.assertIn("- 涉及文件：agentteam_runtime/operator_report.py", markdown)
        self.assertIn("- 验证结果：test_taskpack: passed", markdown)
        self.assertIn("- 风险：真实 Feishu webhook 仍需 operator 环境验证。", markdown)
        self.assertIn("- Token usage: unavailable", markdown)


    def test_agentteam_cli_queue_show_reports_followup_items(self):
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
                    "show",
                    "--project-root",
                    str(repo),
                    "--taskpack",
                    "first-pass",
                    "--json",
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(queue_completed.returncode, 0, queue_completed.stderr)
            summary = json.loads(queue_completed.stdout)
            self.assertEqual(summary["queue_status"], "ready")
            self.assertEqual(summary["source_taskpack_id"], "first-pass")
            self.assertTrue(summary["items"])
            self.assertEqual(summary["next_goal"], summary["items"][0]["objective"])
            self.assertIn("agentteam next --from-taskpack first-pass", summary["next_command"])


    def test_agentteam_cli_feedback_list_reports_pending_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "feedback-project")
            _write_completed_operator_run(work_root / "runs" / "first-pass")
            propose_completed = subprocess.run(
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
            self.assertEqual(propose_completed.returncode, 0, propose_completed.stderr)

            list_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "feedback",
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
            self.assertEqual(summary["proposal_count"], 1)
            self.assertEqual(summary["proposals"][0]["proposal_id"], "design-gap-1")
            self.assertEqual(summary["proposals"][0]["proposal_status"], "pending_review")


    def test_agentteam_cli_grounding_reports_repo_summary_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "grounding-project")
            (repo / "pkg").mkdir()
            (repo / "tests").mkdir()
            (repo / "pkg" / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
            (repo / "tests" / "test_module.py").write_text("from pkg.module import run\n", encoding="utf-8")
            (repo / "pyproject.toml").write_text("[project]\nname = 'grounding'\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "pkg/module.py", "tests/test_module.py", "pyproject.toml"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "add grounding files"],
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
                    "grounding",
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
            self.assertEqual(summary["grounding_schema_version"], "repo_grounding.v1")
            self.assertEqual(summary["project"], "grounding-project")
            self.assertEqual(summary["scan_status"], "ok")
            self.assertEqual(summary["languages"][0]["language"], "python")
            self.assertEqual(summary["project_tools"][0]["tool_id"], "python-pyproject")
            self.assertEqual(
                summary["candidate_verification_commands"][0]["command"],
                ["python3", "-m", "unittest", "discover"],
            )


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


    def test_project_authoring_summary_reports_running_author(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            author_dir = work_root / "drafts" / ".running-author"
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "running",
                    "pid": os.getpid(),
                    "started_at": "2026-06-10T00:00:00Z",
                    "updated_at": "2026-06-10T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            summary = _build_project_authoring_summary(profile)

            self.assertEqual(summary["active_count"], 1)
            self.assertEqual(summary["latest"]["taskpack_id"], "running")
            self.assertEqual(summary["latest"]["liveness_status"], "running-alive")


    def test_run_status_summary_reports_overall_authoring_when_followup_author_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            run_dir = work_root / "runs" / "previous-run"
            author_dir = work_root / "drafts" / ".follow-up-author"
            _write_completed_operator_run(run_dir)
            author_dir.mkdir(parents=True)
            _write_json(
                author_dir / "author_state.json",
                {
                    "author_status": "running",
                    "taskpack_id": "follow-up",
                    "pid": os.getpid(),
                    "started_at": "2026-06-11T00:00:00Z",
                    "updated_at": "2026-06-11T00:00:01Z",
                    "elapsed_seconds": 1.0,
                },
            )
            profile = {"project_key": "fixture", "work_root": str(work_root)}

            summary = _build_run_status_summary(profile, run_dir)

            self.assertEqual(summary["status"], "idle")
            self.assertEqual(summary["run_status"], "idle")
            self.assertEqual(summary["overall_status"], "authoring")
            self.assertEqual(summary["active_phase"], "authoring")
            self.assertEqual(summary["active_authoring"]["taskpack_id"], "follow-up")


    def test_handle_run_prints_compact_summary_and_normalizes_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            frozen_root = tmp_path / "frozen"
            runs_root = tmp_path / "runs"
            _init_repo(repo)
            result = draft_taskpack_files(
                project_root=repo,
                goal="Implement fixture output.",
                draft_root=drafts,
                taskpack_id="compact-run",
                write_scope=["generated/"],
                verification_command=["python3", "-c", "pass"],
            )
            _set_taskpack_runtime_backend(Path(result["taskpack_dir"]), "fake")
            frozen = freeze_taskpack(result["taskpack_dir"], frozen_root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                code = _handle_run(
                    SimpleNamespace(
                        frozen_taskpack_dir=frozen["frozen_taskpack_dir"],
                        run_root=str(runs_root / "compact-run"),
                        one_shot=False,
                        max_inflight=1,
                        max_attempts=1,
                        commit_verified_integration=False,
                        notification_project="fixture",
                        feishu_webhook_env=None,
                        feishu_signing_secret_env=None,
                        json=False,
                    )
                )

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("status: completed", output)
            self.assertIn("taskpack_id: compact-run", output)
            self.assertIn("report:", output)
            self.assertIn(f"run_dir: {runs_root / 'compact-run'}", output)
            self.assertNotIn('"snapshot"', output)
            self.assertTrue((runs_root / "compact-run" / "events.jsonl").exists())
            self.assertFalse((runs_root / "compact-run" / "compact-run").exists())

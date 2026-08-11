try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class PursueMixin:
    def test_agentteam_cli_pursue_runs_one_fake_round_and_stops_on_review_gate(self):
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

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["pursue_status"], "stopped")
            self.assertEqual(summary["stop_reason"], "review_gate_required")
            self.assertEqual(summary["rounds_completed"], 1)
            self.assertEqual(summary["runs"][0]["round"], 1)
            self.assertEqual(summary["runs"][0]["taskpack_id"], "pursue-loop")
            self.assertEqual(summary["runs"][0]["status"], "completed")
            self.assertEqual(
                summary["runs"][0]["follow_up_recommendation"]["action"],
                "integrate",
            )
            self.assertTrue((work_root / "runs" / "pursue-loop").exists())
            recap_path = Path(summary["pursue_recap_path"])
            self.assertTrue(recap_path.exists())
            self.assertIn(str(work_root.resolve()), str(recap_path))
            recap = json.loads(recap_path.read_text(encoding="utf-8"))
            self.assertEqual(recap["pursue_id"], "pursue-loop")
            self.assertEqual(recap["rounds_completed"], 1)
            self.assertEqual(recap["max_rounds"], 1)
            self.assertEqual(recap["stop_reason"], "review_gate_required")
            self.assertEqual(recap["latest_taskpack_id"], "pursue-loop")
            self.assertEqual(
                recap["latest_report_path"],
                summary["runs"][0]["report_path"],
            )
            self.assertIn(
                "agentteam report --taskpack pursue-loop",
                recap["operator_next_action"],
            )


    def test_pursue_status_text_surfaces_all_operator_stop_reasons(self):
        base_summary = {
            "project": "pursue-project",
            "latest_run": "pursue-loop",
            "status": "completed",
            "overall_status": "completed",
            "run_status": "completed",
            "liveness_status": "stopped",
            "tasks": {"done": 1, "blocked": 0},
            "integration": {"blocked": 0},
            "evidence": {},
            "integration_baseline": {},
            "token_usage": {},
            "inflight": {"total": 0},
            "manual_gates": 0,
            "permission_requests": 0,
            "workers": {"total": 0, "stopped": 0, "running": 0, "quarantined": 0},
            "run_dir": "/tmp/agentteam-work/runs/pursue-loop",
        }
        for stop_reason in [
            "review_gate_required",
            "blocked",
            "manual_gate_required",
            "permission_request_required",
            "failed",
            "follow_up_queue_empty",
            "max_rounds_reached",
        ]:
            summary = {
                **base_summary,
                "pursue_recap": {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 1,
                    "max_rounds": 2,
                    "stop_reason": stop_reason,
                    "latest_taskpack_id": "pursue-loop",
                    "latest_report_path": "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.md",
                    "operator_next_action": "agentteam report --taskpack pursue-loop",
                },
            }
            with self.subTest(stop_reason=stop_reason):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    _write_status_text(summary)
                output = buffer.getvalue()
                self.assertIn(f"pursue: pursue-loop stopped because {stop_reason}", output)


    def test_status_guidance_skips_pursue_queue_action_when_queue_has_no_auto_goal(self):
        summary = {
            "project": "pursue-project",
            "latest_run": "pursue-loop-r2",
            "status": "completed",
            "overall_status": "completed",
            "run_status": "completed",
            "liveness_status": "idle",
            "tasks": {"done": 1, "blocked": 0},
            "integration": {"blocked": 0},
            "integration_baseline": {
                "branch": "agentteam/run/pursue-loop-r2/integration",
                "head": "abc123",
                "worktree_exists": True,
                "worktree": "/tmp/agentteam-work/runs/pursue-loop-r2/integration/pursue-loop-r2",
            },
            "manual_gates": 0,
            "permission_requests": 0,
            "pursue_recap": {
                "pursue_id": "pursue-loop",
                "latest_taskpack_id": "pursue-loop-r2",
                "operator_next_action": "agentteam queue next --taskpack pursue-loop-r2",
                "latest_follow_up_queue": {
                    "queue_status": "no_auto_dispatchable_items",
                    "item_count": 3,
                },
            },
            "run_dir": "/tmp/agentteam-work/runs/pursue-loop-r2",
        }

        guidance = agentteam_module._status_operator_guidance(summary)

        self.assertNotIn("agentteam queue next", guidance["next_action"])
        self.assertIn("agentteam report --taskpack pursue-loop-r2", guidance["next_action"])
        self.assertIn("integration baseline", guidance["operator_hint"])


    def test_status_guidance_recomputes_stale_pursue_queue_before_recommending_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "pursue-loop-r2"
            report_path = run_dir / "reports" / "final_report.md"
            operator_report = {
                "report_schema_version": "operator_run_report.v1",
                "task_count": 1,
                "blocked_count": 0,
                "task_reports": [
                    {
                        "task_id": "queue-quality",
                        "attempt_id": "queue-quality-ATTEMPT-001",
                        "status": "implementation completed",
                        "what_changed": ["Added a narrow queue test."],
                        "changed_files": ["tests/test_taskpack.py"],
                        "verification": ["focused queue test passed"],
                        "integration": "passed",
                        "merge_recommendation": "Review before merging.",
                        "next_steps": [
                            "由 operator 审阅并决定是否集成本测试变更。",
                            (
                                "后续可继续用 `pursue_recap.structured_evidence` "
                                "驱动 `agentteam queue next` 或 report 摘要的跨轮检查。"
                            ),
                        ],
                    }
                ],
            }
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {
                    "scheduler_status": "idle",
                    "integration_baseline": {
                        "integration_baseline_status": "ready",
                        "integration_baseline_branch": "agentteam/run/pursue-loop-r2/integration",
                        "integration_baseline_worktree_path": str(
                            (run_dir / "integration-baseline").resolve()
                        ),
                        "integration_baseline_head_sha": "abc123",
                    },
                    "backlog": {"items": [{"task_id": "queue-quality", "backlog_status": "done"}]},
                    "steps": [],
                },
            )
            _write_jsonl(
                run_dir / "events.jsonl",
                [
                    {
                        "event_id": "EVT-0001",
                        "event_type": "run_completed",
                        "sequence": 1,
                        "payload": {
                            "run_status": "completed",
                            "scheduler_status": "idle",
                            "operator_report": operator_report,
                        },
                    }
                ],
            )
            goal_memory_path = work_root / "pursue" / "pursue-loop-goal-memory.json"
            _write_json(
                goal_memory_path,
                {
                    "pursue_id": "pursue-loop",
                    "work_root": str(work_root),
                    "latest_taskpack_id": "pursue-loop-r2",
                    "latest_run_ids": ["pursue-loop-r2"],
                    "follow_up_queue": [
                        {
                            "objective": (
                                "用新的 `pursue_recap.structured_evidence` "
                                "驱动后续 `agentteam queue next` 或 report 摘要的跨轮检查。"
                            ),
                            "source_taskpack_id": "pursue-loop",
                            "source_report_path": str(report_path),
                        }
                    ],
                },
            )
            _write_json(
                work_root / "pursue" / "pursue-loop.json",
                {
                    "pursue_id": "pursue-loop",
                    "pursue_status": "stopped",
                    "rounds_completed": 2,
                    "max_rounds": 2,
                    "stop_reason": "max_rounds_reached",
                    "latest_taskpack_id": "pursue-loop-r2",
                    "latest_report_path": str(report_path),
                    "goal_memory_path": str(goal_memory_path),
                    "operator_next_action": "agentteam queue next --taskpack pursue-loop-r2",
                    "latest_follow_up_queue": {
                        "queue_status": "ready",
                        "next_goal": "由 operator 审阅并决定是否集成本测试变更。",
                    },
                    "runs": [{"taskpack_id": "pursue-loop-r2"}],
                },
            )

            summary = _build_run_status_summary(
                {"project_key": "pursue-project", "work_root": str(work_root)},
                run_dir,
            )

            self.assertNotIn("agentteam queue next", summary["next_action"])
            self.assertIn("agentteam report --taskpack pursue-loop-r2", summary["next_action"])
            self.assertIn("integration baseline", summary["operator_hint"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                _write_status_text(summary)
            status_text = buffer.getvalue()
            self.assertIn(
                "pursue_next_action: agentteam queue next --taskpack pursue-loop-r2 (superseded by next_action)",
                status_text,
            )


    def test_pursue_stop_reason_blocks_operator_gates(self):
        self.assertEqual(
            _pursue_stop_reason({"status": "manual_gate_required"}),
            "manual_gate_required",
        )
        self.assertEqual(
            _pursue_stop_reason({"status": "permission_request_required"}),
            "permission_request_required",
        )
        self.assertEqual(
            _pursue_stop_reason({"status": "completed", "blocked_count": 1}),
            "blocked",
        )
        self.assertEqual(
            _pursue_stop_reason(
                {
                    "status": "completed",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "integrate"},
                }
            ),
            "review_gate_required",
        )
        self.assertIsNone(
            _pursue_stop_reason(
                {
                    "status": "completed",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "integrate_then_next"},
                },
                allow_review_gate_follow_up=True,
            )
        )


    def test_pursue_stop_reason_blocks_stopped_runs_even_when_followup_is_allowed(self):
        self.assertEqual(
            _pursue_stop_reason(
                {
                    "status": "completed",
                    "run_status": "stopped",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "integrate_then_next"},
                },
                allow_review_gate_follow_up=True,
            ),
            "stopped",
        )
        self.assertEqual(
            _pursue_stop_reason(
                {
                    "status": "stopped",
                    "run_status": "running",
                    "blocked_count": 0,
                    "follow_up_recommendation": {"action": "next"},
                },
                allow_review_gate_follow_up=True,
            ),
            "stopped",
        )


    def test_pursue_loop_passes_previous_baseline_head_to_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "agentteam-work"
            captured_submit_args = []
            source_reports = [
                {
                    "run_id": "pursue-loop",
                    "integration_baseline": {
                        "head_sha": "verified-head",
                        "branch": "agentteam/run/pursue-loop/integration",
                    },
                    "completion_summary": {
                        "next_steps": ["Continue with the next optimization task."]
                    },
                },
                {
                    "run_id": "pursue-loop-r2",
                    "integration_baseline": {
                        "head_sha": "round-two-head",
                        "branch": "agentteam/run/pursue-loop-r2/integration",
                    },
                    "completion_summary": {},
                },
            ]
            originals = {
                "_submit_args_from_profile": agentteam_module._submit_args_from_profile,
                "_handle_submit": agentteam_module._handle_submit,
                "build_run_completion_report": agentteam_module.build_run_completion_report,
                "build_goal_memory": agentteam_module.build_goal_memory,
                "write_goal_memory": agentteam_module.write_goal_memory,
                "_pursue_follow_up_queue_summary": agentteam_module._pursue_follow_up_queue_summary,
            }

            def fake_submit_args_from_profile(args, project_root, profile):
                return SimpleNamespace(
                    goal=args.goal,
                    taskpack_id=args.taskpack_id,
                    initial_integration_base_ref=None,
                )

            def fake_handle_submit(submit_args):
                captured_submit_args.append(
                    {
                        "taskpack_id": submit_args.taskpack_id,
                        "initial_integration_base_ref": getattr(
                            submit_args,
                            "initial_integration_base_ref",
                            None,
                        ),
                    }
                )
                action = "continue" if len(captured_submit_args) == 1 else "integrate"
                return {
                    "status": "completed",
                    "taskpack_id": submit_args.taskpack_id,
                    "report": {
                        "run_status": "completed",
                        "blocked_count": 0,
                        "completion_summary": {
                            "follow_up_recommendation": {"action": action}
                        },
                    },
                }

            def fake_build_run_completion_report(*args, **kwargs):
                return source_reports[len(captured_submit_args) - 1]

            def fake_build_goal_memory(**kwargs):
                return {}

            def fake_write_goal_memory(work_root_arg, goal_memory):
                memory_path = Path(work_root_arg) / "pursue" / "goal-memory.json"
                memory_path.parent.mkdir(parents=True, exist_ok=True)
                memory_path.write_text(json.dumps(goal_memory, sort_keys=True), encoding="utf-8")
                return memory_path

            def fake_queue_summary(**kwargs):
                return {
                    "queue_status": "ready",
                    "next_goal": "Continue with the next optimization task.",
                }

            try:
                agentteam_module._submit_args_from_profile = fake_submit_args_from_profile
                agentteam_module._handle_submit = fake_handle_submit
                agentteam_module.build_run_completion_report = fake_build_run_completion_report
                agentteam_module.build_goal_memory = fake_build_goal_memory
                agentteam_module.write_goal_memory = fake_write_goal_memory
                agentteam_module._pursue_follow_up_queue_summary = fake_queue_summary

                result = agentteam_module._run_pursue_loop(
                    SimpleNamespace(
                        work_root=str(work_root),
                        taskpack_id="pursue-loop",
                        goal="Improve the project.",
                        json=True,
                        allow_review_gate_follow_up=False,
                    ),
                    project_root=Path(tmp) / "repo",
                    profile={"work_root": str(work_root), "project_key": "pursue-project"},
                    goal="Improve the project.",
                    max_rounds=2,
                )
            finally:
                for name, value in originals.items():
                    setattr(agentteam_module, name, value)

            self.assertEqual(len(captured_submit_args), 2)
            self.assertIsNone(captured_submit_args[0]["initial_integration_base_ref"])
            self.assertEqual(
                captured_submit_args[1]["initial_integration_base_ref"],
                "verified-head",
            )
            self.assertEqual(
                result["runs"][1]["initial_integration_base_ref"],
                "verified-head",
            )


    def test_pursue_loop_reuses_previous_repo_map_handoff_on_next_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "agentteam-work"
            baseline = tmp_path / "integration-baseline"
            _write_json(
                baseline / taskpack_module.REPO_MAP_HANDOFF_PATH,
                {"schema_version": "repo_map_handoff.v1"},
            )
            captured_submit_args = []
            source_reports = [
                {
                    "run_id": "pursue-loop",
                    "integration_baseline": {
                        "head_sha": "verified-head",
                        "worktree_path": str(baseline),
                        "worktree_exists": True,
                    },
                    "completion_summary": {
                        "next_steps": ["Continue with the next optimization task."]
                    },
                },
                {
                    "run_id": "pursue-loop-r2",
                    "integration_baseline": {"head_sha": "round-two-head"},
                    "completion_summary": {},
                },
            ]
            originals = {
                "_submit_args_from_profile": agentteam_module._submit_args_from_profile,
                "_handle_submit": agentteam_module._handle_submit,
                "build_run_completion_report": agentteam_module.build_run_completion_report,
                "build_goal_memory": agentteam_module.build_goal_memory,
                "write_goal_memory": agentteam_module.write_goal_memory,
                "_pursue_follow_up_queue_summary": agentteam_module._pursue_follow_up_queue_summary,
            }

            def fake_submit_args_from_profile(args, project_root, profile):
                return SimpleNamespace(
                    goal=args.goal,
                    taskpack_id=args.taskpack_id,
                    initial_integration_base_ref=None,
                    reuse_repo_map_handoff_path=None,
                )

            def fake_handle_submit(submit_args):
                captured_submit_args.append(
                    {
                        "taskpack_id": submit_args.taskpack_id,
                        "reuse_repo_map_handoff_path": getattr(
                            submit_args,
                            "reuse_repo_map_handoff_path",
                            None,
                        ),
                    }
                )
                action = "continue" if len(captured_submit_args) == 1 else "integrate"
                return {
                    "status": "completed",
                    "taskpack_id": submit_args.taskpack_id,
                    "report": {
                        "run_status": "completed",
                        "blocked_count": 0,
                        "completion_summary": {
                            "follow_up_recommendation": {"action": action}
                        },
                    },
                }

            def fake_build_run_completion_report(*args, **kwargs):
                return source_reports[len(captured_submit_args) - 1]

            def fake_build_goal_memory(**kwargs):
                return {}

            def fake_write_goal_memory(work_root_arg, goal_memory):
                memory_path = Path(work_root_arg) / "pursue" / "goal-memory.json"
                memory_path.parent.mkdir(parents=True, exist_ok=True)
                memory_path.write_text(json.dumps(goal_memory, sort_keys=True), encoding="utf-8")
                return memory_path

            def fake_queue_summary(**kwargs):
                return {
                    "queue_status": "ready",
                    "next_goal": "Continue with the next optimization task.",
                }

            try:
                agentteam_module._submit_args_from_profile = fake_submit_args_from_profile
                agentteam_module._handle_submit = fake_handle_submit
                agentteam_module.build_run_completion_report = fake_build_run_completion_report
                agentteam_module.build_goal_memory = fake_build_goal_memory
                agentteam_module.write_goal_memory = fake_write_goal_memory
                agentteam_module._pursue_follow_up_queue_summary = fake_queue_summary

                result = agentteam_module._run_pursue_loop(
                    SimpleNamespace(
                        work_root=str(work_root),
                        taskpack_id="pursue-loop",
                        goal="Improve the project.",
                        json=True,
                        allow_review_gate_follow_up=False,
                    ),
                    project_root=tmp_path / "repo",
                    profile={"work_root": str(work_root), "project_key": "pursue-project"},
                    goal="Improve the project.",
                    max_rounds=2,
                )
            finally:
                for name, value in originals.items():
                    setattr(agentteam_module, name, value)

            self.assertIsNone(captured_submit_args[0]["reuse_repo_map_handoff_path"])
            self.assertEqual(
                captured_submit_args[1]["reuse_repo_map_handoff_path"],
                taskpack_module.REPO_MAP_HANDOFF_PATH,
            )
            self.assertEqual(
                result["runs"][1]["repo_map_handoff_reuse"],
                taskpack_module.REPO_MAP_HANDOFF_PATH,
            )


    def test_follow_up_queue_prefers_actionable_step_over_review_only_step(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "agentteam-long-run-reliability-003-r2",
                "report_path": (
                    "/tmp/agentteam-long-run-reliability/runs/"
                    "agentteam-long-run-reliability-003-r2/reports/final_report.md"
                ),
                "completion_summary": {
                    "next_steps": [
                        (
                            "operator 审阅 `operator_report.py` 与 `test_taskpack.py` 的 diff，"
                            "确认 scoped `review_gate` 语义符合 AgentTeam-as-target 策略。"
                        ),
                    ],
                    "follow_up_recommendation": {
                        "action": "integrate_then_next",
                        "next_command": (
                            "agentteam next --from-taskpack agentteam-long-run-reliability-003-r2 "
                            '--goal "operator 审阅 `operator_report.py` 与 `test_taskpack.py` 的 diff，'
                            '确认 scoped `review_gate` 语义符合 AgentTeam-as-target 策略。"'
                        ),
                    },
                },
            },
            goal_memory={
                "memory_path": (
                    "/tmp/agentteam-long-run-reliability/pursue/"
                    "agentteam-long-run-reliability-003-goal-memory.json"
                ),
                "follow_up_queue": [
                    {
                        "objective": "补充 queue selector 的 operator-integrated 后续验证测试。",
                        "source_taskpack_id": "agentteam-long-run-reliability-004",
                        "source_report_path": "/tmp/agentteam-long-run-reliability/runs/004/report.md",
                        "readiness": "ready",
                    }
                ],
            },
            source_taskpack_id="agentteam-long-run-reliability-003-r2",
            limit=5,
        )

        self.assertEqual(
            summary["next_goal"],
            "补充 queue selector 的 operator-integrated 后续验证测试。",
        )
        self.assertEqual(summary["selected_item"]["source"], "goal_memory.follow_up_queue")
        self.assertTrue(
            any(
                item["objective"].startswith("operator 审阅")
                for item in summary["items"]
                if isinstance(item, dict)
            )
        )


    def test_follow_up_queue_has_no_next_goal_for_review_and_process_only_items(self):
        from agentteam_runtime.follow_up_queue import build_follow_up_queue_summary

        summary = build_follow_up_queue_summary(
            source_report={
                "run_id": "db-longrun-observability-r5-r2",
                "report_path": (
                    "/tmp/agentteam-long-run-reliability/runs/"
                    "db-longrun-observability-r5-r2/reports/final_report.md"
                ),
                "completion_summary": {
                    "next_steps": [
                        "由 operator 审阅并决定是否集成本测试变更。",
                        (
                            "后续可继续用 `pursue_recap.structured_evidence` "
                            "驱动 `agentteam queue next` 或 report 摘要的跨轮检查。"
                        ),
                        "继续保持 semantic authority、source merge、push、release activation 由 operator gate 控制。",
                    ],
                    "follow_up_recommendation": {
                        "action": "integrate_then_next",
                        "next_command": (
                            "agentteam next --from-taskpack db-longrun-observability-r5-r2 "
                            '--goal "由 operator 审阅并决定是否集成本测试变更。"'
                        ),
                    },
                },
            },
            source_taskpack_id="db-longrun-observability-r5-r2",
            limit=5,
        )

        self.assertEqual(summary["queue_status"], "no_auto_dispatchable_items")
        self.assertIsNone(summary["selected_item"])
        self.assertIsNone(summary["next_goal"])
        self.assertIsNone(summary["next_command"])
        self.assertEqual(summary["items"][0]["objective"], "由 operator 审阅并决定是否集成本测试变更。")
        self.assertIn("operator_hint", summary)


    def test_follow_up_queue_text_surfaces_goal_memory_recap_fields(self):
        from agentteam_runtime.follow_up_queue import (
            build_follow_up_queue_summary,
            render_follow_up_queue_text,
        )

        report_path = "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.md"
        summary = build_follow_up_queue_summary(
            source_report={"run_id": "pursue-loop-r2"},
            goal_memory={
                "memory_path": "/tmp/agentteam-work/pursue/pursue-loop-goal-memory.json",
                "follow_up_queue": [
                    {
                        "objective": "继续实现 queue recap 展示。",
                        "source_taskpack_id": "pursue-loop",
                        "source_report_path": report_path,
                        "source_result_status": "completed",
                        "source_run_outcome": "completed_with_review_required",
                        "source_evidence_paths": [{"type": "report", "path": report_path}],
                        "stop_reason": "review_gate_required",
                        "token_usage": {
                            "usage_status": "unavailable",
                            "reported_attempt_count": 0,
                            "unreported_attempt_count": 1,
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                        },
                        "blockers": ["需要 operator review。"],
                        "suggested_verification": "python3 -m unittest test_taskpack.FollowUpQueue passed",
                    }
                ],
            },
            source_taskpack_id="pursue-loop-r2",
            limit=5,
        )

        selected = summary["selected_item"]
        self.assertEqual(selected["source"], "goal_memory.follow_up_queue")
        self.assertEqual(selected["source_result_status"], "completed")
        self.assertEqual(selected["source_run_outcome"], "completed_with_review_required")
        self.assertEqual(selected["source_evidence_paths"], [{"type": "report", "path": report_path}])
        self.assertEqual(selected["stop_reason"], "review_gate_required")
        self.assertEqual(selected["token_usage"]["usage_status"], "unavailable")
        self.assertEqual(selected["blockers"], ["需要 operator review。"])

        text = render_follow_up_queue_text(summary, next_only=True)

        self.assertIn("selected_result: completed_with_review_required", text)
        self.assertIn(f"selected_evidence_path: {report_path}", text)
        self.assertIn("selected_stop_reason: review_gate_required", text)
        self.assertIn("selected_token_usage: Token usage: unavailable", text)
        self.assertIn("selected_blockers: 需要 operator review。", text)
        self.assertIn(
            "selected_verification: python3 -m unittest test_taskpack.FollowUpQueue passed",
            text,
        )


    def test_agentteam_cli_pursue_writes_goal_memory_and_reuses_it_in_followup_context(self):
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

            completed = subprocess.run(
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
                    "2",
                    "--allow-review-gate-follow-up",
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
            self.assertEqual(summary["rounds_completed"], 2)
            memory_path = Path(summary["goal_memory_path"])
            self.assertTrue(memory_path.exists())
            self.assertIn(str(work_root.resolve()), str(memory_path))
            self.assertEqual(memory_path.parent.name, "pursue")
            memory = json.loads(memory_path.read_text(encoding="utf-8"))
            self.assertEqual(memory["memory_schema_version"], "goal_memory.v1")
            self.assertEqual(memory["pursue_id"], "pursue-loop")
            self.assertEqual(memory["original_goal"], "持续优化这个仓库的准确率和延迟。")
            self.assertEqual(memory["rounds_completed"], 2)
            self.assertEqual(memory["latest_taskpack_id"], "pursue-loop-r2")
            self.assertEqual(memory["latest_run_ids"], ["pursue-loop", "pursue-loop-r2"])
            self.assertLessEqual(len(memory["round_history"]), memory["limits"]["max_round_history"])
            self.assertLessEqual(len(json.dumps(memory, ensure_ascii=False)), memory["limits"]["max_memory_json_chars"])
            self.assertIn("goal_memory_path", summary["runs"][0])
            self.assertEqual(summary["runs"][0]["follow_up_queue"]["queue_status"], "ready")
            self.assertEqual(
                summary["runs"][0]["selected_next_goal"],
                summary["runs"][0]["follow_up_queue"]["next_goal"],
            )
            self.assertIn("agentteam queue next --taskpack pursue-loop-r2", summary["operator_next_action"])

            followup_taskpack = json.loads(
                (work_root / "frozen" / "pursue-loop-r2" / "taskpack.yaml").read_text(encoding="utf-8")
            )
            followup_goal = followup_taskpack["goal"]
            self.assertIn("Long-goal memory:", followup_goal)
            self.assertIn("completed_rounds: 1", followup_goal)
            self.assertIn("latest_run_ids: pursue-loop", followup_goal)
            self.assertIn("memory_path:", followup_goal)
            self.assertIn(summary["runs"][0]["selected_next_goal"], followup_goal)


    def test_agentteam_cli_pursue_text_output_is_compact(self):
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

            completed = subprocess.run(
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
                ],
                env=_test_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("pursue_status: stopped", completed.stdout)
            self.assertIn("rounds_completed: 1/1", completed.stdout)
            self.assertIn("stop_reason: review_gate_required", completed.stdout)
            self.assertIn("latest_taskpack_id: pursue-loop", completed.stdout)
            self.assertIn("latest_report:", completed.stdout)
            self.assertIn("pursue_recap:", completed.stdout)
            self.assertIn("operator_next_action: agentteam report --taskpack pursue-loop", completed.stdout)
            self.assertLessEqual(len([line for line in completed.stdout.splitlines() if line.strip()]), 7)


    def test_agentteam_cli_queue_show_loads_pursue_goal_memory_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "pursue-loop-r2"
            memory_path = work_root / "pursue" / "pursue-loop-goal-memory.json"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "queue-project")
            _write_completed_operator_run(run_dir)
            _write_json(
                memory_path,
                {
                    "memory_schema_version": "goal_memory.v1",
                    "memory_path": str(memory_path),
                    "latest_taskpack_id": "pursue-loop-r2",
                    "follow_up_queue": [
                        {
                            "objective": "继续修复 queue show 的 pursue recap 路径读取。",
                            "source_taskpack_id": "pursue-loop-r2",
                            "source_report_path": str(run_dir / "reports" / "final_report.md"),
                            "readiness": "ready",
                        }
                    ],
                },
            )
            _write_json(
                work_root / "pursue" / "pursue-loop.json",
                {
                    "pursue_id": "pursue-loop",
                    "rounds_completed": 2,
                    "max_rounds": 2,
                    "stop_reason": "max_rounds_reached",
                    "latest_taskpack_id": "pursue-loop-r2",
                    "latest_report_path": str(run_dir / "reports" / "final_report.md"),
                    "goal_memory_path": str(memory_path),
                    "runs": [{"taskpack_id": "pursue-loop-r2"}],
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
                    "--taskpack",
                    "pursue-loop-r2",
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
            self.assertEqual(summary["selected_item"]["source"], "report.next_steps")
            self.assertTrue(
                any(item["source"] == "goal_memory.follow_up_queue" for item in summary["items"])
            )


    def test_codex_taskpack_author_prompt_includes_roadmap_followup_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            taskpack_dir = tmp_path / "drafts" / "m67-roadmap-followup"
            author_context_dir = tmp_path / "drafts" / ".m67-roadmap-followup-author"
            _init_repo(repo)

            prompt = _author_prompt(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the roadmap-derived implementation route.\n\n"
                    "Previous taskpack context:\n"
                    "- source_taskpack_id: m67-agentteam-dogfood\n"
                    "- source_report_path: /tmp/work/runs/m67/reports/final_report.md\n"
                    "- selected next_goal: Add taskpack-author route-template guidance.\n"
                ),
                taskpack_id="m67-roadmap-followup",
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

            self.assertIn("Roadmap-derived follow-up task template:", prompt)
            self.assertIn("evidence_paths", prompt)
            self.assertIn("non_goals", prompt)
            self.assertIn("success_metrics_or_no_metric_delta", prompt)
            self.assertIn("roadmap_followup_route_template", prompt)
            self.assertIn("source merge, push, and release activation remain operator review gates", prompt)


    def test_author_stage_context_distinguishes_initial_and_follow_up_rounds(self):
        initial = _author_model_invocation_context(
            taskpack_id="round-one",
            draft_root="/tmp/work/drafts",
            model=None,
            supported=False,
            supplied={
                "project": "project",
                "pursue_id": "PURSUE-1",
                "round_index": 1,
                "usage_stage": "taskpack_author",
                "experiment_controller_reference": {
                    "schema_version": (
                        "experiment_budget_controller_reference.v1"
                    )
                },
                "experiment_controller_required": True,
            },
        )
        follow_up = _author_model_invocation_context(
            taskpack_id="round-two",
            draft_root="/tmp/work/drafts",
            model=None,
            supported=False,
            supplied={
                "project": "project",
                "pursue_id": "PURSUE-1",
                "round_index": 2,
                "usage_stage": "follow_up_author",
            },
        )

        self.assertEqual(initial["usage_stage"], "taskpack_author")
        self.assertEqual(initial["round_index"], 1)
        self.assertTrue(initial["experiment_controller_required"])
        self.assertEqual(
            initial["experiment_controller_reference"]["schema_version"],
            "experiment_budget_controller_reference.v1",
        )
        self.assertEqual(follow_up["usage_stage"], "follow_up_author")
        self.assertEqual(follow_up["role"], "follow_up_author")
        self.assertEqual(follow_up["round_index"], 2)


    def test_auto_materialize_semantic_taskpack_completes_roadmap_skeleton_without_operator_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            drafts = tmp_path / "drafts"
            materialized_root = tmp_path / "materialized"
            frozen_root = tmp_path / "frozen"
            run_root = tmp_path / "runs"
            _init_repo(repo)
            (repo / "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime").mkdir(parents=True)

            skeleton = draft_deterministic_taskpack_skeleton(
                project_root=repo,
                goal=(
                    "Follow-up goal:\n"
                    "Continue the roadmap-derived implementation route.\n\n"
                    "Previous taskpack context:\n"
                    "- source_report_path: /tmp/work/runs/previous/reports/final_report.md\n\n"
                    "Instructions for the new taskpack:\n"
                    "- Use the queue-selected next_goal to produce an executable taskpack."
                ),
                draft_root=drafts,
                taskpack_id="auto-roadmap-skeleton",
                context_refs={
                    "source_report_path": "/tmp/work/runs/previous/reports/final_report.md",
                    "repo_context_path": "/tmp/work/repo_contexts/implementation_worker.json",
                    "repo_map_manifest_path": "/tmp/work/state/repo_map/manifest.json",
                    "selected_next_goal": "Implement automatic semantic completion authoring path.",
                    "read_scope": "\n".join(
                        [
                            "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                            "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                        ]
                    ),
                    "write_scope": "\n".join(
                        [
                            "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                            "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                        ]
                    ),
                    "non_goals": "merge, push, release activation",
                },
            )

            materialized = taskpack_module.auto_materialize_semantic_taskpack(
                skeleton["taskpack_dir"],
                output_root=materialized_root,
                taskpack_id="auto-roadmap-executable",
            )

            taskpack_dir = Path(materialized["taskpack_dir"])
            loaded = load_taskpack(taskpack_dir)
            taskpack = loaded["taskpack"]
            item = loaded["backlog"]["items"][0]
            self.assertEqual(validate_taskpack(taskpack_dir)["status"], "accepted")
            self.assertFalse(taskpack.get("semantic_authoring_required"))
            self.assertEqual(taskpack["authoring_mode"], "semantic_materialized")
            self.assertEqual(taskpack["semantic_completion"]["authority"], "automatic_deterministic")
            self.assertIs(taskpack["semantic_completion"]["operator_semantic_json_required"], False)
            self.assertIn("source_report_path", item["objective"])
            self.assertIn("queue-selected next_goal", item["objective"])
            self.assertEqual(
                item["read_scope"],
                [
                    "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                    "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                ],
            )
            self.assertEqual(
                item["write_scope"],
                [
                    "experiments/native_agentteam_runtime/m0_runtime/agentteam_runtime/taskpack.py",
                    "experiments/native_agentteam_runtime/m0_runtime/tests/test_taskpack.py",
                ],
            )
            self.assertIn("roadmap_followup_route_template", item["required_deliverables"])
            self.assertIn("agentteam_target_review_gate", item["required_deliverables"])
            self.assertIn(
                "/tmp/work/runs/previous/reports/final_report.md",
                item["semantic_materialization"]["evidence_paths"],
            )
            self.assertEqual(item["blockers"], [])

            frozen = freeze_taskpack(taskpack_dir, frozen_root)
            args = build_taskpack_runtime_args(frozen["frozen_taskpack_dir"], run_root=run_root)

            self.assertEqual(_arg_value(args, "--backlog"), str(Path(frozen["frozen_taskpack_dir"]) / "backlog.json"))
            self.assertTrue((run_root / "auto-roadmap-executable").exists())


    def test_pursue_next_integration_base_ref_prefers_latest_baseline_head(self):
        report = {
            "integration_baseline": {
                "head_sha": "verified-head",
                "branch": "agentteam/run/previous/integration",
            }
        }

        self.assertEqual(_pursue_next_integration_base_ref(report), "verified-head")


    def test_pursue_next_integration_base_ref_falls_back_to_branch(self):
        report = {
            "integration_baseline": {
                "head_sha": None,
                "branch": "agentteam/run/previous/integration",
            }
        }

        self.assertEqual(
            _pursue_next_integration_base_ref(report),
            "agentteam/run/previous/integration",
        )

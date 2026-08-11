try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class ReportsMixin:
    def test_completion_summary_chinese_brief_includes_follow_up_reason(self):
        from agentteam_runtime.completion_summary import build_completion_summary

        summary = build_completion_summary(
            run_id="RUN-CHINESE-BRIEF",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-CHINESE-BRIEF",
                    "status": "implementation completed",
                    "what_changed": ["补充中文摘要。"],
                    "changed_files": ["agentteam_runtime/operator_brief.py"],
                    "verification": ["test_m0_runtime: passed"],
                    "integration": "passed",
                    "next_steps": ["继续验证 Feishu 摘要。"],
                }
            ],
        )

        self.assertEqual(
            summary["follow_up_recommendation"]["reason"],
            "The run completed with a recommended next implementation step.",
        )
        self.assertIn(
            (
                "下一步原因：The run completed with a recommended next implementation step."
                "；相关下一步：继续验证 Feishu 摘要。"
            ),
            summary["chinese_operator_brief"],
        )
        self.assertIn(
            (
                "下一步原因：The run completed with a recommended next implementation step."
                "；相关下一步：继续验证 Feishu 摘要。"
            ),
            summary["operator_digest"],
        )


    def test_completion_summary_operator_digest_includes_why_and_risks(self):
        from agentteam_runtime.completion_summary import build_completion_summary

        summary = build_completion_summary(
            run_id="RUN-CHINESE-DIGEST",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-REPORT",
                    "status": "implementation completed",
                    "why": ["为了让 operator 在多轮结束后看清本轮取舍。"],
                    "what_changed": ["补充中文 operator digest。"],
                    "changed_files": ["agentteam_runtime/completion_summary.py"],
                    "verification": ["test_m0_runtime: passed"],
                    "risks": ["真实 Feishu webhook 仍需 operator 环境验证。"],
                    "integration": "passed",
                    "next_steps": ["运行 bounded dogfood。"],
                }
            ],
        )

        self.assertEqual(
            summary["why"],
            ["为了让 operator 在多轮结束后看清本轮取舍。"],
        )
        self.assertEqual(
            summary["risks"],
            ["真实 Feishu webhook 仍需 operator 环境验证。"],
        )
        self.assertIn(
            "为什么：为了让 operator 在多轮结束后看清本轮取舍。",
            summary["operator_digest"],
        )
        self.assertIn(
            "风险：真实 Feishu webhook 仍需 operator 环境验证。",
            summary["operator_digest"],
        )


    def test_completion_summary_operator_digest_includes_integration_details(self):
        from agentteam_runtime.completion_summary import build_completion_summary

        summary = build_completion_summary(
            run_id="RUN-INTEGRATION-DETAILS",
            run_status="completed",
            task_count=1,
            blocked_count=1,
            task_reports=[
                {
                    "task_id": "TASK-INTEGRATION-DETAILS",
                    "status": "implementation completed, integration blocked",
                    "what_changed": ["补充 worker 新增验证执行链路。"],
                    "changed_files": ["agentteam_runtime/two_phase_scheduler.py"],
                    "verification": ["test_m0_runtime: passed"],
                    "integration": (
                        "failed: rejected verification addition "
                        "worker-added-shell-check"
                    ),
                }
            ],
        )

        self.assertEqual(summary["integration"], "blocked")
        self.assertEqual(
            summary["integration_details"],
            ["failed: rejected verification addition worker-added-shell-check"],
        )
        self.assertIn(
            (
                "集成状态：failed: rejected verification addition "
                "worker-added-shell-check"
            ),
            summary["operator_digest"],
        )


    def test_completion_summary_operator_digest_derives_review_gate_risk(self):
        from agentteam_runtime.completion_summary import build_completion_summary

        summary = build_completion_summary(
            run_id="RUN-REVIEW-GATE-RISK",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-REVIEW-GATE",
                    "status": "implementation completed",
                    "what_changed": ["补充 review gate 风险汇报。"],
                    "changed_files": ["agentteam_runtime/completion_summary.py"],
                    "verification": ["test_m0_runtime: passed"],
                    "integration": "passed",
                }
            ],
            integration_baseline={"branch": "agentteam/run/RUN-REVIEW-GATE-RISK/integration"},
        )

        self.assertIn(
            "存在 review gate；source merge、push、release activation 仍需 operator 审阅。",
            summary["risks"],
        )
        self.assertIn(
            "风险：存在 review gate；source merge、push、release activation 仍需 operator 审阅。",
            summary["operator_digest"],
        )


    def test_completion_summary_treats_rejected_salvage_as_review_blocker(self):
        from agentteam_runtime.completion_summary import build_completion_summary

        summary = build_completion_summary(
            run_id="RUN-SALVAGE",
            run_status="completed",
            task_count=1,
            blocked_count=0,
            task_reports=[
                {
                    "task_id": "TASK-SALVAGE",
                    "status": "implementation rejected",
                    "what_changed": [
                        "Patch available from worker worktree after timeout or missing outbox; not accepted automatically."
                    ],
                    "changed_files": ["agentteam_runtime/mailbox_worker.py"],
                    "verification": [],
                    "integration": "not requested",
                    "merge_recommendation": "Do not merge until the task is accepted.",
                }
            ],
            integration_baseline={"branch": "agentteam/run/RUN-SALVAGE/integration"},
        )

        self.assertEqual(
            summary["status_line"],
            "completed: 1 task reported, 1 blocked task",
        )
        self.assertEqual(
            summary["follow_up_recommendation"]["action"],
            "review_blocker",
        )
        self.assertEqual(
            summary["integration_recommendation"],
            "Do not merge until integration passes.",
        )


    def test_operator_report_counts_rejected_task_as_blocked(self):
        from agentteam_runtime.two_phase_scheduler import _operator_report_from_state

        report = _operator_report_from_state(
            {
                "steps": [
                    {
                        "task_id": "TASK-SALVAGE",
                        "result": {
                            "task_id": "TASK-SALVAGE",
                            "validation_status": "rejected",
                            "runtime_output": {
                                "summary": (
                                    "Patch available from worker worktree after timeout "
                                    "or missing outbox; not accepted automatically."
                                )
                            },
                            "changed_files": ["agentteam_runtime/mailbox_worker.py"],
                            "integration_verification_status": "not_requested",
                        },
                    }
                ]
            }
        )

        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["blocked_count"], 1)
        self.assertEqual(report["task_reports"][0]["status"], "implementation rejected")


    def test_operator_report_counts_backlog_integration_block_as_blocked(self):
        from agentteam_runtime.two_phase_scheduler import _operator_report_from_state

        report = _operator_report_from_state(
            {
                "backlog": {
                    "items": [
                        {
                            "task_id": "TASK-EVIDENCE",
                            "backlog_status": "blocked",
                            "blockers": ["integration_evidence_incomplete"],
                        }
                    ]
                },
                "steps": [
                    {
                        "task_id": "TASK-EVIDENCE",
                        "result": {
                            "task_id": "TASK-EVIDENCE",
                            "validation_status": "accepted",
                            "runtime_output": {
                                "summary": "Implementation completed."
                            },
                            "changed_files": ["agentteam_runtime/example.py"],
                            "integration_verification_status": "not_requested",
                        },
                    }
                ],
            }
        )

        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["blocked_count"], 1)
        self.assertEqual(report["blocked_task_ids"], ["TASK-EVIDENCE"])


    def test_run_report_recomputes_backlog_block_from_scheduler_state(self):
        from agentteam_runtime.operator_report import build_run_completion_report

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "RUN-EVIDENCE"
            state_dir = run_dir / "state"
            state_dir.mkdir(parents=True)
            event = {
                "event_type": "run_completed",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "report_schema_version": "operator_run_report.v1",
                        "task_count": 1,
                        "blocked_count": 0,
                        "task_reports": [
                            {
                                "task_id": "TASK-EVIDENCE",
                                "status": "implementation completed",
                                "integration": "not requested",
                            }
                        ],
                    },
                },
            }
            (run_dir / "events.jsonl").write_text(
                json.dumps(event, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (state_dir / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "idle",
                        "backlog": {
                            "items": [
                                {
                                    "task_id": "TASK-EVIDENCE",
                                    "backlog_status": "blocked",
                                }
                            ]
                        },
                        "steps": [],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_run_completion_report(
                run_dir,
                project="agentteam",
                write_files=False,
            )

        self.assertEqual(report["blocked_count"], 1)
        self.assertEqual(
            report["run_outcome"],
            "completed_with_review_required",
        )


    def test_run_completed_event_contains_operator_report_from_runtime_summary(self):
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
            scheduler.tick()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/result.json"],
                {
                    "summary": [
                        "用自然语言汇总 worker 完成的实现内容。",
                        "保留机器可读 changed_files 作为辅助定位。",
                    ],
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 300,
                        "total_tokens": 1500,
                    },
                    "verification": {
                        "unit_tests": {
                            "command": "python3 -m unittest",
                            "status": "passed",
                        }
                    },
                },
            )

            scheduler.run_until_idle(max_ticks=5, poll_interval_seconds=0)

            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            run_completed = [
                event for event in events if event["event_type"] == "run_completed"
            ][0]
            report = run_completed["payload"]["operator_report"]
            task_report = report["task_reports"][0]

            self.assertEqual(report["report_schema_version"], "operator_run_report.v1")
            self.assertEqual(
                report["token_usage"],
                {
                    "usage_status": "reported",
                    "usage_source": "runtime_result",
                    "usage_sources": ["runtime_result"],
                    "reported_attempt_count": 1,
                    "unreported_attempt_count": 0,
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "total_tokens": 1500,
                    "cached_input_tokens": None,
                    "reasoning_tokens": None,
                },
            )
            self.assertEqual(task_report["task_id"], "TASK-001")
            self.assertEqual(task_report["token_usage"]["usage_source"], "runtime_result")
            self.assertEqual(task_report["token_usage"]["total_tokens"], 1500)
            self.assertEqual(
                task_report["what_changed"],
                [
                    "用自然语言汇总 worker 完成的实现内容。",
                    "保留机器可读 changed_files 作为辅助定位。",
                ],
            )
            self.assertEqual(task_report["changed_files"], ["generated/result.json"])
            self.assertEqual(task_report["verification"], ["unit_tests: passed"])
            self.assertEqual(task_report["integration"], "not requested")

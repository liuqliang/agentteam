try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class NotificationsMixin:
    def test_feishu_custom_bot_notifier_signs_and_formats_manual_gate_message(self):
        from agentteam_runtime.notifications import (
            FeishuWebhookNotifier,
            feishu_custom_bot_sign,
        )

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append(
                {
                    "url": url,
                    "payload": payload,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        notifier = FeishuWebhookNotifier(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            signing_secret="demo",
            project="agentteam",
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = notifier.notify_manual_gate(
            {
                "event_id": "EVT-12",
                "sequence": 12,
                "event_type": "manual_gate_required",
                "correlation_id": "TASK-001:TASK-001-ATTEMPT-001",
                "payload": {
                    "task_id": "TASK-001",
                    "attempt_id": "TASK-001-ATTEMPT-001",
                    "question_id": "Q-TASK-001-ATTEMPT-001",
                    "question": "Should runtime notify Feishu?",
                    "reason": "Worker needs operator guidance.",
                },
            },
            run_dir="/tmp/agentteam-run",
        )

        self.assertEqual(
            feishu_custom_bot_sign(1599360473, "demo"),
            "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=",
        )
        self.assertEqual(result["event_type"], "notification_sent")
        self.assertEqual(result["payload"]["provider"], "feishu")
        self.assertEqual(result["payload"]["project"], "agentteam")
        self.assertEqual(result["payload"]["source_event_id"], "EVT-12")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["url"],
            "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
        )
        payload = calls[0]["payload"]
        self.assertEqual(payload["timestamp"], "1599360473")
        self.assertEqual(payload["sign"], "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("Q-TASK-001-ATTEMPT-001", payload["content"]["text"])
        self.assertIn("resume --run-dir /tmp/agentteam-run", payload["content"]["text"])
        event_payload = json.dumps(result["payload"], sort_keys=True)
        self.assertNotIn("secret-token", event_payload)
        self.assertNotIn("demo", event_payload)


    def test_feishu_custom_bot_notifier_records_failure_without_secret_leak(self):
        from agentteam_runtime.notifications import FeishuWebhookNotifier

        calls = []

        def failing_post(_url, _payload, _timeout_seconds):
            calls.append({"url": _url, "payload": _payload, "timeout_seconds": _timeout_seconds})
            raise TimeoutError("network timed out near webhook secret-token")

        notifier = FeishuWebhookNotifier(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            signing_secret="demo",
            project="agentteam",
            http_post=failing_post,
            clock=lambda: 1599360473,
        )
        result = notifier.notify_manual_gate(
            {
                "event_id": "EVT-12",
                "sequence": 12,
                "event_type": "manual_gate_required",
                "correlation_id": "TASK-001:TASK-001-ATTEMPT-001",
                "payload": {
                    "task_id": "TASK-001",
                    "question_id": "Q-TASK-001-ATTEMPT-001",
                    "question": "Should runtime notify Feishu?",
                },
            },
            run_dir="/tmp/agentteam-run",
        )

        self.assertEqual(result["event_type"], "notification_failed")
        payload = json.dumps(result["payload"], sort_keys=True)
        self.assertIn("TimeoutError", payload)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("demo", payload)


    def test_feishu_custom_bot_notifier_retries_failure_with_bounded_metadata(self):
        from agentteam_runtime.notifications import FeishuWebhookNotifier

        calls = []

        def failing_post(_url, _payload, _timeout_seconds):
            calls.append({"url": _url, "payload": _payload, "timeout_seconds": _timeout_seconds})
            raise TimeoutError("network timed out near webhook secret-token")

        notifier = FeishuWebhookNotifier(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            signing_secret="demo",
            project="agentteam",
            http_post=failing_post,
            clock=lambda: 1599360473,
            max_attempts=2,
        )
        result = notifier.notify_event(
            {
                "event_id": "EVT-RETRY",
                "sequence": 13,
                "event_type": "run_completed",
                "correlation_id": "run:retry",
                "payload": {"run_status": "completed"},
            },
            run_dir="/tmp/agentteam-run",
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["event_type"], "notification_failed")
        self.assertEqual(result["payload"]["notification_status"], "failed")
        self.assertEqual(result["payload"]["delivery_attempt_count"], 2)
        self.assertEqual(result["payload"]["max_delivery_attempts"], 2)
        attempts = result["payload"]["delivery_attempts"]
        self.assertEqual([attempt["attempt"] for attempt in attempts], [1, 2])
        self.assertEqual({attempt["variant"] for attempt in attempts}, {"rich_text"})
        self.assertEqual({attempt["error_class"] for attempt in attempts}, {"TimeoutError"})
        payload = json.dumps(result["payload"], sort_keys=True)
        self.assertIn("[redacted]", payload)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("demo", payload)


    def test_feishu_custom_bot_notifier_falls_back_when_rich_message_is_rejected(self):
        from agentteam_runtime.notifications import FeishuWebhookNotifier

        calls = []

        def fake_post(_url, payload, _timeout_seconds):
            calls.append(payload)
            if len(calls) == 1:
                return {
                    "status_code": 200,
                    "body": {
                        "code": 11232,
                        "msg": "rich message rejected near webhook secret-token",
                    },
                }
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        notifier = FeishuWebhookNotifier(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            signing_secret="demo",
            project="agentteam",
            http_post=fake_post,
            clock=lambda: 1599360473,
            max_attempts=2,
        )
        result = notifier.notify_event(
            {
                "event_id": "EVT-FALLBACK",
                "sequence": 14,
                "event_type": "run_completed",
                "correlation_id": "run:fallback",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "task_reports": [
                            {
                                "task_id": "TASK-FALLBACK",
                                "status": "implementation completed",
                                "what_changed": ["Built a rich Feishu notification."],
                                "verification": ["unit test passed"],
                                "integration": "passed",
                            }
                        ],
                    },
                },
            },
            run_dir="/tmp/agentteam-run",
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["event_type"], "notification_sent")
        self.assertEqual(result["payload"]["notification_status"], "sent")
        self.assertTrue(result["payload"]["fallback_used"])
        self.assertEqual(result["payload"]["fallback_reason"], "rich_message_rejected")
        self.assertEqual(result["payload"]["delivery_variant"], "concise_text")
        self.assertEqual(result["payload"]["delivery_attempt_count"], 2)
        self.assertEqual(result["payload"]["delivery_attempts"][0]["body_code"], 11232)
        self.assertEqual(result["payload"]["delivery_attempts"][1]["result"], "sent")
        self.assertLess(
            len(calls[1]["content"]["text"]),
            len(calls[0]["content"]["text"]),
        )
        payload = json.dumps(result["payload"], sort_keys=True)
        self.assertIn("body_code=11232", payload)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("demo", payload)


    def test_feishu_webhook_diagnosis_tests_variants_without_secret_leak(self):
        from agentteam_runtime.notifications import diagnose_feishu_webhook_delivery

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        summary = diagnose_feishu_webhook_delivery(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            signing_secret="demo",
            project="agentteam",
            http_post=fake_post,
            clock=lambda: 1599360473,
        )

        self.assertEqual(summary["diagnosis_status"], "sent")
        self.assertEqual([item["variant"] for item in summary["delivery_variants"]], ["rich_text", "concise_text"])
        self.assertEqual([item["notification_status"] for item in summary["delivery_variants"]], ["sent", "sent"])
        self.assertEqual(len(calls), 2)
        self.assertEqual({call["payload"]["msg_type"] for call in calls}, {"text"})
        payload = json.dumps(summary, sort_keys=True)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("demo", payload)
        self.assertIn(
            "Token usage: not applicable (diagnostic_notification)",
            calls[0]["payload"]["content"]["text"],
        )


    def test_feishu_notification_sink_from_env_skips_missing_webhook(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_MISSING_WEBHOOK",
            signing_secret_env="AGENTTEAM_FEISHU_MISSING_SECRET",
            project="agentteam",
            env={},
        )

        self.assertIsNone(sink)


    def test_feishu_notification_sink_from_env_sends_manual_gate(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_TEST_WEBHOOK",
            signing_secret_env="AGENTTEAM_FEISHU_TEST_SECRET",
            project="agentteam",
            env={
                "AGENTTEAM_FEISHU_TEST_WEBHOOK": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
                ),
                "AGENTTEAM_FEISHU_TEST_SECRET": "demo",
            },
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = sink.notify(
            {
                "event_id": "EVT-12",
                "sequence": 12,
                "event_type": "manual_gate_required",
                "correlation_id": "TASK-001:TASK-001-ATTEMPT-001",
                "payload": {
                    "task_id": "TASK-001",
                    "question_id": "Q-TASK-001-ATTEMPT-001",
                    "question": "Should runtime notify Feishu?",
                },
            },
            {"run_dir": "/tmp/agentteam-run"},
        )

        self.assertEqual(result["event_type"], "notification_sent")
        self.assertEqual(calls[0]["payload"]["sign"], "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")
        self.assertNotIn("Token usage: unavailable", calls[0]["payload"]["content"]["text"])
        self.assertNotIn("secret-token", json.dumps(result["payload"], sort_keys=True))
        self.assertNotIn("demo", json.dumps(result["payload"], sort_keys=True))


    def test_feishu_notification_sink_from_env_sends_run_completed(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_TEST_WEBHOOK",
            project="agentteam",
            env={
                "AGENTTEAM_FEISHU_TEST_WEBHOOK": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
                ),
            },
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = sink.notify(
            {
                "event_id": "EVT-99",
                "sequence": 99,
                "event_type": "run_completed",
                "correlation_id": "run:completed",
                "payload": {
                    "run_status": "completed",
                    "task_done_count": 3,
                    "task_blocked_count": 0,
                },
            },
            {"run_dir": "/tmp/agentteam-run"},
        )

        self.assertEqual(result["event_type"], "notification_sent")
        self.assertEqual(result["payload"]["source_event_type"], "run_completed")
        self.assertEqual(result["payload"]["message_summary"], "run_completed completed")
        self.assertEqual(len(calls), 1)
        self.assertIn("[AgentTeam] run_completed", calls[0]["payload"]["content"]["text"])
        self.assertIn("Run dir: /tmp/agentteam-run", calls[0]["payload"]["content"]["text"])


    def test_feishu_run_completed_includes_operator_report(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_TEST_WEBHOOK",
            project="verisilicon",
            env={
                "AGENTTEAM_FEISHU_TEST_WEBHOOK": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
                ),
            },
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = sink.notify(
            {
                "event_id": "EVT-99",
                "sequence": 99,
                "event_type": "run_completed",
                "correlation_id": "run:completed",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "report_schema_version": "operator_run_report.v1",
                        "task_reports": [
                            {
                                "task_id": "optimize-gesture-evaluation-pipeline",
                                "status": "implementation completed, integration blocked",
                                "what_changed": [
                                    "优化了 IMU 解析和特征提取流程。",
                                    "延后 negative window 特征提取以减少计算量。",
                                ],
                                "changed_files": [
                                    "gesture_recognition/sim_eval.py",
                                    "gesture_recognition/tests/test_sim_eval.py",
                                ],
                                "verification": [
                                    "compileall: passed",
                                    "sim_eval_tests: passed",
                                ],
                                "integration": "failed: FAILED (failures=1)",
                                "merge_recommendation": "Do not merge until integration passes.",
                                "next_steps": ["恢复不改变采样语义的优化，或重新导出 C 模型。"],
                            }
                        ],
                    },
                },
            },
            {"run_dir": "/tmp/agentteam-run"},
        )

        self.assertEqual(result["event_type"], "notification_sent")
        text = calls[0]["payload"]["content"]["text"]
        self.assertIn("工作摘要:", text)
        self.assertIn("- 状态：completed；任务：1；阻塞：1", text)
        self.assertIn("中文工作汇报:", text)
        self.assertIn("Token usage: unavailable", text)
        self.assertIn("优化了 IMU 解析和特征提取流程。", text)
        self.assertIn("gesture_recognition/sim_eval.py", text)
        self.assertIn("compileall: passed", text)
        self.assertIn("合并建议：Do not merge until integration passes.", text)
        self.assertIn("恢复不改变采样语义的优化，或重新导出 C 模型。", text)
        self.assertIn(
            (
                "下一步原因：A blocked or failed task needs operator review before follow-up work."
                "；相关下一步：恢复不改变采样语义的优化，或重新导出 C 模型。"
            ),
            text,
        )
        self.assertNotIn("Completion summary:", text)
        self.assertNotIn("中文简报:", text)
        self.assertNotIn("What changed:", text)
        self.assertNotIn("Changed files:", text)
        self.assertNotIn("Task: optimize-gesture-evaluation-pipeline", text)


    def test_feishu_run_completed_includes_chinese_why_risks_and_pursue_recap(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_TEST_WEBHOOK",
            project="agentteam",
            env={
                "AGENTTEAM_FEISHU_TEST_WEBHOOK": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
                ),
            },
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = sink.notify(
            {
                "event_id": "EVT-102",
                "sequence": 102,
                "event_type": "run_completed",
                "correlation_id": "run:completed",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "report_schema_version": "operator_run_report.v1",
                        "task_count": 1,
                        "blocked_count": 0,
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
                        "task_reports": [
                            {
                                "task_id": "round-002-reporting",
                                "status": "implementation completed",
                                "why": ["为了让 operator 在多轮结束后看清本轮取舍。"],
                                "what_changed": ["补充中文汇报字段映射。"],
                                "changed_files": ["agentteam_runtime/operator_report.py"],
                                "verification": ["test_m0_runtime: passed"],
                                "risks": ["真实 Feishu webhook 仍需 operator 环境验证。"],
                                "integration": "passed",
                                "next_steps": ["运行 2 轮 AgentTeam-as-target dogfood。"],
                            }
                        ],
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
            },
            {"run_dir": "/tmp/agentteam-run"},
        )

        self.assertEqual(result["event_type"], "notification_sent")
        text = calls[0]["payload"]["content"]["text"]
        self.assertIn("Pursue 轮次：2/4", text)
        self.assertIn("停止原因：review_gate_required", text)
        self.assertIn("下一步：继续执行 bounded dogfood 验证。", text)
        self.assertIn("为什么：为了让 operator 在多轮结束后看清本轮取舍。", text)
        self.assertIn("风险：真实 Feishu webhook 仍需 operator 环境验证。", text)
        self.assertIn("涉及文件：agentteam_runtime/operator_report.py", text)
        self.assertIn("验证结果：test_m0_runtime: passed", text)
        self.assertIn("Token usage: unavailable", text)


    def test_feishu_run_completed_summarizes_multiple_tasks(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_TEST_WEBHOOK",
            project="agentteam",
            env={
                "AGENTTEAM_FEISHU_TEST_WEBHOOK": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
                ),
            },
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = sink.notify(
            {
                "event_id": "EVT-100",
                "sequence": 100,
                "event_type": "run_completed",
                "correlation_id": "run:completed",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "report_schema_version": "operator_run_report.v1",
                        "task_count": 2,
                        "blocked_count": 0,
                        "task_reports": [
                            {
                                "task_id": "TASK-A",
                                "status": "implementation completed",
                                "what_changed": ["实现任务 A 的代码路径。"],
                                "changed_files": ["src/a.py"],
                                "verification": ["unit-a: passed"],
                                "integration": "passed",
                                "next_steps": ["继续验证 A。"],
                            },
                            {
                                "task_id": "TASK-B",
                                "status": "implementation completed",
                                "what_changed": ["补充任务 B 的验证入口。"],
                                "changed_files": ["src/b.py"],
                                "verification": ["unit-b: passed"],
                                "integration": "passed",
                                "next_steps": ["继续验证 B。"],
                            },
                        ],
                    },
                },
            },
            {"run_dir": "/tmp/agentteam-run"},
        )

        self.assertEqual(result["event_type"], "notification_sent")
        text = calls[0]["payload"]["content"]["text"]
        self.assertIn("工作摘要:", text)
        self.assertIn("中文工作汇报:", text)
        self.assertIn("做了什么：实现任务 A 的代码路径。；补充任务 B 的验证入口。", text)
        self.assertIn("涉及文件：src/a.py；src/b.py", text)
        self.assertIn("验证结果：unit-a: passed；unit-b: passed", text)
        self.assertNotIn("Completion summary:", text)
        self.assertNotIn("What changed:", text)
        self.assertNotIn("Task: TASK-A", text)
        self.assertNotIn("Task: TASK-B", text)


    def test_feishu_run_completed_uses_operator_summary_without_zero_task_fallback(self):
        from agentteam_runtime.notifications import build_feishu_notification_sink_from_env

        calls = []

        def fake_post(url, payload, timeout_seconds):
            calls.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})
            return {"status_code": 200, "body": {"code": 0, "msg": "success"}}

        sink = build_feishu_notification_sink_from_env(
            webhook_env="AGENTTEAM_FEISHU_TEST_WEBHOOK",
            project="agentteam",
            env={
                "AGENTTEAM_FEISHU_TEST_WEBHOOK": (
                    "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
                ),
            },
            http_post=fake_post,
            clock=lambda: 1599360473,
        )
        result = sink.notify(
            {
                "event_id": "EVT-101",
                "sequence": 101,
                "event_type": "run_completed",
                "correlation_id": "run:milestone",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "operator_summary": {
                            "operator_digest": [
                                "M65 已完成：新增 agentteam grounding。",
                                "验证：412 tests OK。",
                            ]
                        },
                        "token_usage": {
                            "usage_status": "reported",
                            "reported_attempt_count": 1,
                            "unreported_attempt_count": 0,
                            "input_tokens": 4200,
                            "output_tokens": 800,
                            "total_tokens": 5000,
                        },
                    },
                },
            },
            {"run_dir": "/tmp/agentteam-run"},
        )

        self.assertEqual(result["event_type"], "notification_sent")
        text = calls[0]["payload"]["content"]["text"]
        self.assertIn("中文工作汇报:", text)
        self.assertIn("M65 已完成：新增 agentteam grounding。", text)
        self.assertIn("验证：412 tests OK。", text)
        self.assertIn("Token usage: total=5000 input=4200 output=800 reported=1/1", text)
        self.assertNotIn("0 tasks reported", text)
        self.assertNotIn("No task-level operator report was found", text)


    def test_two_phase_scheduler_emits_run_started_and_completed_notifications(self):
        class RecordingNotificationSink:
            def __init__(self):
                self.calls = []

            def notify(self, event, context):
                self.calls.append({"event": event, "context": context})
                return {
                    "event_type": "notification_sent",
                    "actor": "agent-notifier",
                    "target_agent_id": None,
                    "idempotency_key": f"notification:{event['event_id']}",
                    "correlation_id": event["correlation_id"],
                    "payload": {
                        "provider": "feishu",
                        "project": "agentteam",
                        "source_event_type": event["event_type"],
                        "source_event_id": event["event_id"],
                        "source_event_sequence": event["sequence"],
                        "notification_status": "sent",
                        "message_summary": event["event_type"],
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"], tasks=[])
            sink = RecordingNotificationSink()
            scheduler = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
                notification_sink=sink,
            )

            summary = scheduler.run_until_idle(max_ticks=1, poll_interval_seconds=0)

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(
                [call["event"]["event_type"] for call in sink.calls],
                ["run_started", "run_completed"],
            )
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["event_type"] for event in events if event["event_type"].startswith("run_")],
                ["run_started", "run_completed"],
            )
            self.assertEqual(
                [
                    event["payload"]["source_event_type"]
                    for event in events
                    if event["event_type"] == "notification_sent"
                ],
                ["run_started", "run_completed"],
            )


    def test_cli_diagnoses_feishu_webhook_variants_without_printing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            env["AGENTTEAM_FEISHU_DIAG_WEBHOOK"] = (
                "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
            )
            env["AGENTTEAM_FEISHU_DIAG_SECRET"] = "demo-secret"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--output-dir",
                    str(output_dir),
                    "--diagnose-feishu-webhook",
                    "--diagnose-feishu-dry-run",
                    "--notification-project",
                    "agentteam",
                    "--feishu-webhook-env",
                    "AGENTTEAM_FEISHU_DIAG_WEBHOOK",
                    "--feishu-signing-secret-env",
                    "AGENTTEAM_FEISHU_DIAG_SECRET",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["diagnosis_status"], "dry_run")
            self.assertEqual(summary["webhook_env"], "AGENTTEAM_FEISHU_DIAG_WEBHOOK")
            self.assertTrue(summary["webhook_env_set"])
            self.assertTrue(summary["signing_enabled"])
            self.assertEqual(
                [item["variant"] for item in summary["delivery_variants"]],
                ["rich_text", "concise_text"],
            )
            self.assertEqual(
                [item["notification_status"] for item in summary["delivery_variants"]],
                ["dry_run", "dry_run"],
            )
            self.assertNotIn("secret-token", completed.stdout)
            self.assertNotIn("demo-secret", completed.stdout)


    def test_supervised_two_phase_scheduler_emits_run_lifecycle_notifications(self):
        class RecordingNotificationSink:
            def __init__(self):
                self.calls = []

            def notify(self, event, context):
                self.calls.append({"event": event, "context": context})
                return {
                    "event_type": "notification_sent",
                    "actor": "agent-notifier",
                    "target_agent_id": None,
                    "idempotency_key": f"notification:{event['event_id']}",
                    "correlation_id": event["correlation_id"],
                    "payload": {
                        "provider": "feishu",
                        "project": "agentteam",
                        "source_event_type": event["event_type"],
                        "source_event_id": event["event_id"],
                        "source_event_sequence": event["sequence"],
                        "notification_status": "sent",
                        "message_summary": event["event_type"],
                    },
                }

        class ImmediateResultWorkerPool:
            def __init__(self):
                self.result_written = False

            def start(self):
                return {"worker_pool_status": "started"}

            def stop(self):
                return {"worker_pool_status": "stopped"}

            def health_check(self):
                return {
                    "pool_status": "running",
                    "workers": [{"worker_agent_id": "agent-repo-map", "worker_status": "running"}],
                }

            def supervise_once(self):
                if self.result_written:
                    return self._supervision_result()
                state_path = output_dir / "state" / "two_phase_scheduler_state.json"
                if not state_path.exists():
                    return self._supervision_result()
                state = json.loads(state_path.read_text(encoding="utf-8"))
                inflight = state.get("inflight_attempts", [])
                if not inflight:
                    return self._supervision_result()
                attempt = inflight[0]
                _append_runtime_result(
                    attempt["outbox_path"],
                    attempt["message_id"],
                    attempt["task_id"],
                    attempt["attempt_id"],
                    attempt["lease_id"],
                    "completed",
                    [],
                )
                self.result_written = True
                return self._supervision_result()

            def _supervision_result(self):
                health = self.health_check()
                return {
                    "supervision_status": health["pool_status"],
                    "restarted_count": 0,
                    "before": health,
                    "restart": {"restarted_count": 0},
                    "after": health,
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_roles(agent_pool_path, [("agent-repo-map", "repo_map_agent")])
            sink = RecordingNotificationSink()
            args = SimpleNamespace(
                agent_pool=str(agent_pool_path),
                backlog=str(backlog_path),
                output_dir=str(output_dir),
                project_root=None,
                max_inflight=1,
                max_attempts=1,
                lease_timeout_seconds=900,
                integrate_accepted_patch=False,
                commit_verified_integration=False,
                auto_decompose_backlog=False,
                decomposition_milestone_id="M21",
                decomposition_planner_role="task_planner",
                decomposition_default_worker_role="repo_map_agent",
                planner_context_artifact=[],
                planner_context_excerpt_chars=1200,
                max_steps=10,
            )

            result = _run_supervised_two_phase_scheduler(
                args,
                integration_verification_command=None,
                worker_pool=ImmediateResultWorkerPool(),
                notification_sink=sink,
            )

            self.assertEqual(result["scheduler_status"], "idle")
            self.assertEqual(
                [call["event"]["event_type"] for call in sink.calls],
                ["run_started", "run_completed"],
            )
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["event_type"] for event in events if event["event_type"].startswith("run_")],
                ["run_started", "run_completed"],
            )
            self.assertEqual(
                [
                    event["payload"]["source_event_type"]
                    for event in events
                    if event["event_type"] == "notification_sent"
                ],
                ["run_started", "run_completed"],
            )

try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class NotificationsMixin:
    def test_terminal_and_feishu_reports_share_canonical_usage_and_label_legacy_totals(self):
        from agentteam_runtime.operator_report import (
            build_run_completion_report,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "full-usage"
            _write_json(
                run_dir / "state" / "two_phase_scheduler_state.json",
                {"scheduler_status": "idle", "steps": []},
            )
            operator_report = {
                "task_count": 1,
                "blocked_count": 0,
                "task_reports": [],
                "token_usage": {
                    "usage_status": "reported",
                    "reported_attempt_count": 1,
                    "unreported_attempt_count": 0,
                    "input_tokens": 900,
                    "output_tokens": 99,
                    "total_tokens": 999,
                },
            }
            terminal_event = {
                "event_id": "EVT-RUN-COMPLETED",
                "event_type": "run_completed",
                "sequence": 100,
                "payload": {
                    "run_status": "completed",
                    "scheduler_status": "idle",
                    "operator_report": operator_report,
                },
            }
            _write_jsonl(
                run_dir / "events.jsonl",
                [*_full_run_model_invocation_events(), terminal_event],
            )

            report = build_run_completion_report(
                run_dir,
                project="usage-project",
                write_files=False,
            )
            markdown = render_run_completion_report(report)
            sent_payloads = []

            def fake_http_post(_url, payload, _timeout_seconds):
                sent_payloads.append(payload)
                return {"status_code": 200, "body": {"code": 0}}

            notifier = FeishuWebhookNotifier(
                webhook_url="https://example.invalid/hook",
                project="usage-project",
                http_post=fake_http_post,
                message_limit=4000,
            )
            notification = notifier.notify_event(
                terminal_event,
                run_dir=str(run_dir),
            )

        self.assertEqual(report["task_count"], 1)
        self.assertEqual(
            report["model_invocation_usage"]["invocation_count"],
            7,
        )
        self.assertEqual(
            report["model_invocation_usage"]["reported_token_totals"][
                "total_tokens"
            ],
            180,
        )
        self.assertEqual(
            report["legacy_task_token_usage"]["usage"]["total_tokens"],
            999,
        )
        self.assertFalse(
            report["legacy_task_token_usage"]["benchmark_counted"]
        )
        self.assertEqual(report["run_outcome"], "blocked_open_invocations")
        self.assertEqual(notification["event_type"], "notification_sent")
        feishu_text = sent_payloads[0]["content"]["text"]
        for line in compact_model_invocation_usage_lines(
            report["model_invocation_usage"]
        ):
            self.assertIn(line, markdown)
            self.assertIn(line, feishu_text)
        self.assertIn(
            "Token usage: total=999 input=900 output=99 reported=1/1 "
            "(legacy task-result aggregate; not benchmark-counted)",
            markdown,
        )
        self.assertIn(
            "Token usage: total=999 input=900 output=99 reported=1/1 "
            "(legacy task-result aggregate; not benchmark-counted)",
            feishu_text,
        )
        self.assertNotIn("total=6999", markdown)


    def test_doctor_inotify_check_warns_and_reports_top_consumer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            process = proc_root / str(os.getpid())
            fdinfo = process / "fdinfo"
            fdinfo.mkdir(parents=True)
            (process / "comm").write_text("fixture-watcher\n", encoding="utf-8")
            (process / "cmdline").write_bytes(
                b"/usr/bin/fixture-watcher\0--watch\0"
            )
            (fdinfo / "7").write_text(
                "pos:\t0\n"
                + "".join(
                    f"inotify wd:{index:x} ino:1 sdev:1 mask:1\n"
                    for index in range(95)
                ),
                encoding="utf-8",
            )
            max_watches_path = root / "max_user_watches"
            max_watches_path.write_text("100\n", encoding="utf-8")

            check = agentteam_module._doctor_inotify_check(
                proc_root=proc_root,
                max_watches_path=max_watches_path,
                uid=os.getuid(),
            )

            self.assertEqual(check["status"], "warning")
            self.assertEqual(check["current_watches"], 95)
            self.assertEqual(check["remaining_watches"], 5)
            self.assertEqual(check["top_consumers"][0]["pid"], os.getpid())
            self.assertEqual(
                check["top_consumers"][0]["executable"],
                "/usr/bin/fixture-watcher",
            )
            self.assertNotIn("--watch", json.dumps(check))


    def test_doctor_inotify_check_passes_with_available_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            proc_root.mkdir()
            max_watches_path = root / "max_user_watches"
            max_watches_path.write_text("100\n", encoding="utf-8")

            check = agentteam_module._doctor_inotify_check(
                proc_root=proc_root,
                max_watches_path=max_watches_path,
                uid=os.getuid(),
            )

            self.assertEqual(check["status"], "passed")
            self.assertEqual(check["usage_ratio"], 0.0)


    @unittest.skipUnless(
        os.environ.get("AGENTTEAM_HOST_TESTS") == "1",
        "requires localhost socket access; run the host test lane",
    )
    def test_agentteam_cli_notify_test_sends_feishu_message_from_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            server, payloads = _start_webhook_capture_server()
            try:
                env = _test_env()
                env["AGENTTEAM_FEISHU_TEST_WEBHOOK"] = (
                    f"http://127.0.0.1:{server.server_port}/hook"
                )
                init_completed = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "agentteam_runtime.agentteam",
                        "init",
                        "--project-root",
                        str(repo),
                        "--project-key",
                        "notify-project",
                        "--work-root",
                        str(work_root),
                        "--author-runtime",
                        "fake",
                        "--runtime",
                        "fake",
                        "--notification-project",
                        "notify-project",
                        "--feishu-webhook-env",
                        "AGENTTEAM_FEISHU_TEST_WEBHOOK",
                    ],
                    env=env,
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
                        "notify",
                        "test",
                        "--project-root",
                        str(repo),
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["notify_status"], "sent")
            self.assertEqual(summary["provider"], "feishu")
            self.assertEqual(summary["project"], "notify-project")
            self.assertEqual(summary["webhook_env"], "AGENTTEAM_FEISHU_TEST_WEBHOOK")
            self.assertFalse(summary["signing_enabled"])
            self.assertEqual(len(payloads), 1)
            message = payloads[0]["content"]["text"]
            self.assertIn("[AgentTeam] run_completed", message)
            self.assertIn("工作摘要:", message)
            self.assertIn("中文工作汇报:", message)
            self.assertIn(
                "Token usage: not applicable (diagnostic notification; no AgentTeam run)",
                message,
            )
            self.assertNotIn("Token usage: unavailable", message)
            self.assertIn("AgentTeam notification test for notify-project.", message)
            self.assertIn("If you receive this message, Feishu notification delivery works.", message)
            self.assertNotIn("Completion summary:", message)
            self.assertNotIn("中文简报:", message)
            self.assertNotIn("What changed:", message)


    @unittest.skipUnless(
        os.environ.get("AGENTTEAM_HOST_TESTS") == "1",
        "requires localhost socket access; run the host test lane",
    )
    def test_agentteam_cli_notify_run_completed_sends_existing_run_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "completed-run"
            _init_repo(repo)
            _write_completed_operator_run(run_dir)
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "worker_count": 1,
                        "pool_diagnostic_status": "attention",
                        "diagnostic_worker_counts": {"processing_stale": 1},
                        "workers": [
                            {
                                "worker_agent_id": "implementation-worker-1",
                                "worker_status": "running",
                                "worker_diagnostic_state": "processing_stale",
                                "last_activity": "processing",
                                "heartbeat_age_seconds": 181,
                            }
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            server, payloads = _start_webhook_capture_server()
            try:
                env = _test_env()
                env["AGENTTEAM_FEISHU_TEST_WEBHOOK"] = (
                    f"http://127.0.0.1:{server.server_port}/hook"
                )
                init_completed = subprocess.run(
                    [
                        "python3",
                        "-m",
                        "agentteam_runtime.agentteam",
                        "init",
                        "--project-root",
                        str(repo),
                        "--project-key",
                        "notify-run-project",
                        "--work-root",
                        str(work_root),
                        "--author-runtime",
                        "fake",
                        "--runtime",
                        "fake",
                        "--notification-project",
                        "notify-run-project",
                        "--feishu-webhook-env",
                        "AGENTTEAM_FEISHU_TEST_WEBHOOK",
                    ],
                    env=env,
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
                        "notify",
                        "run-completed",
                        "--project-root",
                        str(repo),
                        "--taskpack",
                        "completed-run",
                        "--json",
                    ],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["notify_status"], "sent")
            self.assertEqual(summary["event_type"], "run_completed")
            self.assertEqual(summary["taskpack_id"], "completed-run")
            self.assertEqual(len(payloads), 1)
            message = payloads[0]["content"]["text"]
            self.assertIn("[AgentTeam] run_completed", message)
            self.assertIn("工作摘要:", message)
            self.assertIn("中文工作汇报:", message)
            self.assertIn("Token usage: total=1500 input=1200 output=300 reported=1/1", message)
            self.assertIn("Scanned the repository and implemented one evidence-backed optimization.", message)
            self.assertIn("gesture_recognition/sim_eval.py", message)
            self.assertIn("Worker 诊断：pool=attention；processing_stale=1", message)
            self.assertIn("implementation-worker-1 diagnostic=processing_stale", message)
            self.assertNotIn("Completion summary:", message)
            self.assertNotIn("What changed:", message)


    def test_agentteam_cli_notify_diagnose_dry_run_does_not_print_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            _init_repo(repo)
            env = _test_env()
            env["AGENTTEAM_FEISHU_DIAG_WEBHOOK"] = (
                "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"
            )
            env["AGENTTEAM_FEISHU_DIAG_SECRET"] = "demo-secret"
            init_completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "init",
                    "--project-root",
                    str(repo),
                    "--project-key",
                    "notify-diagnose-project",
                    "--work-root",
                    str(work_root),
                    "--author-runtime",
                    "fake",
                    "--runtime",
                    "fake",
                    "--notification-project",
                    "notify-diagnose-project",
                    "--feishu-webhook-env",
                    "AGENTTEAM_FEISHU_DIAG_WEBHOOK",
                    "--feishu-signing-secret-env",
                    "AGENTTEAM_FEISHU_DIAG_SECRET",
                ],
                env=env,
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
                    "notify",
                    "diagnose",
                    "--project-root",
                    str(repo),
                    "--dry-run",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("secret-token", completed.stdout)
            self.assertNotIn("demo-secret", completed.stdout)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["diagnosis_status"], "dry_run")
            self.assertEqual(summary["provider"], "feishu")
            self.assertEqual(summary["project"], "notify-diagnose-project")
            self.assertEqual(summary["webhook_env"], "AGENTTEAM_FEISHU_DIAG_WEBHOOK")
            self.assertTrue(summary["webhook_env_set"])
            self.assertEqual(summary["signing_secret_env"], "AGENTTEAM_FEISHU_DIAG_SECRET")
            self.assertTrue(summary["signing_enabled"])
            self.assertEqual(
                [item["variant"] for item in summary["variants"]],
                ["rich_text", "concise_text"],
            )
            self.assertEqual(
                [item["status"] for item in summary["variants"]],
                ["dry_run", "dry_run"],
            )


    def test_feishu_notification_failure_summary_includes_body_message(self):
        def fake_http_post(_url, _payload, _timeout_seconds):
            return {
                "status_code": 200,
                "body": {
                    "code": 11232,
                    "msg": "security keyword mismatch",
                },
            }

        notifier = FeishuWebhookNotifier(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/redacted-token",
            project="notify-project",
            http_post=fake_http_post,
        )
        result = notifier.notify_event(
            {
                "event_id": "EVT-001",
                "sequence": 1,
                "event_type": "run_completed",
                "payload": {"run_status": "completed"},
            },
            run_dir="/tmp/run",
        )

        payload = result["payload"]
        self.assertEqual(payload["notification_status"], "failed")
        self.assertIn("body_code=11232", payload["error_summary"])
        self.assertIn("body_msg=security keyword mismatch", payload["error_summary"])
        self.assertNotIn("redacted-token", payload["error_summary"])


    def test_agentteam_cli_notify_test_requires_configured_feishu_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
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
                    "missing-notify-project",
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

            completed = subprocess.run(
                [
                    "python3",
                    "-m",
                    "agentteam_runtime.agentteam",
                    "notify",
                    "test",
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

            self.assertEqual(completed.returncode, 1)
            error = json.loads(completed.stderr)
            self.assertEqual(error["error"], "Feishu webhook env is not configured")

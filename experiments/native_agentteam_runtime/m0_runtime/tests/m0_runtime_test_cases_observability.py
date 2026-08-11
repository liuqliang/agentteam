try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class ObservabilityMixin:
    def test_token_usage_unavailable_output_explains_missing_runtime_reports(self):
        from agentteam_runtime.token_usage import aggregate_token_usage, format_token_usage

        usage = aggregate_token_usage([None, None], expected_count=2)

        self.assertEqual(usage["usage_status"], "unavailable")
        self.assertEqual(usage["unavailable_reason"], "missing_runtime_token_usage")
        self.assertEqual(
            format_token_usage(usage),
            "Token usage: unavailable (missing_runtime_token_usage)",
        )


    def test_token_usage_records_source_for_runtime_result_and_codex_jsonl(self):
        from agentteam_runtime.token_usage import (
            aggregate_token_usage,
            format_token_usage,
            token_usage_from_jsonl,
            token_usage_from_result,
        )

        codex_usage = token_usage_from_jsonl(
            json.dumps(
                {
                    "type": "turn_completed",
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                }
            )
        )
        runtime_usage = token_usage_from_result(
            {
                "result_status": "completed",
                "changed_files": [],
                "output": {
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 5,
                        "total_tokens": 16,
                    }
                },
            }
        )
        aggregate = aggregate_token_usage(
            [codex_usage, runtime_usage],
            expected_count=2,
        )

        self.assertEqual(codex_usage["usage_source"], "codex_jsonl")
        self.assertEqual(runtime_usage["usage_source"], "runtime_result")
        self.assertEqual(aggregate["usage_status"], "reported")
        self.assertEqual(aggregate["usage_source"], "mixed")
        self.assertEqual(aggregate["usage_sources"], ["codex_jsonl", "runtime_result"])
        self.assertEqual(aggregate["total_tokens"], 26)
        self.assertIn("source=mixed", format_token_usage(aggregate))


    def test_token_usage_preserves_not_applicable_diagnostic_semantics(self):
        from agentteam_runtime.token_usage import aggregate_token_usage, format_token_usage

        usage = aggregate_token_usage(
            [
                {
                    "usage_status": "not_applicable",
                    "reason": "diagnostic_notification",
                }
            ],
            expected_count=1,
        )

        self.assertEqual(usage["usage_status"], "not_applicable")
        self.assertEqual(usage["reason"], "diagnostic_notification")
        self.assertEqual(
            format_token_usage(usage),
            "Token usage: not applicable (diagnostic_notification)",
        )


    def test_run_completion_report_surfaces_worker_heartbeat_diagnostics(self):
        from agentteam_runtime.operator_report import (
            build_run_completion_report,
            concise_report_lines,
            render_run_completion_report,
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "RUN-WORKER-DIAGNOSTICS"
            (run_dir / "state").mkdir(parents=True)
            event = {
                "event_type": "run_completed",
                "payload": {
                    "run_status": "completed",
                    "operator_report": {
                        "report_schema_version": "operator_run_report.v1",
                        "task_count": 0,
                        "blocked_count": 0,
                        "task_reports": [],
                    },
                },
            }
            registry = {
                "registry_status": "running",
                "worker_count": 1,
                "pool_diagnostic_status": "attention",
                "diagnostic_worker_counts": {"processing_stale": 1},
                "workers": [
                    {
                        "worker_agent_id": "agent-implementation-worker-1",
                        "worker_status": "running",
                        "worker_diagnostic_state": "processing_stale",
                        "last_activity": "processing",
                        "last_poll_status": "processing",
                        "heartbeat_age_seconds": 181,
                        "heartbeat_stale_after_seconds": 120,
                        "heartbeat_task_id": "TASK-001",
                        "heartbeat_progress_summary": (
                            "processing TASK-001 attempt=ATTEMPT-001: "
                            "Run long Codex worker task"
                        ),
                        "heartbeat_path": str(
                            run_dir
                            / "state"
                            / "workers"
                            / "agent-implementation-worker-1.heartbeat.json"
                        ),
                    }
                ],
            }
            (run_dir / "events.jsonl").write_text(
                json.dumps(event, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (run_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(registry, sort_keys=True),
                encoding="utf-8",
            )

            report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

        markdown = render_run_completion_report(report)
        concise = "\n".join(concise_report_lines(report))

        self.assertEqual(
            report["worker_diagnostics"]["pool_diagnostic_status"],
            "attention",
        )
        self.assertEqual(
            report["worker_diagnostics"]["workers"][0]["worker_diagnostic_state"],
            "processing_stale",
        )
        self.assertIn("## Worker Diagnostics", markdown)
        self.assertIn("agent-implementation-worker-1", markdown)
        self.assertIn("diagnostic=processing_stale", markdown)
        self.assertIn("heartbeat_age_seconds=181", markdown)
        self.assertIn(
            "progress=processing TASK-001 attempt=ATTEMPT-001: Run long Codex worker task",
            markdown,
        )
        self.assertIn("worker_diagnostics: pool=attention", concise)
        self.assertIn(
            "worker agent-implementation-worker-1: diagnostic=processing_stale",
            concise,
        )
        self.assertIn(
            "progress=processing TASK-001 attempt=ATTEMPT-001: Run long Codex worker task",
            concise,
        )


    def test_file_mailbox_worker_poll_once_writes_token_usage_to_outbox(self):
        class TokenRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": [],
                    "output": {"summary": "done"},
                    "token_usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                        "cached_input_tokens": 4,
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-TOKENS-001",
                agent_id="agent-repo-map",
                write_scope=[],
            )
            _append_test_jsonl(inbox, [message])

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=TokenRuntimeAdapter(),
                clock=FixedClock(),
            )
            worker.poll_once()

            result_message = _read_first_jsonl(outbox)

            self.assertEqual(
                result_message["payload"].get("token_usage"),
                {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "cached_input_tokens": 4,
                },
            )


    def test_file_mailbox_worker_poll_once_writes_activity_heartbeat(self):
        class InspectHeartbeatRuntimeAdapter:
            def __init__(self, heartbeat_path):
                self.heartbeat_path = heartbeat_path
                self.processing_heartbeat = None

            def run(self, message, worktree_path=None):
                del message, worktree_path
                self.processing_heartbeat = json.loads(
                    self.heartbeat_path.read_text(encoding="utf-8")
                )
                return {
                    "result_status": "completed",
                    "changed_files": ["generated/m0_generated_repo_index.json"],
                    "output": {"summary": "done"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            heartbeat_path = (
                output_dir / "state" / "workers" / "agent-repo-map.heartbeat.json"
            )
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-HEARTBEAT-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])
            runtime_adapter = InspectHeartbeatRuntimeAdapter(heartbeat_path)

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=runtime_adapter,
                clock=FixedClock(),
            )
            summary = worker.poll_once()

            final_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(
                runtime_adapter.processing_heartbeat["activity"],
                "processing",
            )
            self.assertEqual(
                runtime_adapter.processing_heartbeat["source_message_id"],
                "MSG-MAILBOX-HEARTBEAT-001",
            )
            self.assertEqual(
                runtime_adapter.processing_heartbeat["progress_summary"],
                (
                    "processing TASK-MAILBOX attempt=ATTEMPT-MAILBOX-001: "
                    "Exercise file mailbox worker runtime."
                ),
            )
            self.assertEqual(final_heartbeat["activity"], "processed")
            self.assertEqual(final_heartbeat["result_status"], "completed")
            self.assertEqual(final_heartbeat["changed_file_count"], 1)
            self.assertEqual(
                final_heartbeat["progress_summary"],
                "processed TASK-MAILBOX result=completed changed_files=1",
            )


    def test_file_mailbox_worker_runtime_progress_callback_refreshes_processing_heartbeat(self):
        class ProgressHeartbeatRuntimeAdapter:
            def __init__(self, heartbeat_path):
                self.heartbeat_path = heartbeat_path
                self.before_callback = None
                self.after_callback = None

            def run(self, message, worktree_path=None, progress_callback=None):
                del message, worktree_path
                if progress_callback is None:
                    raise AssertionError("missing progress callback")
                self.before_callback = json.loads(
                    self.heartbeat_path.read_text(encoding="utf-8")
                )
                progress_callback()
                self.after_callback = json.loads(
                    self.heartbeat_path.read_text(encoding="utf-8")
                )
                return {
                    "result_status": "completed",
                    "changed_files": [],
                    "output": {"summary": "done"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            heartbeat_path = (
                output_dir / "state" / "workers" / "agent-repo-map.heartbeat.json"
            )
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-HEARTBEAT-PROGRESS-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])
            runtime_adapter = ProgressHeartbeatRuntimeAdapter(heartbeat_path)

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=runtime_adapter,
                clock=FixedClock(),
            )
            summary = worker.poll_once()

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(runtime_adapter.before_callback["activity"], "processing")
            self.assertEqual(runtime_adapter.after_callback["activity"], "processing")
            self.assertNotEqual(
                runtime_adapter.after_callback["updated_at"],
                runtime_adapter.before_callback["updated_at"],
            )
            self.assertEqual(
                runtime_adapter.after_callback["source_message_id"],
                "MSG-MAILBOX-HEARTBEAT-PROGRESS-001",
            )


    def test_file_mailbox_worker_throttles_idle_heartbeat_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            heartbeat_path = (
                output_dir / "state" / "workers" / "agent-repo-map.heartbeat.json"
            )
            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=FakeRuntimeAdapter(),
                clock=FixedClock(),
            )

            worker.poll_once()
            first_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            worker.poll_once()
            second_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            old_mtime = time.time() - 30
            os.utime(heartbeat_path, (old_mtime, old_mtime))
            worker.poll_once()
            third_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))

            self.assertEqual(first_heartbeat, second_heartbeat)
            self.assertEqual(third_heartbeat["activity"], "idle")
            self.assertNotEqual(third_heartbeat["updated_at"], first_heartbeat["updated_at"])


    def test_heartbeat_json_writer_uses_atomic_replace(self):
        from unittest import mock

        from agentteam_runtime.mailbox_worker import _write_json_best_effort

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.heartbeat.json"
            calls = []
            original_replace = os.replace

            def tracking_replace(source, target):
                calls.append((Path(source), Path(target)))
                original_replace(source, target)

            with mock.patch(
                "agentteam_runtime.mailbox_worker.os.replace",
                side_effect=tracking_replace,
            ):
                _write_json_best_effort(path, {"activity": "idle"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"activity": "idle"})
            self.assertEqual(calls[0][1], path)
            self.assertNotEqual(calls[0][0], path)
            self.assertFalse(calls[0][0].exists())


    def test_mailbox_worker_outbox_reader_preserves_token_usage(self):
        from agentteam_runtime.mailbox_worker import _runtime_result_from_outbox

        with tempfile.TemporaryDirectory() as tmp:
            outbox = Path(tmp) / "outbox.jsonl"
            usage = {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "cached_input_tokens": 4,
            }
            _append_test_jsonl(
                outbox,
                [
                    {
                        "message_type": "runtime_result",
                        "payload": {
                            "source_message_id": "MSG-MAILBOX-TOKENS-001",
                            "result_status": "completed",
                            "changed_files": [],
                            "output": {"summary": "done"},
                            "token_usage": usage,
                        },
                    }
                ],
            )

            result = _runtime_result_from_outbox(
                outbox,
                "MSG-MAILBOX-TOKENS-001",
            )

            self.assertEqual(result.get("token_usage"), usage)


    def test_file_mailbox_worker_pool_health_includes_worker_heartbeat(self):
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

            pool.start()
            heartbeat_path = (
                output_dir / "state" / "workers" / "agent-repo-map.heartbeat.json"
            )
            deadline = time.monotonic() + 2
            try:
                while not heartbeat_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                health = pool.health_check()
            finally:
                pool.stop()

            self.assertTrue(heartbeat_path.exists())
            worker = health["workers"][0]
            self.assertEqual(worker["worker_status"], "running")
            self.assertEqual(worker["last_activity"], "idle")
            self.assertEqual(worker["last_poll_status"], "idle")
            self.assertEqual(worker["heartbeat_path"], str(heartbeat_path))
            self.assertEqual(worker["worker_diagnostic_state"], "idle")
            self.assertEqual(health["pool_diagnostic_status"], "healthy")


    def test_file_mailbox_worker_pool_health_classifies_missing_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                heartbeat_stale_seconds=10,
            )
            worker = FileMailboxWorkerProcessSupervisor(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
            )
            worker.attach_existing_process(os.getpid())
            pool.workers = [worker]

            health = pool.health_check()

            self.assertEqual(health["pool_status"], "running")
            self.assertEqual(health["pool_diagnostic_status"], "attention")
            self.assertEqual(health["diagnostic_worker_counts"]["no_heartbeat"], 1)
            self.assertEqual(
                health["workers"][0]["worker_diagnostic_state"],
                "no_heartbeat",
            )


    def test_file_mailbox_worker_pool_health_classifies_normal_heartbeat_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            heartbeat_path = (
                output_dir / "state" / "workers" / "agent-repo-map.heartbeat.json"
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                heartbeat_stale_seconds=10,
            )
            worker = FileMailboxWorkerProcessSupervisor(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
            )
            worker.attach_existing_process(os.getpid())
            pool.workers = [worker]

            for activity in ["idle", "processing", "processed"]:
                heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
                heartbeat_path.write_text(
                    json.dumps(
                        {
                            "activity": activity,
                            "poll_status": activity,
                            "updated_at": "2026-06-15T00:00:00Z",
                            "worker_agent_id": "agent-repo-map",
                            "worker_pid": os.getpid(),
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                now = time.time()
                os.utime(heartbeat_path, (now, now))

                health = pool.health_check()

                self.assertEqual(
                    health["workers"][0]["worker_diagnostic_state"],
                    activity,
                )
                self.assertEqual(health["pool_diagnostic_status"], "healthy")


    def test_file_mailbox_worker_pool_health_classifies_processing_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            heartbeat_path = (
                output_dir / "state" / "workers" / "agent-repo-map.heartbeat.json"
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            heartbeat_path.write_text(
                json.dumps(
                    {
                        "activity": "processing",
                        "poll_status": "processing",
                        "updated_at": "2026-06-15T00:00:00Z",
                        "worker_agent_id": "agent-repo-map",
                        "worker_pid": os.getpid(),
                        "task_id": "TASK-001",
                        "progress_summary": (
                            "processing TASK-001 attempt=ATTEMPT-001: "
                            "Run long Codex worker task"
                        ),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            old_mtime = time.time() - 30
            os.utime(heartbeat_path, (old_mtime, old_mtime))
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                heartbeat_stale_seconds=10,
            )
            worker = FileMailboxWorkerProcessSupervisor(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
            )
            worker.attach_existing_process(os.getpid())
            pool.workers = [worker]

            health = pool.health_check()
            registry = json.loads(
                Path(health["process_registry_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(health["pool_status"], "running")
            self.assertEqual(health["pool_diagnostic_status"], "attention")
            self.assertEqual(
                health["workers"][0]["worker_diagnostic_state"],
                "processing_stale",
            )
            self.assertTrue(health["workers"][0]["heartbeat_is_stale"])
            self.assertGreaterEqual(health["workers"][0]["heartbeat_age_seconds"], 10)
            self.assertEqual(
                health["workers"][0]["heartbeat_progress_summary"],
                "processing TASK-001 attempt=ATTEMPT-001: Run long Codex worker task",
            )
            self.assertEqual(
                registry["workers"][0]["worker_diagnostic_state"],
                "processing_stale",
            )
            self.assertEqual(
                registry["workers"][0]["heartbeat_progress_summary"],
                "processing TASK-001 attempt=ATTEMPT-001: Run long Codex worker task",
            )
            self.assertEqual(registry["diagnostic_worker_counts"]["processing_stale"], 1)


    def test_status_last_worker_includes_heartbeat_activity(self):
        from agentteam_runtime.agentteam import _status_last_worker

        line = _status_last_worker(
            {
                "workers": [
                    {
                        "worker_agent_id": "agent-implementation-worker-1",
                        "worker_status": "running",
                        "worker_diagnostic_state": "processing_stale",
                        "last_activity": "processing",
                        "last_poll_status": "processing",
                        "heartbeat_task_id": "TASK-001",
                        "heartbeat_result_status": "completed",
                        "heartbeat_progress_summary": (
                            "processing TASK-001 attempt=ATTEMPT-001: "
                            "Run long Codex worker task"
                        ),
                    }
                ]
            }
        )

        self.assertIn("agent-implementation-worker-1 running", line)
        self.assertIn("diagnostic=processing_stale", line)
        self.assertIn("activity=processing", line)
        self.assertIn("poll=processing", line)
        self.assertIn("task=TASK-001", line)
        self.assertIn("result=completed", line)
        self.assertIn(
            "progress=processing TASK-001 attempt=ATTEMPT-001: Run long Codex worker task",
            line,
        )


    def test_codex_runtime_adapter_collects_token_usage_from_json_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_usage.py"
            _init_git_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import sys",
                        "args = sys.argv[1:]",
                        "sys.stdin.read()",
                        "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                        "worktree = pathlib.Path(args[args.index('-C') + 1])",
                        "target = worktree / 'generated' / 'codex_usage_result.json'",
                        "target.parent.mkdir(parents=True, exist_ok=True)",
                        "target.write_text('{}', encoding='utf-8')",
                        "output_path.parent.mkdir(parents=True, exist_ok=True)",
                        "output_path.write_text(json.dumps({",
                        "    'result_status': 'completed',",
                        "    'changed_files': ['generated/codex_usage_result.json'],",
                        "    'output': {'adapter': 'codex'},",
                        "}), encoding='utf-8')",
                        "print(json.dumps({'type': 'session_started'}))",
                        "print(json.dumps({",
                        "    'type': 'turn_completed',",
                        "    'usage': {",
                        "        'input_tokens': 1234,",
                        "        'output_tokens': 321,",
                        "        'total_tokens': 1555,",
                        "        'cached_input_tokens': 100,",
                        "        'reasoning_tokens': 77,",
                        "    },",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            message = {
                "payload": {
                    "task_id": "TASK-001",
                    "attempt_id": "ATTEMPT-001",
                    "objective": "Collect Codex usage.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                }
            }

            result = CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]).run(
                message,
                worktree_path=repo,
            )

            self.assertEqual(result["result_status"], "completed")
            self.assertEqual(
                result["token_usage"],
                {
                    "input_tokens": 1234,
                    "output_tokens": 321,
                    "total_tokens": 1555,
                    "cached_input_tokens": 100,
                    "reasoning_tokens": 77,
                    "usage_source": "codex_jsonl",
                },
            )

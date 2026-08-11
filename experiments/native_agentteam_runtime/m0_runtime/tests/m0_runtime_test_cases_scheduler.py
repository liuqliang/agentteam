try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class SchedulerMixin:
    def test_two_phase_scheduler_state_write_preserves_previous_snapshot_on_replace_failure(self):
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
            state_path = output_dir / "state" / "two_phase_scheduler_state.json"
            scheduler._write_state()
            previous = state_path.read_bytes()
            scheduler.state["atomic_write_probe"] = "new-value"

            with mock.patch(
                "agentteam_runtime.two_phase_scheduler.os.replace",
                side_effect=OSError("simulated interrupted replacement"),
            ):
                with self.assertRaisesRegex(OSError, "interrupted replacement"):
                    scheduler._write_state()

            self.assertEqual(state_path.read_bytes(), previous)
            self.assertNotIn("atomic_write_probe", json.loads(previous))
            self.assertEqual(
                list(state_path.parent.glob(f".{state_path.name}.tmp-*")),
                [],
            )

            scheduler._write_state()
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "atomic_write_probe"
                ],
                "new-value",
            )

    def test_two_phase_scheduler_notifies_manual_gate_after_canonical_event(self):
        class RecordingNotificationSink:
            def __init__(self):
                self.calls = []

            def notify(self, event, context):
                self.calls.append({"event": event, "context": context})
                return [
                    {
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
                            "message_summary": (
                                "manual gate "
                                + event["payload"]["question_id"]
                                + " for "
                                + event["payload"]["task_id"]
                            ),
                        },
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            sink = RecordingNotificationSink()
            scheduler = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
                notification_sink=sink,
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
                        "question": "Should runtime implement Feishu notification first?",
                        "reason": "Worker needs operator direction.",
                    }
                },
            )

            scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            manual_gate = [
                event for event in events if event["event_type"] == "manual_gate_required"
            ][0]
            notification = [
                event for event in events if event["event_type"] == "notification_sent"
            ][0]

            self.assertEqual(len(sink.calls), 1)
            self.assertEqual(sink.calls[0]["event"]["event_id"], manual_gate["event_id"])
            self.assertEqual(sink.calls[0]["event"]["sequence"], manual_gate["sequence"])
            self.assertEqual(sink.calls[0]["context"]["run_dir"], str(output_dir))
            self.assertGreater(notification["sequence"], manual_gate["sequence"])
            self.assertEqual(
                notification["payload"]["source_event_id"],
                manual_gate["event_id"],
            )


    def test_two_phase_scheduler_notifies_allowed_run_level_event(self):
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
                        "message_summary": "run_completed completed",
                    },
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            sink = RecordingNotificationSink()
            scheduler = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
                notification_sink=sink,
            )
            canonical_event = {
                **scheduler._event(
                    "run_completed",
                    "agent-scheduler",
                    None,
                    "run:completed",
                    "run:completed",
                    {"run_status": "completed"},
                ),
                "event_id": "EVT-0099",
                "sequence": 99,
                "run_id": scheduler.run_id,
                "step_id": "STEP-RUN",
            }

            scheduler._notify_canonical_events("STEP-RUN", [canonical_event])

            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            notification = [
                event for event in events if event["event_type"] == "notification_sent"
            ][0]
            self.assertEqual(len(sink.calls), 1)
            self.assertEqual(sink.calls[0]["event"]["event_type"], "run_completed")
            self.assertEqual(notification["payload"]["source_event_type"], "run_completed")


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


    def test_run_simulation_dispatch_preserves_task_artifact_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            handoff_path = ".agentteam/generated/repo_map_handoff.json"
            task = _backlog_task("TASK-ARTIFACTS", write_scope=[".agentteam/generated/"])
            task["input_artifacts"] = [handoff_path]
            task["expected_output_artifacts"] = [handoff_path]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=[".agentteam/generated/"],
                tasks=[task],
            )

            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
            )

            message = _read_first_jsonl(
                output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            )

            self.assertEqual(message["payload"]["input_artifacts"], [handoff_path])
            self.assertEqual(message["payload"]["expected_output_artifacts"], [handoff_path])


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


    def test_run_simulation_role_context_can_reference_repo_map_without_embedding_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            (repo / "pkg").mkdir()
            (repo / "pkg" / "module.py").write_text(
                "SECRET_SOURCE_BODY = 'do-not-embed'\n\n"
                "def build_worker():\n"
                "    return SECRET_SOURCE_BODY\n",
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
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_role_context_packages(
                agent_pool_path,
                role_context_packages={
                    "repo_map_agent": {
                        "context_notes": ["Read repo map references only as navigation."],
                        "include_repo_map_references": True,
                    }
                },
            )

            run_simulation(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            message = _read_first_jsonl(
                output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            )
            context_path = Path(message["payload"]["role_context_path"])
            context = json.loads(context_path.read_text(encoding="utf-8"))
            repo_map_reference = context["repo_map_reference"]

            self.assertTrue(Path(repo_map_reference["manifest_path"]).exists())
            self.assertTrue(Path(repo_map_reference["inventory_path"]).exists())
            self.assertTrue(Path(repo_map_reference["symbols_path"]).exists())
            self.assertEqual(
                repo_map_reference["boundary"],
                "navigation_reference_only",
            )
            self.assertIn("repo_context_path", repo_map_reference["read_policy"])
            self.assertNotIn("SECRET_SOURCE_BODY", json.dumps(context))
            self.assertNotIn("do-not-embed", json.dumps(context))


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
                        "runtime_profile_source": "explicit_runtime_adapter",
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
                        "runtime_profile_source": "explicit_runtime_adapter",
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
            self.assertEqual(session["runtime_profile_source"], "explicit_runtime_adapter")
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
            self.assertEqual(session["runtime_profile_source"], "agent_runtime_profile")
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
            self.assertEqual(session["runtime_profile_source"], "role_runtime_profile")
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


    def test_two_phase_scheduler_dispatch_includes_evidence_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            task = _backlog_task("TASK-001", write_scope=["generated/"])
            task["risk_target"] = "L2"
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
            policy = message["payload"]["evidence_policy"]

            self.assertEqual(policy["evidence_level"], "L2")
            self.assertTrue(policy["integration_requires_complete_evidence"])
            self.assertEqual(policy["required_result_key"], "evidence_summary")
            self.assertIn("trace_carrier", policy["required_fields"])
            self.assertIn("missing_evidence", policy["required_fields"])


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


    def test_two_phase_scheduler_records_l3_semantic_escalation_from_planner_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-arch", "semantic_architecture_agent"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                auto_decompose=True,
                decomposition_milestone_id="M39",
                decomposition_allowed_write_scopes=["design/"],
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                "DECOMPOSE-M39-001",
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
                {
                    "task_proposal": {
                        "milestone_id": "M39",
                        "tasks": [
                            {
                                "task_id": "TASK-M39-ARCH-001",
                                "objective": "Resolve a semantic architecture ambiguity.",
                                "read_scope": ["design/"],
                                "write_scope": ["design/architecture.md"],
                                "required_role": "semantic_architecture_agent",
                                "risk_target": "L3",
                                "depends_on": [],
                                "blockers": [],
                            }
                        ],
                    }
                },
            )

            collected = scheduler.collect_ready_results()
            next_dispatch = scheduler.dispatch_ready()
            generated_task = scheduler._task_by_id("TASK-M39-ARCH-001")
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            escalation_events = [
                event for event in events if event["event_type"] == "semantic_escalation_required"
            ]

            self.assertEqual(collected["results"][0]["decomposition_status"], "applied")
            self.assertEqual(next_dispatch["dispatch_status"], "dispatched")
            self.assertEqual(next_dispatch["dispatched_task_ids"], ["TASK-M39-ARCH-001"])
            self.assertEqual(generated_task["backlog_status"], "ready")
            self.assertEqual(generated_task["blockers"], [])
            self.assertEqual(generated_task["required_role"], "semantic_architecture_agent")
            self.assertEqual(generated_task["recommended_role"], "semantic_architecture_agent")
            self.assertEqual(len(escalation_events), 1)
            self.assertEqual(
                escalation_events[0]["payload"],
                {
                    "task_id": "TASK-M39-ARCH-001",
                    "source_task_id": "DECOMPOSE-M39-001",
                    "risk_target": "L3",
                    "reason": "semantic_escalation_required",
                    "recommended_role": "semantic_architecture_agent",
                    "semantic_escalation_status": "required",
                },
            )


    def test_two_phase_scheduler_opens_manual_gate_for_unresolved_l3_semantic_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            task = _backlog_task(
                "TASK-M39-ARCH-001",
                write_scope=[],
                required_role="semantic_architecture_agent",
            )
            task["risk_target"] = "L3"
            task["semantic_escalation_status"] = "required"
            task["recommended_role"] = "semantic_architecture_agent"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=[],
                tasks=[task],
            )
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [("agent-arch", "semantic_architecture_agent")],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )

            dispatch = scheduler.dispatch_ready()
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
                        "question": "Which semantic architecture route should be authoritative?",
                        "options": ["route-a", "route-b"],
                        "reason": "semantic_architecture_agent could not resolve the decision.",
                    },
                    "evidence_summary": {
                        "evidence_level": "L3",
                        "evidence_status": "blocked",
                        "trace_carrier": [
                            {
                                "type": "semantic_proposal",
                                "path": "events.jsonl",
                            }
                        ],
                        "missing_evidence": ["operator_decision"],
                    },
                },
            )

            collect = scheduler.collect_ready_results()
            snapshot = replay_events(output_dir / "events.jsonl")
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            manual_gate = [
                event
                for event in events
                if event["event_type"] == "manual_gate_required"
            ][0]

            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-M39-ARCH-001"])
            self.assertEqual(collect["results"][0]["validation_status"], "rejected")
            self.assertEqual(
                manual_gate["payload"]["question_id"],
                "Q-TASK-M39-ARCH-001-ATTEMPT-001",
            )
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-M39-ARCH-001-ATTEMPT-001"]["gate_status"],
                "waiting",
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
            backlog_event = next(
                event
                for event in events
                if event["event_type"] == "backlog_updated"
            )
            indexed_state = read_scheduler_state_index(output_dir)

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
            self.assertEqual(
                backlog_event["payload"]["task_status"],
                "blocked",
            )
            self.assertEqual(
                indexed_state["tasks"],
                [
                    {
                        "task_id": "DECOMPOSE-M25-001",
                        "task_status": "blocked",
                    }
                ],
            )


    def test_verified_dependency_dispatch_failed_verification_keeps_dependency_unsatisfied(self):
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
                    _backlog_task("TASK-SOURCE", write_scope=["generated/"]),
                    _backlog_task(
                        "TASK-DEPENDENT",
                        write_scope=["generated/"],
                        depends_on=["TASK-SOURCE"],
                    ),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                max_inflight=1,
                max_attempts=2,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree = Path(inflight["worktree_path"])
            target = worktree / "generated" / "failed-verification.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"verified": False}), encoding="utf-8")
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/failed-verification.json"],
            )

            result = scheduler.collect_ready_results()["results"][0]
            next_dispatch = scheduler.dispatch_ready()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            status_by_id = {
                task["task_id"]: task["backlog_status"]
                for task in scheduler.state["backlog"]["items"]
            }

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["task_status"], "ready")
            self.assertEqual(
                result["failure_category"],
                "integration_verification_failed",
            )
            self.assertTrue(result["retry_allowed"])
            self.assertEqual(status_by_id["TASK-SOURCE"], "ready")
            self.assertEqual(status_by_id["TASK-DEPENDENT"], "ready")
            self.assertEqual(next_dispatch["dispatched_task_ids"], ["TASK-SOURCE"])
            self.assertEqual(
                _git_rev_parse(repo, "agentteam/run/run/integration"),
                source_head,
            )
            self.assertFalse(
                any(
                    event["event_type"] == "backlog_updated"
                    and event["payload"].get("task_status") == "done"
                    for event in events
                )
            )
            recovery_events = [
                event for event in events if event["event_type"] == "recovery_routed"
            ]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(
                recovery_events[0]["payload"]["failure_category"],
                "integration_verification_failed",
            )


    def test_verified_dependency_dispatch_apply_failure_preserves_baseline_and_dependency(self):
        from unittest import mock

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
                    _backlog_task("TASK-SOURCE", write_scope=["generated/"]),
                    _backlog_task(
                        "TASK-DEPENDENT",
                        write_scope=["generated/"],
                        depends_on=["TASK-SOURCE"],
                    ),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                max_inflight=1,
                max_attempts=1,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(0)",
                ],
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            target = Path(inflight["worktree_path"]) / "generated" / "apply.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ok": True}), encoding="utf-8")
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/apply.json"],
            )

            with mock.patch(
                (
                    "agentteam_runtime.two_phase_scheduler."
                    "apply_patch_to_integration_baseline_worktree"
                ),
                side_effect=RuntimeError("simulated apply failure"),
            ):
                result = scheduler.collect_ready_results()["results"][0]
            dispatch = scheduler.dispatch_ready()
            status_by_id = {
                task["task_id"]: task["backlog_status"]
                for task in scheduler.state["backlog"]["items"]
            }

            self.assertEqual(result["integration_status"], "failed")
            self.assertEqual(
                result["failure_category"],
                "integration_apply_failed",
            )
            self.assertEqual(result["task_status"], "blocked")
            self.assertFalse(result["retry_allowed"])
            self.assertEqual(status_by_id["TASK-SOURCE"], "blocked")
            self.assertEqual(status_by_id["TASK-DEPENDENT"], "ready")
            self.assertEqual(dispatch["dispatch_count"], 0)
            self.assertEqual(
                _git_rev_parse(repo, "agentteam/run/run/integration"),
                source_head,
            )
            self.assertEqual(
                _git_rev_parse(output_dir / "integration-baseline", "HEAD"),
                source_head,
            )
            self.assertEqual(
                result["integration_baseline_commit_reason"],
                "apply_failed",
            )


    def test_verified_dependency_dispatch_no_patch_uses_verified_noop_policy(self):
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
                tasks=[_backlog_task("TASK-DOCS", write_scope=["generated/"])],
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
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(9)",
                ],
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                [],
            )

            result = scheduler.collect_ready_results()["results"][0]
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["task_status"], "done")
            self.assertEqual(result["completion_policy"], "verified_noop")
            self.assertEqual(result["integration_status"], "verified_noop")
            self.assertEqual(result["integration_noop_status"], "verified")
            self.assertEqual(
                result["integration_baseline_commit_status"],
                "unchanged",
            )
            self.assertEqual(
                result["verified_integration_head_sha"],
                source_head,
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in events
                    if event["event_type"] == "integration_noop_verified"
                ],
                ["integration_noop_verified"],
            )


    def test_file_scheduler_materializes_runtime_artifact_between_steps(self):
        class InspectingFakeRuntime(FakeRuntimeAdapter):
            def __init__(self, artifact_path):
                self.artifact_path = artifact_path
                self.consumer_observed_artifact = False

            def run(self, message, worktree_path=None):
                if message["payload"]["task_id"] == "TASK-REPO-MAP":
                    target = Path(worktree_path) / self.artifact_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('{"repo_map":"ready"}\n', encoding="utf-8")
                    return {
                        "result_status": "completed",
                        "changed_files": [self.artifact_path],
                        "output": {"adapter": "artifact-only"},
                    }
                if message["payload"]["task_id"] == "TASK-IMPLEMENT":
                    self.consumer_observed_artifact = (
                        Path(worktree_path) / self.artifact_path
                    ).is_file()
                return super().run(message, worktree_path=worktree_path)

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
                write_scope=["generated/"],
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
            runtime = InspectingFakeRuntime(artifact_path)
            scheduler = FileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=runtime,
            )

            result = scheduler.run_until_idle(max_steps=3)

            self.assertEqual(result["scheduler_status"], "idle")
            self.assertTrue(runtime.consumer_observed_artifact)
            self.assertTrue(
                (output_dir / "runtime_artifacts" / "manifest.json").is_file()
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

                collected_task_ids = []
                for _ in range(250):
                    collected = scheduler.collect_ready_results()
                    collected_task_ids.extend(collected["collected_task_ids"])
                    if set(collected_task_ids) == {"TASK-001", "TASK-002"}:
                        break
                    time.sleep(0.02)
            finally:
                pool.stop()

            state = read_scheduler_state_index(output_dir)
            self.assertEqual(set(collected_task_ids), {"TASK-001", "TASK-002"})
            self.assertEqual(
                set(scheduler.summary()["processed_task_ids"]),
                {"TASK-001", "TASK-002"},
            )
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done", "TASK-002": "done"},
            )


    def test_stop_run_marker_prevents_active_two_phase_tick_from_restarting_run(self):
        from agentteam_runtime.operator_control import stop_run

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_inflight=1,
            )
            dispatch = scheduler.dispatch_ready()
            registry_path = output_dir / "state" / "worker_process_registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "agent-repo-map",
                                "worker_status": "running",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stop = stop_run(output_dir, force=True, operator="test-operator")
            tick = scheduler.tick()
            state = json.loads(scheduler.state_path.read_text(encoding="utf-8"))
            stop_request = json.loads(
                (output_dir / "state" / "run_stop_request.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(dispatch["inflight_count"], 1)
            self.assertEqual(stop["stop_status"], "stopped")
            self.assertEqual(stop_request["stop_status"], "stopped")
            self.assertEqual(stop_request["stop_operator"], "test-operator")
            self.assertEqual(tick["tick_status"], "stopped")
            self.assertEqual(tick["inflight_count"], 0)
            self.assertEqual(tick["inactive_inflight_count"], 1)
            self.assertEqual(state["scheduler_status"], "stopped")
            self.assertEqual(state["stop_operator"], "test-operator")
            self.assertEqual(len(state["inflight_attempts"]), 1)


    def test_supervised_two_phase_scheduler_honors_stop_request_before_supervising_pool(self):
        from agentteam_runtime.operator_control import stop_run

        class RestartingWorkerPool:
            def __init__(self):
                self.supervise_calls = 0

            def supervise_once(self):
                self.supervise_calls += 1
                return {
                    "supervision_status": "running",
                    "restarted_count": 1,
                    "before": self.health_check(),
                    "restart": {"restarted_count": 1},
                    "after": self.health_check(),
                }

            def health_check(self):
                return {
                    "pool_status": "degraded",
                    "workers": [
                        {
                            "worker_agent_id": "agent-repo-map",
                            "worker_status": "exited",
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[
                    _backlog_task("TASK-001", write_scope=["generated/task-001/"]),
                ],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_inflight=1,
            )
            scheduler.state["scheduler_status"] = "running"
            scheduler._write_state()
            (output_dir / "state" / "worker_process_registry.json").write_text(
                json.dumps(
                    {
                        "registry_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "agent-repo-map",
                                "worker_status": "running",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stop_run(output_dir, force=True, operator="test-operator")
            worker_pool = RestartingWorkerPool()
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
                max_steps=3,
            )

            result = _run_supervised_two_phase_scheduler(
                args,
                integration_verification_command=None,
                worker_pool=worker_pool,
                notification_sink=None,
            )

            self.assertEqual(result["scheduler_status"], "stopped")
            self.assertEqual(result["tick_count"], 0)
            self.assertEqual(result["inflight_count"], 0)
            self.assertEqual(worker_pool.supervise_calls, 0)


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


    def test_two_phase_scheduler_records_manual_gate_for_blocked_result(self):
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

            dispatch = scheduler.dispatch_ready()
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
                        "question": "Should runtime implement Feishu notification first?",
                        "options": ["yes", "no"],
                        "reason": "Worker needs operator direction.",
                    }
                },
            )

            collect = scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            manual_gate = [
                event for event in events if event["event_type"] == "manual_gate_required"
            ][0]
            state_task = scheduler.state["backlog"]["items"][0]
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(collect["results"][0]["validation_status"], "rejected")
            self.assertEqual(manual_gate["payload"]["question_id"], "Q-TASK-001-ATTEMPT-001")
            self.assertEqual(
                manual_gate["payload"]["question"],
                "Should runtime implement Feishu notification first?",
            )
            self.assertEqual(manual_gate["payload"]["options"], ["yes", "no"])
            self.assertEqual(state_task["backlog_status"], "blocked")
            self.assertEqual(state_task["blockers"], ["Q-TASK-001-ATTEMPT-001"])
            self.assertEqual(snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["gate_status"], "waiting")


    def test_two_phase_scheduler_records_permission_request_for_blocked_result(self):
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

            dispatch = scheduler.dispatch_ready()
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
                    "permission_request": {
                        "requested_capability": "sandbox_escalation",
                        "reason": "Codex hit an operation-not-permitted sandbox failure.",
                        "command": ["codex", "exec", "-s", "workspace-write"],
                        "sandbox": "workspace-write",
                        "scope": "next_attempt",
                    }
                },
            )

            collect = scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            permission_request = [
                event for event in events if event["event_type"] == "permission_request_required"
            ][0]
            state_task = scheduler.state["backlog"]["items"][0]
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(collect["results"][0]["validation_status"], "rejected")
            self.assertEqual(
                permission_request["payload"]["request_id"],
                "PERM-TASK-001-ATTEMPT-001",
            )
            self.assertEqual(
                permission_request["payload"]["requested_capability"],
                "sandbox_escalation",
            )
            self.assertEqual(state_task["backlog_status"], "blocked")
            self.assertEqual(state_task["blockers"], ["PERM-TASK-001-ATTEMPT-001"])
            self.assertEqual(
                snapshot["permission_requests"]["PERM-TASK-001-ATTEMPT-001"]["request_status"],
                "waiting",
            )


    def test_scheduler_hydrates_provider_predecessor_from_canonical_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            predecessor = InvocationLifecycle(
                output_dir,
                _model_invocation_context(
                    coverage_class="supported_model_invocation",
                    provider_usage_scope="session_cumulative",
                    provider_session_lock_held=True,
                    provider_project_binding_valid=True,
                ),
                invocation_id="INV-provider-canonical-001",
                started_at="2026-07-23T00:00:00Z",
            )
            predecessor.publish_start(_supported_execution_group_identity())
            predecessor.finalize(
                "completed",
                stdout=json.dumps(
                    {
                        "type": "turn_completed",
                        "provider_usage_scope": "session_cumulative",
                        "provider_session_id": "provider-session-canonical",
                        "provider_turn_id": "turn-canonical-1",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 30,
                            "reasoning_tokens": 5,
                            "total_tokens": 130,
                        },
                    }
                ),
                finished_at="2026-07-23T00:00:01Z",
            )
            import_model_invocation_lifecycle(
                output_dir / "events.jsonl",
                predecessor.started_path,
                source_root=output_dir,
            )
            canonical_events = _read_jsonl_for_test(
                output_dir / "events.jsonl"
            )
            lineage = authoritative_provider_snapshot(
                list(reversed(canonical_events)),
                "provider-session-canonical",
            )
            self.assertEqual(
                lineage["provider_predecessor_invocation_id"],
                predecessor.invocation_id,
            )
            self.assertEqual(
                lineage["provider_predecessor_usage_snapshot"]["total_tokens"],
                130,
            )

            agent_pool_path = tmp_path / "agent_pool.json"
            task = _backlog_task(
                "TASK-RESUME-001",
                write_scope=[],
            )
            task.update(
                {
                    "requested_provider_session_id": (
                        "provider-session-canonical"
                    ),
                    "provider_usage_scope": "session_cumulative",
                }
            )
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=[],
                tasks=[task],
            )
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map"],
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
            )

            scheduler.dispatch_ready()

            inflight = scheduler.state["inflight_attempts"][0]
            message = _read_first_jsonl(
                output_dir
                / "steps"
                / inflight["step_id"]
                / "mailboxes"
                / inflight["agent_id"]
                / "inbox.jsonl"
            )
            payload = message["payload"]
            self.assertEqual(
                payload["provider_predecessor_invocation_id"],
                predecessor.invocation_id,
            )
            self.assertEqual(
                payload["provider_predecessor_turn_id"],
                "turn-canonical-1",
            )
            self.assertEqual(
                payload["provider_predecessor_usage_snapshot"]["total_tokens"],
                130,
            )
            self.assertNotEqual(
                payload["runtime_execution_session_id"],
                payload["requested_provider_session_id"],
            )

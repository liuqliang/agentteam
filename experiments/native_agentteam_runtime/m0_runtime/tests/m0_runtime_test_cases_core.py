try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class CoreMixin:
    def test_run_completion_report_recomputes_stale_rejected_blocked_count(self):
        from agentteam_runtime.operator_report import build_run_completion_report

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "RUN-SALVAGE"
            run_dir.mkdir()
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
                                "task_id": "TASK-SALVAGE",
                                "status": "implementation rejected",
                                "what_changed": [
                                    "Patch available from worker worktree after timeout."
                                ],
                                "changed_files": ["agentteam_runtime/mailbox_worker.py"],
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

            report = build_run_completion_report(run_dir, project="agentteam", write_files=False)

        self.assertEqual(report["blocked_count"], 1)
        self.assertEqual(report["run_outcome"], "completed_with_review_required")
        self.assertEqual(
            report["completion_summary"]["status_line"],
            "completed: 1 task reported, 1 blocked task",
        )


    def test_evidence_summary_defaults_and_preserves_missing_items(self):
        summary = normalize_evidence_summary(
            {
                "evidence_level": "L2",
                "trace_carrier": [{"type": "event", "path": "EVT-0001"}],
                "missing_evidence": ["review_result"],
            },
            default_level="L1",
        )

        self.assertEqual(summary["evidence_level"], "L2")
        self.assertEqual(summary["evidence_status"], "incomplete")
        self.assertEqual(summary["trace_carrier"], [{"type": "event", "path": "EVT-0001"}])
        self.assertEqual(summary["missing_evidence"], ["review_result"])

        default_summary = normalize_evidence_summary({}, default_level="L1")

        self.assertEqual(default_summary["evidence_level"], "L1")
        self.assertEqual(default_summary["evidence_status"], "complete")
        self.assertEqual(default_summary["trace_carrier"], [])
        self.assertEqual(default_summary["missing_evidence"], [])


    def test_runtime_evidence_summary_degrades_malformed_trace_carrier(self):
        result = _runtime_evidence_summary(
            {"risk_target": "L1"},
            {
                "output": {
                    "evidence_summary": {
                        "evidence_level": "L1",
                        "evidence_status": "complete",
                        "trace_carrier": {"command": "python3 -m unittest"},
                    }
                }
            },
        )

        self.assertEqual(result["evidence_level"], "L1")
        self.assertEqual(result["evidence_status"], "incomplete")
        self.assertEqual(result["trace_carrier"], [])
        self.assertIn("invalid_evidence_summary: trace_carrier must be a list of objects", result["missing_evidence"])


    def test_operator_task_report_marks_agentteam_target_review_gate(self):
        report = _operator_task_report(
            {"task_id": "TASK-AGENTTEAM-001"},
            {
                "task_id": "TASK-AGENTTEAM-001",
                "attempt_id": "TASK-AGENTTEAM-001-ATTEMPT-001",
                "validation_status": "accepted",
                "integration_verification_status": "passed",
                "runtime_output": {
                    "operator_summary": {
                        "what_changed": ["已实现 AgentTeam 目标仓库策略。"],
                        "deliverables": [
                            {
                                "deliverable": "agentteam_target_review_gate",
                                "summary": "已保留 operator 审查门。",
                                "evidence": ["taskpack.yaml"],
                            }
                        ],
                    }
                },
            },
        )

        self.assertTrue(report["agentteam_target_review_required"])


    def test_operator_task_report_preserves_measured_result(self):
        report = _operator_task_report(
            {"task_id": "TASK-OPT-001"},
            {
                "task_id": "TASK-OPT-001",
                "attempt_id": "TASK-OPT-001-ATTEMPT-001",
                "validation_status": "accepted",
                "integration_verification_status": "passed",
                "runtime_output": {
                    "operator_summary": {
                        "what_changed": ["优化了算法窗口复制路径。"],
                        "measured_result": ["窗口复制阶段耗时下降 2%。"],
                    }
                },
            },
        )

        self.assertEqual(report["measured_result"], ["窗口复制阶段耗时下降 2%。"])


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


    def test_status_run_outcome_marks_completed_run_with_blockers_for_review(self):
        from agentteam_runtime.agentteam import _status_run_outcome

        self.assertEqual(
            _status_run_outcome("completed", {"done": 0, "blocked": 1}),
            "completed_with_review_required",
        )
        self.assertEqual(
            _status_run_outcome("running", {"done": 0, "blocked": 1}),
            "review_required",
        )
        self.assertEqual(
            _status_run_outcome("completed", {"done": 1, "blocked": 0}),
            "completed",
        )


    def test_supervision_snapshots_are_bounded_and_keep_latest_observations(self):
        snapshots = []
        total = MAX_RETAINED_SUPERVISION_SNAPSHOTS + 7
        for sequence in range(total):
            _record_supervision_snapshot(snapshots, {"sequence": sequence})

        self.assertEqual(len(snapshots), MAX_RETAINED_SUPERVISION_SNAPSHOTS)
        self.assertEqual(
            [item["sequence"] for item in snapshots],
            list(range(total - MAX_RETAINED_SUPERVISION_SNAPSHOTS, total)),
        )


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


    def test_runtime_observability_reports_runtime_profile_source_counts(self):
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

            summary = build_runtime_observability(output_dir)

            self.assertEqual(
                summary["runtime_profile_source_counts"],
                {"explicit_runtime_adapter": 2},
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


    def test_runtime_command_progress_streams_run_events_while_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "run"
            script = tmp_path / "slow_runtime.py"
            script.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys, time",
                        "run_dir = pathlib.Path(sys.argv[1])",
                        "(run_dir / 'state').mkdir(parents=True, exist_ok=True)",
                        "events_path = run_dir / 'events.jsonl'",
                        "state_path = run_dir / 'state' / 'two_phase_scheduler_state.json'",
                        "state_path.write_text(json.dumps({",
                        "  'scheduler_status': 'running',",
                        "  'backlog': {'items': [{'task_id': 'TASK-001', 'backlog_status': 'ready'}]},",
                        "  'inflight_attempts': [{'task_id': 'TASK-001'}]",
                        "}, sort_keys=True), encoding='utf-8')",
                        "events_path.write_text(json.dumps({",
                        "  'event_type': 'runtime_session_started',",
                        "  'payload': {",
                        "    'task_id': 'TASK-001',",
                        "    'attempt_id': 'ATTEMPT-001',",
                        "    'lease_id': 'LEASE-001',",
                        "    'runtime_session_id': 'SESSION-001',",
                        "    'runtime_adapter': 'fake'",
                        "  }",
                        "}, sort_keys=True) + '\\n', encoding='utf-8')",
                        "time.sleep(0.25)",
                        "with events_path.open('a', encoding='utf-8') as stream:",
                        "    stream.write(json.dumps({",
                        "      'event_type': 'runtime_output_received',",
                        "      'payload': {",
                        "        'task_id': 'TASK-001',",
                        "        'attempt_id': 'ATTEMPT-001',",
                        "        'result_status': 'completed'",
                        "      }",
                        "    }, sort_keys=True) + '\\n')",
                        "print(json.dumps({'status': 'ok'}, sort_keys=True))",
                    ]
                ),
                encoding="utf-8",
            )
            progress_stream = io.StringIO()

            completed = _run_runtime_command_with_progress(
                [sys.executable, str(script), str(run_dir)],
                env=os.environ.copy(),
                run_dir=run_dir,
                progress=True,
                progress_interval_seconds=0.05,
                progress_stream=progress_stream,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {"status": "ok"})
            progress = progress_stream.getvalue()
            self.assertIn("[agentteam] runtime", progress)
            self.assertIn("event=runtime_session_started", progress)
            self.assertIn("inflight=1", progress)


    def test_runtime_command_progress_suppresses_unchanged_wait_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "run"
            (run_dir / "state").mkdir(parents=True, exist_ok=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "backlog": {
                            "items": [
                                {
                                    "task_id": "TASK-001",
                                    "backlog_status": "ready",
                                }
                            ]
                        },
                        "inflight_attempts": [{"task_id": "TASK-001"}],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (run_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "event_type": "runtime_session_started",
                        "payload": {
                            "task_id": "TASK-001",
                            "attempt_id": "ATTEMPT-001",
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            script = tmp_path / "stable_runtime.py"
            script.write_text(
                "\n".join(
                    [
                        "import json, time",
                        "time.sleep(0.25)",
                        "print(json.dumps({'status': 'ok'}, sort_keys=True))",
                    ]
                ),
                encoding="utf-8",
            )
            progress_stream = io.StringIO()

            completed = _run_runtime_command_with_progress(
                [sys.executable, str(script)],
                env=os.environ.copy(),
                run_dir=run_dir,
                progress=True,
                progress_interval_seconds=0.05,
                progress_stream=progress_stream,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            progress_lines = [
                line
                for line in progress_stream.getvalue().splitlines()
                if line.startswith("[agentteam] runtime ")
            ]
            self.assertEqual(len(progress_lines), 1, progress_lines)


    def test_runtime_command_progress_suppresses_non_state_event_chatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_dir = tmp_path / "run"
            (run_dir / "state").mkdir(parents=True, exist_ok=True)
            (run_dir / "state" / "two_phase_scheduler_state.json").write_text(
                json.dumps(
                    {
                        "scheduler_status": "running",
                        "backlog": {
                            "items": [
                                {
                                    "task_id": "TASK-001",
                                    "backlog_status": "ready",
                                }
                            ]
                        },
                        "inflight_attempts": [{"task_id": "TASK-001"}],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            events_path = run_dir / "events.jsonl"
            events_path.write_text(
                json.dumps(
                    {
                        "event_type": "runtime_session_started",
                        "payload": {
                            "task_id": "TASK-001",
                            "attempt_id": "ATTEMPT-001",
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            script = tmp_path / "chatty_runtime.py"
            script.write_text(
                "\n".join(
                    [
                        "import json, pathlib, sys, time",
                        "events_path = pathlib.Path(sys.argv[1])",
                        "for index in range(3):",
                        "    time.sleep(0.08)",
                        "    with events_path.open('a', encoding='utf-8') as stream:",
                        "        stream.write(json.dumps({",
                        "          'event_type': 'runtime_output_received',",
                        "          'payload': {'task_id': 'TASK-001', 'attempt_id': f'ATTEMPT-{index}'}",
                        "        }, sort_keys=True) + '\\n')",
                        "print(json.dumps({'status': 'ok'}, sort_keys=True))",
                    ]
                ),
                encoding="utf-8",
            )
            progress_stream = io.StringIO()

            completed = _run_runtime_command_with_progress(
                [sys.executable, str(script), str(events_path)],
                env=os.environ.copy(),
                run_dir=run_dir,
                progress=True,
                progress_interval_seconds=0.05,
                progress_stream=progress_stream,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            progress_lines = [
                line
                for line in progress_stream.getvalue().splitlines()
                if line.startswith("[agentteam] runtime ")
            ]
            self.assertEqual(len(progress_lines), 1, progress_lines)


    def test_attempt_outcome_accepts_exact_file_write_scope(self):
        task = {"write_scope": ["pkg/module.py"]}
        result = {
            "result_status": "completed",
            "changed_files": ["pkg/module.py"],
            "output": {},
        }

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "accepted")
        self.assertIsNone(outcome["failure_category"])
        self.assertFalse(outcome["retryable"])


    def test_attempt_outcome_rejects_completed_result_missing_required_deliverables(self):
        task = {
            "task_id": "TASK-001",
            "write_scope": ["generated/"],
            "required_deliverables": [
                "optimization_candidate_matrix",
                "evidence_paths",
            ],
        }
        result = {
            "result_status": "completed",
            "changed_files": ["generated/result.json"],
            "output": {
                "operator_summary": {
                    "what_changed": ["Changed one generated file."],
                }
            },
        }

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(outcome["failure_category"], "missing_required_deliverables")
        self.assertFalse(outcome["retryable"])
        self.assertEqual(
            outcome["semantic_validation"]["missing_required_deliverables"],
            ["optimization_candidate_matrix", "evidence_paths"],
        )


    def test_attempt_outcome_accepts_completed_result_with_required_deliverables(self):
        task = {
            "task_id": "TASK-001",
            "write_scope": ["generated/"],
            "required_deliverables": [
                "optimization_candidate_matrix",
                "evidence_paths",
            ],
        }
        result = {
            "result_status": "completed",
            "changed_files": ["generated/result.json"],
            "output": {
                "operator_summary": {
                    "deliverables": [
                        {
                            "deliverable": "optimization_candidate_matrix",
                            "summary": "已比较 parser 和 feature extraction 候选方案。",
                            "evidence": ["src/pipeline.py"],
                        },
                        {
                            "deliverable": "evidence_paths",
                            "summary": "已列出具体源码和测试文件。",
                            "evidence": ["tests/test_pipeline.py"],
                        },
                    ]
                }
            },
        }

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "accepted")
        self.assertIsNone(outcome["failure_category"])


    def test_attempt_outcome_rejects_completed_result_with_english_operator_summary(self):
        task = {
            "task_id": "TASK-001",
            "write_scope": ["generated/"],
            "required_deliverables": ["evidence_paths"],
        }
        result = {
            "result_status": "completed",
            "changed_files": ["generated/result.json"],
            "output": {
                "operator_summary": {
                    "what_changed": ["Changed one generated file."],
                    "verification_summary": ["Ran focused regression tests."],
                    "merge_recommendation": "Review accepted patch before merging.",
                    "deliverables": [
                        {
                            "deliverable": "evidence_paths",
                            "summary": "Listed concrete source and test files.",
                            "evidence": ["tests/test_pipeline.py"],
                        }
                    ],
                }
            },
        }

        outcome = classify_attempt_outcome(result, task)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(outcome["failure_category"], "operator_summary_language")
        self.assertFalse(outcome["retryable"])
        self.assertEqual(
            outcome["semantic_validation"]["operator_summary_language_issues"],
            [
                "operator_summary.what_changed must include zh-CN text",
                "operator_summary.verification_summary must include zh-CN text",
                "operator_summary.merge_recommendation must include zh-CN text",
                "operator_summary.deliverables[0].summary must include zh-CN text",
            ],
        )


    def test_operator_answer_resumes_manual_gate_task(self):
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
                {"manual_gate": {"question": "Pick the next architecture route."}},
            )
            scheduler.collect_ready_results()

            answer = answer_manual_gate(
                output_dir,
                "Q-TASK-001-ATTEMPT-001",
                "Use the CLI operator answer route first.",
                operator="liuql",
                clock=FixedClock(),
            )
            resumed = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )
            dispatch = resumed.dispatch_ready()
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(answer["answer_status"], "accepted")
            self.assertEqual(answer["task_id"], "TASK-001")
            self.assertEqual(resumed.state["backlog"]["items"][0]["backlog_status"], "ready")
            self.assertEqual(resumed.state["backlog"]["items"][0]["blockers"], [])
            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["gate_status"],
                "answered",
            )
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["answer"],
                "Use the CLI operator answer route first.",
            )
            resumed_inflight = resumed.state["inflight_attempts"][0]
            resumed_message = json.loads(
                Path(resumed_inflight["step_dir"])
                .joinpath("mailboxes/agent-repo-map/inbox.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(
                resumed_message["payload"]["operator_guidance"],
                [
                    {
                        "question_id": "Q-TASK-001-ATTEMPT-001",
                        "answer": "Use the CLI operator answer route first.",
                        "operator": "liuql",
                    }
                ],
            )


    def test_agentteam_answer_cli_resumes_manual_gate_task(self):
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
                {"manual_gate": {"question": "Which route should the runtime take?"}},
            )
            scheduler.collect_ready_results()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "answer",
                    "--run-dir",
                    str(output_dir),
                    "--question-id",
                    "Q-TASK-001-ATTEMPT-001",
                    "--answer",
                    "Resume with the minimal operator answer CLI.",
                    "--operator",
                    "liuql",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            resumed = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )

            self.assertEqual(summary["answer_status"], "accepted")
            self.assertEqual(summary["question_id"], "Q-TASK-001-ATTEMPT-001")
            self.assertEqual(resumed.state["backlog"]["items"][0]["backlog_status"], "ready")
            self.assertEqual(resumed.state["backlog"]["items"][0]["blockers"], [])


    def test_agentteam_permissions_cli_lists_and_approves_request(self):
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
                    "permission_request": {
                        "requested_capability": "sandbox_escalation",
                        "reason": "Need one escalated retry.",
                        "sandbox": "workspace-write",
                    }
                },
            )
            scheduler.collect_ready_results()

            listed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "permissions",
                    "list",
                    "--run-dir",
                    str(output_dir),
                    "--json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            list_summary = json.loads(listed.stdout)
            self.assertEqual(list_summary["permission_status"], "waiting_permission_requests")
            self.assertEqual(list_summary["waiting_count"], 1)
            self.assertEqual(
                list_summary["waiting"][0]["request_id"],
                "PERM-TASK-001-ATTEMPT-001",
            )

            approved = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "permissions",
                    "approve",
                    "--run-dir",
                    str(output_dir),
                    "--request-id",
                    "PERM-TASK-001-ATTEMPT-001",
                    "--operator",
                    "liuql",
                    "--reason",
                    "Allow exactly one bounded retry.",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
            approve_summary = json.loads(approved.stdout)
            resumed = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )

            self.assertEqual(approve_summary["permission_status"], "approved")
            self.assertEqual(
                approve_summary["request_id"],
                "PERM-TASK-001-ATTEMPT-001",
            )
            self.assertEqual(resumed.state["backlog"]["items"][0]["backlog_status"], "ready")
            self.assertEqual(resumed.state["backlog"]["items"][0]["blockers"], [])
            self.assertEqual(
                resumed.state["backlog"]["items"][0]["permission_grants"],
                [
                    {
                        "request_id": "PERM-TASK-001-ATTEMPT-001",
                        "requested_capability": "sandbox_escalation",
                        "operator": "liuql",
                        "reason": "Allow exactly one bounded retry.",
                    }
                ],
            )
            dispatch = resumed.dispatch_ready()
            resumed_inflight = resumed.state["inflight_attempts"][0]
            resumed_message = json.loads(
                Path(resumed_inflight["step_dir"])
                .joinpath("mailboxes/agent-repo-map/inbox.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(dispatch["dispatched_task_ids"], ["TASK-001"])
            self.assertEqual(
                resumed_message["payload"]["permission_grants"],
                [
                    {
                        "request_id": "PERM-TASK-001-ATTEMPT-001",
                        "requested_capability": "sandbox_escalation",
                        "operator": "liuql",
                        "reason": "Allow exactly one bounded retry.",
                    }
                ],
            )


    def test_agentteam_help_lists_permissions_command(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentteam_runtime.agentteam",
                "help",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("permissions", completed.stdout)
        self.assertIn("List, approve, or deny runtime permission requests.", completed.stdout)


    def test_agentteam_resume_interactive_answers_waiting_manual_gate(self):
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
                        "question": "Should runtime continue with the CLI resume route?",
                        "options": ["yes", "no"],
                    }
                },
            )
            scheduler.collect_ready_results()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "resume",
                    "--run-dir",
                    str(output_dir),
                    "--interactive",
                    "--operator",
                    "liuql",
                ],
                input="Yes, continue with the CLI resume route.\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            resumed = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertIn("Q-TASK-001-ATTEMPT-001", completed.stderr)
            self.assertIn(
                "Should runtime continue with the CLI resume route?",
                completed.stderr,
            )
            self.assertEqual(summary["resume_status"], "answered_manual_gate")
            self.assertEqual(summary["answered_count"], 1)
            self.assertEqual(
                summary["answered"][0]["question_id"],
                "Q-TASK-001-ATTEMPT-001",
            )
            self.assertEqual(resumed.state["backlog"]["items"][0]["backlog_status"], "ready")
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["answer"],
                "Yes, continue with the CLI resume route.",
            )


    def test_agentteam_resume_interactive_can_answer_selected_manual_gate(self):
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
            scheduler = TwoPhaseFileScheduler(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                max_attempts=1,
            )
            scheduler.dispatch_ready()
            first = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                first["outbox_path"],
                first["message_id"],
                first["task_id"],
                first["attempt_id"],
                first["lease_id"],
                "blocked",
                [],
                {"manual_gate": {"question": "Resolve the first gate."}},
            )
            scheduler.collect_ready_results()
            scheduler.dispatch_ready()
            second = scheduler.state["inflight_attempts"][0]
            _append_runtime_result_with_output(
                second["outbox_path"],
                second["message_id"],
                second["task_id"],
                second["attempt_id"],
                second["lease_id"],
                "blocked",
                [],
                {"manual_gate": {"question": "Resolve the second gate."}},
            )
            scheduler.collect_ready_results()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "resume",
                    "--run-dir",
                    str(output_dir),
                    "--interactive",
                    "--question-id",
                    "Q-TASK-002-ATTEMPT-001",
                    "--operator",
                    "liuql",
                ],
                input="/gates\n/answer Answer only the second gate.\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertIn("Waiting manual gates:", completed.stderr)
            self.assertIn("Q-TASK-001-ATTEMPT-001", completed.stderr)
            self.assertIn("Q-TASK-002-ATTEMPT-001", completed.stderr)
            self.assertEqual(summary["resume_status"], "answered_manual_gate")
            self.assertEqual(summary["answered_count"], 1)
            self.assertEqual(
                summary["answered"][0]["question_id"],
                "Q-TASK-002-ATTEMPT-001",
            )
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["gate_status"],
                "waiting",
            )
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-002-ATTEMPT-001"]["answer"],
                "Answer only the second gate.",
            )


    def test_agentteam_resume_interactive_rejects_unknown_selected_manual_gate(self):
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
                {"manual_gate": {"question": "Choose the valid gate."}},
            )
            scheduler.collect_ready_results()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "resume",
                    "--run-dir",
                    str(output_dir),
                    "--interactive",
                    "--question-id",
                    "Q-UNKNOWN",
                    "--operator",
                    "liuql",
                ],
                input="This should not be consumed.\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            payload = json.loads(completed.stderr)

            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["question_id"], "Q-UNKNOWN")
            self.assertEqual(
                payload["waiting_question_ids"],
                ["Q-TASK-001-ATTEMPT-001"],
            )


    def test_agentteam_resume_list_prints_waiting_manual_gates_without_answering(self):
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
                        "question": "List this waiting gate.",
                        "reason": "Operator wants to inspect before answering.",
                    }
                },
            )
            scheduler.collect_ready_results()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.agentteam",
                    "resume",
                    "--run-dir",
                    str(output_dir),
                    "--list",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(summary["resume_status"], "waiting_manual_gates")
            self.assertEqual(summary["waiting_count"], 1)
            self.assertEqual(
                summary["waiting"][0]["question_id"],
                "Q-TASK-001-ATTEMPT-001",
            )
            self.assertEqual(summary["waiting"][0]["task_id"], "TASK-001")
            self.assertEqual(summary["waiting"][0]["question"], "List this waiting gate.")
            self.assertEqual(
                summary["waiting"][0]["objective"],
                "Create generated repo index for TASK-001.",
            )
            self.assertEqual(summary["waiting"][0]["risk_target"], "L0")
            self.assertEqual(
                snapshot["manual_gates"]["Q-TASK-001-ATTEMPT-001"]["gate_status"],
                "waiting",
            )


    def test_provider_session_coordinator_is_single_writer_and_rejects_cross_project(self):
        from agentteam_runtime.mailbox_worker import (
            ProviderSessionCoordinator,
            ProviderSessionProjectBindingError,
        )

        class ExplicitResumeAdapter:
            resume_session_id = "provider-session-1"
            resume_last = False

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "provider-state"
            lifecycle_root = tmp_path / "project-a" / "runs"
            predecessor = InvocationLifecycle(
                lifecycle_root,
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                    provider_session_lock_held=True,
                    provider_project_binding_valid=True,
                ),
                invocation_id="INV-provider-predecessor-001",
                started_at="2026-07-23T00:00:00Z",
            )
            predecessor.publish_start(ExecutionGroupIdentity.not_applicable())
            predecessor.finalize(
                "completed",
                stdout=json.dumps(
                    {
                        "type": "turn_completed",
                        "provider_session_id": "provider-session-1",
                        "provider_turn_id": "turn-1",
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
            message = _model_invocation_message()
            message["payload"].update(
                {
                    "provider_session_state_root": str(state_root),
                    "provider_project_identity": "project-a",
                    "provider_project_lifecycle_root": str(lifecycle_root),
                }
            )
            active = 0
            maximum_active = 0
            state_lock = threading.Lock()
            ready = threading.Barrier(2)
            observed = []

            def enter_coordinator():
                nonlocal active, maximum_active
                coordinator = ProviderSessionCoordinator(state_root)
                ready.wait()
                with coordinator.prepare_message(
                    message,
                    ExplicitResumeAdapter(),
                    authority_root=lifecycle_root,
                ) as prepared:
                    with state_lock:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    observed.append(
                        (
                            prepared["payload"]["provider_session_lock_held"],
                            prepared["payload"][
                                "provider_predecessor_invocation_id"
                            ],
                            prepared["payload"][
                                "provider_predecessor_usage_snapshot"
                            ]["total_tokens"],
                        )
                    )
                    time.sleep(0.03)
                    with state_lock:
                        active -= 1

            threads = [threading.Thread(target=enter_coordinator) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(maximum_active, 1)
            self.assertEqual(
                observed,
                [
                    (True, "INV-provider-predecessor-001", 130),
                    (True, "INV-provider-predecessor-001", 130),
                ],
            )

            other_project = deepcopy(message)
            other_project["payload"]["provider_project_identity"] = "project-b"
            with self.assertRaises(ProviderSessionProjectBindingError):
                with ProviderSessionCoordinator(state_root).prepare_message(
                    other_project,
                    ExplicitResumeAdapter(),
                    authority_root=tmp_path / "project-b" / "runs",
                ):
                    pass

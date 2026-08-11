try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class RetryMixin:
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
            retry_message = json.loads(
                Path(second_inflight["step_dir"])
                .joinpath("mailboxes", "agent-repo-map", "inbox.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            retry_handoff = retry_message["payload"]["retry_handoff"]
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
            self.assertEqual(retry_handoff["schema_version"], "retry_handoff.v1")
            self.assertEqual(retry_handoff["prior_attempt_id"], "TASK-001-ATTEMPT-001")
            self.assertEqual(retry_handoff["validation_status"], "rejected")
            self.assertNotIn("runtime_output", retry_handoff)
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
                {"TASK-001": "blocked"},
            )
            self.assertEqual(
                scheduler.state["backlog"]["items"][0]["blockers"],
                ["timeout"],
            )


    def test_verified_dependency_dispatch_retry_success_completes_once_and_unblocks_dependent(self):
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
                    (
                        "import json, pathlib; "
                        "data=json.loads(pathlib.Path("
                        "'generated/retry.json').read_text()); "
                        "assert data['verified'] is True"
                    ),
                ],
            )

            scheduler.dispatch_ready()
            first = scheduler.state["inflight_attempts"][0]
            first_target = Path(first["worktree_path"]) / "generated" / "retry.json"
            first_target.parent.mkdir(parents=True, exist_ok=True)
            first_target.write_text(
                json.dumps({"verified": False}),
                encoding="utf-8",
            )
            _append_runtime_result(
                first["outbox_path"],
                first["message_id"],
                first["task_id"],
                first["attempt_id"],
                first["lease_id"],
                "completed",
                ["generated/retry.json"],
            )
            first_result = scheduler.collect_ready_results()["results"][0]

            retry_dispatch = scheduler.dispatch_ready()
            second = scheduler.state["inflight_attempts"][0]
            second_target = Path(second["worktree_path"]) / "generated" / "retry.json"
            second_target.parent.mkdir(parents=True, exist_ok=True)
            second_target.write_text(
                json.dumps({"verified": True}),
                encoding="utf-8",
            )
            _append_runtime_result(
                second["outbox_path"],
                second["message_id"],
                second["task_id"],
                second["attempt_id"],
                second["lease_id"],
                "completed",
                ["generated/retry.json"],
            )
            second_result = scheduler.collect_ready_results()["results"][0]
            dependent_dispatch = scheduler.dispatch_ready()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            done_events = [
                event
                for event in events
                if event["event_type"] == "backlog_updated"
                and event["payload"].get("task_id") == "TASK-SOURCE"
                and event["payload"].get("task_status") == "done"
            ]

            self.assertEqual(first_result["task_status"], "ready")
            self.assertEqual(retry_dispatch["dispatched_task_ids"], ["TASK-SOURCE"])
            self.assertEqual(second_result["task_status"], "done")
            self.assertEqual(
                second_result["completion_policy"],
                "verified_integration_commit",
            )
            self.assertNotEqual(
                second_result["verified_integration_head_sha"],
                source_head,
            )
            self.assertEqual(len(done_events), 1)
            self.assertEqual(
                dependent_dispatch["dispatched_task_ids"],
                ["TASK-DEPENDENT"],
            )


    def test_verified_dependency_dispatch_success_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
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
                    (
                        "import pathlib; assert pathlib.Path("
                        "'generated/replay.json').exists()"
                    ),
                ],
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            target = Path(inflight["worktree_path"]) / "generated" / "replay.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ok": True}), encoding="utf-8")
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/replay.json"],
            )

            state_before_collection = json.loads(json.dumps(scheduler.state))
            first = scheduler.collect_ready_results()
            verified_head = first["results"][0]["verified_integration_head_sha"]
            scheduler.state = state_before_collection
            replay = scheduler.collect_ready_results()
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            done_events = [
                event
                for event in events
                if event["event_type"] == "backlog_updated"
                and event["payload"].get("task_status") == "done"
            ]
            commit_events = [
                event
                for event in events
                if event["event_type"] == "integration_baseline_commit_evaluated"
                and event["payload"].get("integration_baseline_commit_status")
                == "committed"
            ]

            self.assertEqual(first["results"][0]["task_status"], "done")
            self.assertEqual(replay["collect_status"], "collected")
            self.assertEqual(replay["collected_count"], 1)
            self.assertEqual(replay["results"][0]["task_status"], "done")
            self.assertEqual(
                replay["results"][0]["verified_integration_head_sha"],
                verified_head,
            )
            self.assertEqual(
                _git_rev_parse(output_dir / "integration-baseline", "HEAD"),
                verified_head,
            )
            self.assertEqual(len(done_events), 1)
            self.assertEqual(len(commit_events), 1)


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


    def test_scheduler_reconciles_dead_orphan_but_keeps_live_expired_lease_open(self):
        from agentteam_runtime.model_invocation import invocation_context_from_message

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=[],
                tasks=[_backlog_task("TASK-001", write_scope=[])],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            live_pidfd = os.open("/dev/null", os.O_RDONLY)
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                lease_timeout_seconds=0,
                invocation_fence_assessor=lambda start: {
                    "fence_status": "live_pinned",
                    "proof": "pidfd_and_start_ticks_match",
                    "signal_allowed": True,
                    "pidfd": live_pidfd,
                },
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            message = _read_first_jsonl(
                output_dir
                / "steps"
                / inflight["step_id"]
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            message["payload"]["coverage_class"] = "supported_model_invocation"
            context = invocation_context_from_message(message)
            lifecycle = InvocationLifecycle(
                output_dir,
                context,
                invocation_id="INV-orphan-recovery-001",
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(_supported_execution_group_identity())

            first_collect = scheduler.collect_ready_results()

            self.assertEqual(first_collect["collect_status"], "idle")
            self.assertEqual(scheduler.summary()["inflight_count"], 1)
            self.assertFalse(lifecycle.terminal_path.exists())
            with self.assertRaises(OSError):
                os.fstat(live_pidfd)

            scheduler.invocation_fence_assessor = lambda start: {
                "fence_status": "death_proven",
                "proof": "host_boot_changed",
                "signal_allowed": False,
            }
            second_collect = scheduler.collect_ready_results()
            terminal = json.loads(
                lifecycle.terminal_path.read_text(encoding="utf-8")
            )
            revocation = json.loads(
                lifecycle.revoked_path.read_text(encoding="utf-8")
            )

            self.assertEqual(second_collect["collect_status"], "collected")
            self.assertEqual(scheduler.summary()["inflight_count"], 0)
            self.assertEqual(terminal["invocation_id"], lifecycle.invocation_id)
            self.assertEqual(terminal["terminal_status"], "recovered_orphan")
            self.assertEqual(terminal["terminal_writer"], "recovery_controller")
            self.assertEqual(
                revocation["reason"],
                "worker_process_death_confirmed",
            )
            self.assertEqual(
                second_collect["results"][0]["runtime_output"][
                    "invocation_reconciliation"
                ]["usage_event_id"],
                terminal["usage_event_id"],
            )
            recovery_events = [
                event
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
                for event in [json.loads(line)]
                if event["event_type"] == "model_invocation_writer_revoked"
            ]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(
                recovery_events[0]["payload"]["invocation_id"],
                lifecycle.invocation_id,
            )
            _validate_model_invocation_record(
                "model_invocation_usage.schema.json",
                terminal,
            )
            _validate_model_invocation_record(
                "model_invocation_writer_revoked.schema.json",
                revocation,
            )


    def test_model_invocation_timeout_preserves_prior_provider_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = InvocationLifecycle(
                tmp,
                _model_invocation_context(
                    coverage_class="supported_model_invocation",
                ),
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(_supported_execution_group_identity())
            stdout = json.dumps(
                {
                    "type": "turn_completed",
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_tokens": 4,
                        "total_tokens": 150,
                    },
                }
            )

            terminal = lifecycle.finalize(
                "timed_out",
                stdout=stdout,
                finished_at="2026-07-23T00:00:03Z",
            )

            self.assertEqual(terminal["terminal_status"], "timed_out")
            self.assertEqual(terminal["usage_status"], "reported")
            self.assertEqual(terminal["total_tokens"], 150)
            self.assertEqual(terminal["accounting_method"], "provider_reported")
            _validate_model_invocation_record(
                "model_invocation_usage.schema.json",
                terminal,
            )


    def test_model_invocation_open_crash_and_retry_identities_are_discoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = InvocationLifecycle(
                tmp,
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                    attempt_id="ATTEMPT-001",
                ),
            )
            first.publish_start(ExecutionGroupIdentity.not_applicable())
            second = InvocationLifecycle(
                tmp,
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                    attempt_id="ATTEMPT-002",
                ),
            )
            second.publish_start(ExecutionGroupIdentity.not_applicable())

            self.assertNotEqual(first.invocation_id, second.invocation_id)
            self.assertTrue(first.started_path.is_file())
            self.assertFalse(first.terminal_path.exists())
            self.assertEqual(
                len(list((Path(tmp) / "model_invocations").glob("*/started.json"))),
                2,
            )


    def test_canonical_lifecycle_import_replay_is_idempotent_and_conflict_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events_path = root / "events.jsonl"
            first = InvocationLifecycle(
                root,
                _model_invocation_context(
                    coverage_class="supported_model_invocation",
                    attempt_id="ATTEMPT-RETRY-001",
                ),
                invocation_id="INV-canonical-retry-001",
                started_at="2026-07-23T00:00:00Z",
            )
            first.publish_start(_supported_execution_group_identity())

            imported_open = import_model_invocation_lifecycle(
                events_path,
                first.started_path,
                source_root=root,
            )
            replayed_open = replay_model_invocation_events(events_path)

            self.assertEqual(len(imported_open), 1)
            self.assertEqual(replayed_open["invocation_count"], 1)
            self.assertEqual(
                replayed_open["open_invocation_ids"],
                ["INV-canonical-retry-001"],
            )

            first.finalize(
                "completed",
                stdout=json.dumps(
                    {
                        "type": "turn_completed",
                        "usage": {
                            "input_tokens": 8,
                            "cached_input_tokens": 2,
                            "output_tokens": 3,
                            "reasoning_tokens": 1,
                            "total_tokens": 11,
                        },
                    }
                ),
                finished_at="2026-07-23T00:00:01Z",
            )
            self.assertEqual(
                len(
                    import_model_invocation_lifecycle(
                        events_path,
                        first.started_path,
                        source_root=root,
                    )
                ),
                1,
            )
            self.assertEqual(
                import_model_invocation_lifecycle(
                    events_path,
                    first.started_path,
                    source_root=root,
                ),
                [],
            )

            second = InvocationLifecycle(
                root,
                _model_invocation_context(
                    coverage_class="supported_model_invocation",
                    attempt_id="ATTEMPT-RETRY-001",
                ),
                invocation_id="INV-canonical-retry-002",
                started_at="2026-07-23T00:00:02Z",
            )
            second.publish_start(_supported_execution_group_identity())
            second.finalize(
                "completed",
                stdout=json.dumps(
                    {
                        "type": "turn_completed",
                        "usage": {
                            "input_tokens": 13,
                            "cached_input_tokens": 4,
                            "output_tokens": 6,
                            "reasoning_tokens": 2,
                            "total_tokens": 19,
                        },
                    }
                ),
                finished_at="2026-07-23T00:00:03Z",
            )
            import_model_invocation_lifecycle(
                events_path,
                second.started_path,
                source_root=root,
            )
            events = _read_jsonl_for_test(events_path)
            projection = replay_model_invocation_events(events + events)

            self.assertEqual(projection["invocation_count"], 2)
            self.assertEqual(projection["terminal_count"], 2)
            self.assertEqual(projection["open_invocation_ids"], [])
            self.assertEqual(
                projection["reported_token_totals"]["total_tokens"],
                30,
            )
            usage_event = next(
                event
                for event in events
                if event["event_type"] == "model_invocation_usage_recorded"
            )
            self.assertEqual(
                usage_event["idempotency_key"],
                usage_event["payload"]["usage_event_id"],
            )
            conflicting = deepcopy(usage_event)
            conflicting["event_id"] = "EVT-9999"
            conflicting["sequence"] = 9999
            conflicting["payload"]["terminal_status"] = "failed"
            with self.assertRaises(ModelInvocationIntegrityError):
                replay_model_invocation_events([*events, conflicting])

            revoked = InvocationLifecycle(
                root / "revoked-authority",
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                ),
                invocation_id="INV-canonical-revoked-001",
                started_at="2026-07-23T00:00:04Z",
            )
            revoked.publish_start(ExecutionGroupIdentity.not_applicable())
            revocation = revoked.revoke_writer(
                "LEASE-001",
                revoked_by="recovery-controller",
                reason="worker_process_death_confirmed",
                revoked_at="2026-07-23T00:00:05Z",
            )
            revoked_events_path = root / "revoked-events.jsonl"
            import_model_invocation_lifecycle(
                revoked_events_path,
                revoked.started_path,
                source_root=root,
            )
            revocation_event = next(
                event
                for event in _read_jsonl_for_test(revoked_events_path)
                if event["event_type"]
                == "model_invocation_writer_revoked"
            )
            self.assertEqual(
                revocation_event["idempotency_key"],
                (
                    "model-invocation-writer-revoked:"
                    f"{revocation['invocation_id']}:"
                    f"{revocation['lifecycle_owner_token']}"
                ),
            )

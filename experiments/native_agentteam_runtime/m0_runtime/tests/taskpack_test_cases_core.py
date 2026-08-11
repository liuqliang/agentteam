try:
    from .taskpack_test_support import *
except ImportError:
    from taskpack_test_support import *


class CoreMixin:
    def _run_agentteam_json(self, *args):
        completed = subprocess.run(
            ["python3", "-m", "agentteam_runtime.agentteam", *args, "--json"],
            env=_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)


    def assertProjectionFreshMetadata(self, summary):
        self.assertEqual(summary.get("projection_source"), "db")
        self.assertEqual(summary.get("projection_status"), "fresh")
        self.assertTrue(summary.get("projection_db_path"))
        self.assertNotIn("projection_warning", summary)


    def assertProjectionFallbackHealthMetadata(self, summary, projection_status):
        self.assertEqual(summary.get("projection_source"), "files")
        self.assertEqual(summary.get("projection_status"), projection_status)
        self.assertEqual(summary.get("projection_warning"), "projection_db_unavailable")
        self.assertTrue(summary.get("projection_db_path"))


    def assertProjectionFallbackMetadata(self, summary, projection_status):
        self.assertProjectionFallbackHealthMetadata(summary, projection_status)
        self.assertEqual(summary.get("next_action"), "run agentteam db rebuild")
        self.assertEqual(summary.get("operator_hint"), "agentteam db rebuild")


    def test_runtime_diagnostic_context_summarizes_failed_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = _write_failed_integration_run(Path(tmp) / "runs" / "taskpack-5")

            context = build_runtime_diagnostic_context(run_dir, topic="integration-failure")
            rendered = render_runtime_diagnostic_context(context)

            self.assertEqual(context["agent_role"], "runtime_diagnostic_agent")
            self.assertEqual(context["chat_status"], "context_ready")
            self.assertEqual(context["topic"], "integration-failure")
            self.assertEqual(context["latest_failure"]["task_id"], "optimize-pipeline")
            self.assertEqual(context["latest_failure"]["failed_test"], "test_host_c_model_matches_exported_python_reference_exactly")
            self.assertIn("First differing element 346", context["latest_failure"]["stderr_excerpt"])
            self.assertEqual(
                context["worker_results"][0]["changed_files"],
                [
                    "gesture_recognition/sim_eval.py",
                    "gesture_recognition/tests/test_sim_eval.py",
                ],
            )
            self.assertIn("runtime_diagnostic_agent", rendered)
            self.assertIn("test_host_c_model_matches_exported_python_reference_exactly", rendered)
            self.assertIn("Read-only role", rendered)


    def test_model_invocation_usage_aggregation_keeps_exact_partial_and_open_states_separate(self):
        summary = aggregate_model_invocation_usage(
            _full_run_model_invocation_events()
        )

        self.assertEqual(summary["invocation_count"], 7)
        self.assertEqual(summary["terminal_invocation_count"], 6)
        self.assertEqual(summary["supported_invocation_count"], 6)
        self.assertEqual(
            summary["usage_status_counts"],
            {
                "reported": 2,
                "partial": 2,
                "unavailable": 1,
                "not_applicable": 1,
            },
        )
        self.assertEqual(summary["open_invocations"], 1)
        self.assertEqual(summary["open_supported_invocations"], 1)
        self.assertEqual(
            summary["lifecycle_terminal_coverage"],
            {
                "covered": 5,
                "total": 6,
                "percent": 83.33,
                "status": "partial",
            },
        )
        self.assertEqual(
            summary["token_usage_coverage"],
            {
                "covered": 2,
                "total": 6,
                "percent": 33.33,
                "status": "partial",
            },
        )
        self.assertEqual(summary["reported_totals_scope"], "reported_subset")
        self.assertEqual(
            summary["reported_token_totals"],
            {
                "input_tokens": 150,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_tokens": 3,
                "total_tokens": 180,
                "contributing_invocation_count": 2,
            },
        )
        self.assertEqual(
            summary["partial_known_token_lower_bounds"],
            {
                "input_tokens": 7,
                "cached_input_tokens": None,
                "output_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 9,
                "contributing_invocation_count": 1,
            },
        )
        self.assertEqual(
            summary["observed_token_lower_bound"]["total_tokens"],
            189,
        )
        self.assertEqual(
            summary["stage_breakdown"]["implementation_worker"][
                "invocation_count"
            ],
            3,
        )
        self.assertEqual(
            summary["stage_breakdown"]["runtime_diagnostic"][
                "invocation_count"
            ],
            1,
        )
        self.assertEqual(
            summary["stage_breakdown"]["development_smoke"][
                "invocation_count"
            ],
            1,
        )
        self.assertEqual(
            summary["reason_counts"]["partial"],
            [
                {"reason": "provider_payload_incomplete", "count": 1},
                {
                    "reason": "provider_session_lineage_ambiguous",
                    "count": 1,
                },
            ],
        )
        self.assertEqual(
            summary["reason_counts"]["unavailable"],
            [{"reason": "timeout_before_usage", "count": 1}],
        )
        self.assertEqual(
            summary["completion_status"],
            "blocked_open_invocations",
        )
        self.assertFalse(summary["benchmark_ready"])


    def test_submit_args_from_profile_propagates_worker_codex_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_repo(repo)
            args = SimpleNamespace(
                goal="Use a lower cost worker model.",
                work_root=None,
                taskpack_id=None,
                author_runtime=None,
                runtime=None,
                codex_timeout_seconds=600,
                codex_model=None,
                one_shot=None,
                max_inflight=None,
                max_attempts=None,
                commit_verified_integration=None,
                notification_project=None,
                feishu_webhook_env=None,
                feishu_signing_secret_env=None,
                codex_command=None,
            )
            profile = {
                "project_key": "model-profile",
                "work_root": str(tmp_path / "work"),
                "author_runtime": "codex",
                "default_runtime": "codex",
                "codex_model": "medium",
            }

            submit_args = _submit_args_from_profile(args, repo, profile)

            self.assertEqual(submit_args.codex_model, "medium")

            args.codex_model = "strong-review-model"
            submit_args = _submit_args_from_profile(args, repo, profile)

            self.assertEqual(submit_args.codex_model, "strong-review-model")


    def test_invocation_supervision_probe_proves_linger_identity_and_cleanup_without_provider(self):
        host = _InvocationProbeFakeHost()
        with mock.patch.object(
            agentteam_module,
            "draft_taskpack_from_goal",
            side_effect=AssertionError("provider path must not be called"),
        ) as author_call, mock.patch.object(
            agentteam_module,
            "run_runtime_diagnostic_chat",
            side_effect=AssertionError("provider path must not be called"),
        ) as chat_call:
            summary = agentteam_module._run_invocation_supervision_probe(
                timeout_seconds=1.0,
                host=host,
            )

        self.assertEqual(summary["status"], "passed")
        self.assertTrue(summary["linger"])
        self.assertTrue(summary["enclosing_identity_stable"])
        self.assertTrue(summary["user_manager_identity_stable"])
        self.assertTrue(summary["unit_identity_verified"])
        self.assertTrue(summary["cgroup_drained"])
        self.assertTrue(summary["cleanup_complete"])
        self.assertEqual(summary["provider_calls"], 0)
        self.assertEqual(host.enclosing_show_count, 3)
        self.assertTrue(host.released)
        self.assertTrue(host.pidfd_closed)
        self.assertTrue(host.state_removed)
        author_call.assert_not_called()
        chat_call.assert_not_called()
        self.assertTrue(
            all(command[0] in {"loginctl", "systemctl", "systemd-run"} for command in host.commands)
        )
        systemd_run = next(command for command in host.commands if command[0] == "systemd-run")
        self.assertIn("--property=Type=oneshot", systemd_run)
        self.assertIn("--property=RemainAfterExit=yes", systemd_run)
        self.assertIn("--property=KillMode=control-group", systemd_run)
        self.assertIn("--no-block", systemd_run)
        self.assertTrue(
            any(command[:3] == ["systemctl", "--user", "stop"] for command in host.commands)
        )
        self.assertTrue(
            any(command[:3] == ["systemctl", "--user", "reset-failed"] for command in host.commands)
        )


    def test_invocation_supervision_probe_fails_closed_for_every_bounded_branch(self):
        cases = {
            "non_linux": "linux_required",
            "pidfd_unavailable": "pidfd_open_unavailable",
            "boot_id_missing": "boot_id_missing",
            "command_missing": "required_command_unavailable",
            "command_timeout": "timeout",
            "loginctl_failed": "loginctl_failed",
            "linger_disabled": "linger_disabled",
            "enclosing_unavailable": "enclosing_user_service_unavailable",
            "enclosing_identity_missing": "enclosing_identity_missing",
            "kill_mode_unsuitable": "enclosing_kill_mode_unsuitable",
            "manager_identity_missing": "user_manager_identity_missing",
            "process_identity_missing": "process_identity_missing",
            "unexpected_unit_reuse": "unexpected_unit_reuse",
            "transient_start_failed": "transient_unit_start_failed",
            "unit_identity_missing": "unit_identity_missing",
            "unit_property_mismatch": "unit_property_mismatch",
            "helper_cgroup_mismatch": "helper_cgroup_mismatch",
            "enclosing_restarted": "enclosing_user_service_restarted",
            "manager_identity_changed": "user_manager_identity_changed",
            "unit_show_failed": "unit_identity_unavailable",
            "unit_identity_changed": "unit_identity_changed",
            "helper_identity_changed": "helper_identity_changed",
            "pidfd_open_failed": "pidfd_open_failed",
            "release_failed": "host_operation_failed",
            "helper_exit_timeout": "helper_exit_timeout",
            "cgroup_events_unavailable": "cgroup_events_unavailable",
            "cgroup_events_invalid": "cgroup_events_invalid",
            "cgroup_drain_timeout": "timeout",
            "unit_identity_not_queryable": "unit_identity_not_queryable",
            "cleanup_incomplete": "cleanup_incomplete",
        }
        for scenario, expected_failure in cases.items():
            with self.subTest(scenario=scenario):
                host = _InvocationProbeFakeHost(scenario)
                summary = agentteam_module._run_invocation_supervision_probe(
                    timeout_seconds=0.08,
                    host=host,
                )
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["failure_code"], expected_failure)
                self.assertEqual(summary["provider_calls"], 0)
                if host.unit_started:
                    self.assertTrue(
                        any(
                            command[:3] == ["systemctl", "--user", "stop"]
                            for command in host.commands
                        )
                    )
                    self.assertTrue(host.state_removed)


    def test_invocation_supervision_probe_json_is_compact_and_secret_free(self):
        summary = agentteam_module._run_invocation_supervision_probe(
            timeout_seconds=1.0,
            host=_InvocationProbeFakeHost(),
        )
        expected_fields = {
            "probe",
            "status",
            "linux",
            "pidfd_open",
            "linger",
            "boot_id",
            "enclosing_kill_mode",
            "enclosing_identity_stable",
            "user_manager_identity_stable",
            "transient_unit_created",
            "unit_identity_verified",
            "pidfd_opened",
            "cgroup_drained",
            "unit_identity_queryable_after_exit",
            "cleanup_complete",
            "provider_calls",
            "failure_code",
        }
        self.assertEqual(set(summary), expected_fields)
        with mock.patch.dict(os.environ, {"AGENTTEAM_TEST_SECRET": "do-not-leak"}):
            with mock.patch.object(
                agentteam_module,
                "_run_invocation_supervision_probe",
                return_value=summary,
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = agentteam_module.main(
                        ["doctor", "--invocation-supervision-probe", "--json"]
                    )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(set(payload), expected_fields)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("do-not-leak", serialized)
        self.assertNotIn("/tmp/", serialized)

        failed_summary = dict(summary, status="failed", failure_code="linger_disabled")
        with mock.patch.object(
            agentteam_module,
            "_run_invocation_supervision_probe",
            return_value=failed_summary,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = agentteam_module.main(
                    ["doctor", "--invocation-supervision-probe", "--json"]
                )
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["failure_code"], "linger_disabled")


    def test_permission_request_notification_includes_approve_and_deny_hints(self):
        message = _permission_request_text(
            {
                "payload": {
                    "task_id": "optimize-pipeline",
                    "request_id": "PERM-001",
                    "requested_capability": "sandbox_escalation",
                    "reason": "network is restricted",
                }
            },
            "/tmp/agentteam-run",
            "notify-project",
        )

        self.assertIn("[AgentTeam] permission request required", message)
        self.assertIn("Capability: sandbox_escalation", message)
        self.assertIn(
            "Approve: agentteam permissions approve --run-dir /tmp/agentteam-run --request-id PERM-001",
            message,
        )
        self.assertIn(
            "Deny: agentteam permissions deny --run-dir /tmp/agentteam-run --request-id PERM-001",
            message,
        )


    def test_status_integration_counts_use_latest_attempt_per_task(self):
        snapshot = {
            "integration_queue": {
                "task-1:attempt-1": {
                    "task_id": "task-1",
                    "attempt_id": "attempt-1",
                    "queue_status": "blocked",
                },
                "task-1:attempt-2": {
                    "task_id": "task-1",
                    "attempt_id": "attempt-2",
                    "queue_status": "committed",
                },
                "task-2:attempt-1": {
                    "task_id": "task-2",
                    "attempt_id": "attempt-1",
                    "queue_status": "verified",
                },
            }
        }

        self.assertEqual(
            agentteam_module._status_integration_counts(snapshot),
            {"total": 2, "blocked": 0, "verified": 2},
        )


    def test_post_backlog_repair_publishes_new_epoch_with_candidate_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            run_dir = root / "work" / "runs" / "v4" / "phase1-run"
            current_worktree = root / "current"
            repair_worktree = root / "repair"
            for path in (repo, run_dir, current_worktree, repair_worktree):
                path.mkdir(parents=True)
            runtime_package = (
                repair_worktree
                / "experiments"
                / "native_agentteam_runtime"
                / "m0_runtime"
                / "agentteam_runtime"
            )
            runtime_package.mkdir(parents=True)
            (runtime_package / "__init__.py").write_text("", encoding="utf-8")
            current_head = "a" * 40
            target_head = "b" * 40
            repair_head = "c" * 40
            context = {
                "project_root": repo.resolve(),
                "work_root": root / "work",
                "run_dir": run_dir,
                "declarations_by_id": {"P1-LIVE": {}, "P1-06E": {}},
                "gate_root": run_dir / "state" / "post_backlog_gates",
                "epochs_root": (
                    run_dir / "state" / "post_backlog_gates" / "epochs"
                ),
                "locks_root": (
                    run_dir / "state" / "post_backlog_gates" / "locks"
                ),
            }
            record = {
                "schema_version": "post_backlog_gate_epoch.v1",
                "implementation_run_id": "phase1-run",
                "epoch_number": 1,
                "prior_epoch_sha256": None,
                "gate_declaration_sha256": "d" * 64,
                "git_object_format": "sha1",
                "target_branch": "target",
                "target_head_sha": target_head,
                "integration_branch": "integration-epoch-1",
                "integration_head_sha": current_head,
                "validated_code_sha": current_head,
                "verification_command_sha256": "e" * 64,
                "verification_result_sha256": "f" * 64,
                "created_at": "2026-01-01T00:00:00Z",
            }
            current = {"record": record, "digest": "1" * 64}
            calls = []

            def git_stdout(repo_path, command):
                if command == ["rev-parse", "--show-object-format"]:
                    return "sha1"
                if command == [
                    "rev-parse",
                    "--verify",
                    "integration-epoch-1^{commit}",
                ]:
                    return current_head
                if command == ["rev-parse", "--verify", "target^{commit}"]:
                    return target_head
                if command == [
                    "rev-parse",
                    "--verify",
                    "repair-branch^{commit}",
                ]:
                    return repair_head
                if command == ["status", "--porcelain=v1", "--untracked-files=all"]:
                    return ""
                if command == ["rev-parse", "HEAD"]:
                    self.assertEqual(Path(repo_path), repair_worktree)
                    return repair_head
                raise AssertionError((repo_path, command))

            def publish_epoch(_context, value):
                calls.append(value)
                path = context["epochs_root"] / str(value["epoch_number"])
                path.mkdir(parents=True)
                return path

            verification = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            verified_idle = {
                "status": "awaiting_post_backlog_gates",
            }
            with (
                mock.patch.object(
                    agentteam_module,
                    "_require_post_backlog_gate_context",
                    return_value=context,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_gate_mutation_locks",
                    return_value=nullcontext(),
                ),
                mock.patch.object(
                    agentteam_module,
                    "_require_current_gate_epoch",
                    return_value=current,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_build_run_status_summary",
                    return_value=verified_idle,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_open_gate_controller_invocations",
                    return_value=[],
                ),
                mock.patch.object(
                    agentteam_module,
                    "_git_stdout",
                    side_effect=git_stdout,
                ),
                mock.patch.object(
                    agentteam_module,
                    "_git_completed",
                    return_value=SimpleNamespace(returncode=0),
                ),
                mock.patch.object(
                    agentteam_module,
                    "_git_worktree_for_branch",
                    side_effect=lambda _repo, branch, _head: (
                        repair_worktree
                        if branch == "repair-branch"
                        else current_worktree
                    ),
                ),
                mock.patch.object(
                    agentteam_module,
                    "_frozen_gate_verification_command",
                    return_value=["python3", "-c", "pass"],
                ),
                mock.patch.object(
                    agentteam_module,
                    "_validate_gate_record_schema",
                ),
                mock.patch.object(
                    agentteam_module,
                    "_publish_gate_epoch",
                    side_effect=publish_epoch,
                ),
                mock.patch.object(
                    agentteam_module.subprocess,
                    "run",
                    return_value=verification,
                ) as run_mock,
            ):
                os.environ["AGENTTEAM_LAUNCHER_SELECTION"] = "outer-release"
                try:
                    summary = agentteam_module._gate_repair_baseline(
                        repo,
                        {},
                        run_dir,
                        expected_gate_epoch=1,
                        repair_branch="repair-branch",
                        expected_repair_head=repair_head,
                    )
                finally:
                    os.environ.pop("AGENTTEAM_LAUNCHER_SELECTION", None)

            self.assertEqual(summary["gate_epoch"], 2)
            self.assertEqual(summary["validated_code_sha"], repair_head)
            self.assertEqual(calls[0]["prior_epoch_sha256"], current["digest"])
            self.assertEqual(calls[0]["integration_branch"], "repair-branch")
            verification_env = run_mock.call_args.kwargs["env"]
            self.assertNotIn("AGENTTEAM_LAUNCHER_SELECTION", verification_env)
            self.assertTrue(
                verification_env["PYTHONPATH"].startswith(
                    str(
                        repair_worktree
                        / "experiments"
                        / "native_agentteam_runtime"
                        / "m0_runtime"
                    )
                )
            )


    def test_status_includes_permission_request_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            work_root = tmp_path / "agentteam-work"
            run_dir = work_root / "runs" / "permission-run"
            _init_repo(repo)
            _init_agentteam_profile_for_test(repo, work_root, "permission-project")
            _write_jsonl(
                run_dir / "events.jsonl",
                [
                    {
                        "event_id": "EVT-0001",
                        "event_type": "permission_request_required",
                        "sequence": 1,
                        "time": "2026-06-14T00:00:00Z",
                        "task_id": "optimize-pipeline",
                        "attempt_id": "ATTEMPT-001",
                        "lease_id": "LEASE-001",
                        "payload": {
                            "task_id": "optimize-pipeline",
                            "attempt_id": "ATTEMPT-001",
                            "lease_id": "LEASE-001",
                            "request_id": "PERM-001",
                            "request_type": "sandbox_permission",
                            "request_status": "waiting",
                            "requested_capability": "sandbox_escalation",
                            "reason": "network is restricted",
                            "scope": "next_attempt",
                            "sandbox": "workspace-write",
                            "command": ["codex", "exec"],
                        },
                    }
                ],
            )

            completed = subprocess.run(
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

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("overall_status: permission_required", completed.stdout)
            self.assertIn("permission_requests: 1", completed.stdout)
            self.assertIn(
                "permission_request: PERM-001 task=optimize-pipeline capability=sandbox_escalation",
                completed.stdout,
            )
            self.assertIn("reason: network is restricted", completed.stdout)
            self.assertIn(
                f"approve: agentteam permissions approve --run-dir {run_dir.resolve()} --request-id PERM-001",
                completed.stdout,
            )
            self.assertIn(
                f"deny: agentteam permissions deny --run-dir {run_dir.resolve()} --request-id PERM-001",
                completed.stdout,
            )


    def test_goal_memory_bounds_round_history_and_prompt_text(self):
        long_text = "继续验证候选实现并记录基准。" * 40
        rounds = [
            {
                "round": index,
                "taskpack_id": f"pursue-loop-r{index}",
                "status": "completed",
                "run_status": "completed",
                "blocked_count": index % 2,
                "report_path": f"/tmp/reports/r{index}.md",
            }
            for index in range(1, 8)
        ]

        memory = build_goal_memory(
            pursue_id="pursue-loop",
            original_goal=long_text,
            work_root="/tmp/agentteam-work",
            rounds=rounds,
            source_report={
                "report_path": "/tmp/reports/r7.md",
                "completion_summary": {
                    "what_changed": [long_text],
                    "next_steps": [long_text],
                    "evidence_gaps": [long_text],
                },
            },
            stop_reason="blocked",
            max_round_history=3,
            max_text_chars=80,
            max_queue_items=2,
        )
        rendered = render_goal_memory_prompt_context(memory)

        self.assertEqual(memory["memory_schema_version"], "goal_memory.v1")
        self.assertEqual([item["round"] for item in memory["round_history"]], [5, 6, 7])
        self.assertEqual(memory["latest_run_ids"], ["pursue-loop-r5", "pursue-loop-r6", "pursue-loop-r7"])
        self.assertLessEqual(len(memory["original_goal"]), 80)
        self.assertLessEqual(len(memory["current_hypothesis"]), 80)
        self.assertLessEqual(len(memory["current_next_step"]), 80)
        self.assertLessEqual(len(memory["blocked_reasons"][0]), 80)
        self.assertLessEqual(len(memory["follow_up_queue"]), 2)
        self.assertIn("Long-goal memory:", rendered)
        self.assertIn("memory_bounds: round_history<=3 text<=80 queue<=2", rendered)


    def test_goal_memory_carries_latest_round_recap_for_followup_context(self):
        report_path = "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.md"
        report_json_path = "/tmp/agentteam-work/runs/pursue-loop/reports/final_report.json"

        memory = build_goal_memory(
            pursue_id="pursue-loop",
            original_goal="持续增强 AgentTeam 长期任务可靠性。",
            work_root="/tmp/agentteam-work",
            rounds=[
                {
                    "round": 1,
                    "taskpack_id": "pursue-loop",
                    "status": "completed",
                    "run_status": "completed",
                    "blocked_count": 1,
                    "report_path": report_path,
                }
            ],
            source_report={
                "run_id": "pursue-loop",
                "run_dir": "/tmp/agentteam-work/runs/pursue-loop",
                "run_status": "completed",
                "run_outcome": "completed_with_review_required",
                "blocked_count": 1,
                "report_path": report_path,
                "report_json_path": report_json_path,
                "token_usage": {
                    "usage_status": "reported",
                    "reported_attempt_count": 1,
                    "unreported_attempt_count": 0,
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "total_tokens": 1500,
                },
                "completion_summary": {
                    "what_changed": ["持久化上一轮 recap。"],
                    "changed_files": ["agentteam_runtime/goal_memory.py"],
                    "verification": ["python3 -m unittest test_taskpack.GoalMemory passed"],
                    "measured_results": ["latest recap persisted"],
                    "next_steps": ["继续实现 queue recap 展示。"],
                    "evidence_gaps": ["需要 operator review。"],
                },
            },
            stop_reason="review_gate_required",
        )
        rendered = render_goal_memory_prompt_context(memory)

        latest = memory["latest_round_recap"]
        self.assertEqual(latest["taskpack_id"], "pursue-loop")
        self.assertEqual(latest["result_status"], "completed")
        self.assertEqual(latest["run_outcome"], "completed_with_review_required")
        self.assertEqual(latest["stop_reason"], "review_gate_required")
        self.assertEqual(latest["recommended_next_step"], "继续实现 queue recap 展示。")
        self.assertEqual(latest["token_usage"]["usage_status"], "reported")
        self.assertEqual(latest["token_usage"]["total_tokens"], 1500)
        self.assertIn({"type": "report", "path": report_path}, latest["evidence_paths"])
        self.assertIn({"type": "report_json", "path": report_json_path}, latest["evidence_paths"])
        self.assertIn("需要 operator review。", latest["blockers"])
        self.assertEqual(memory["round_history"][-1]["recommended_next_step"], "继续实现 queue recap 展示。")
        self.assertEqual(
            memory["follow_up_queue"][0]["source_evidence_paths"],
            latest["evidence_paths"],
        )
        self.assertEqual(memory["follow_up_queue"][0]["stop_reason"], "review_gate_required")
        self.assertEqual(memory["follow_up_queue"][0]["token_usage"]["total_tokens"], 1500)
        self.assertIn("latest_result: completed_with_review_required", rendered)
        self.assertIn(f"latest_evidence_path: {report_path}", rendered)
        self.assertIn("latest_stop_reason: review_gate_required", rendered)
        self.assertIn("latest_token_usage: Token usage: total=1500 input=1200 output=300 reported=1/1", rendered)
        self.assertIn("latest_recommended_next_step: 继续实现 queue recap 展示。", rendered)


    def test_classify_goal_kind_treats_generic_improve_as_implementation(self):
        self.assertEqual(
            taskpack_module.classify_goal_kind("Improve repo grounding with deterministic structure signals."),
            "implementation",
        )
        self.assertEqual(
            taskpack_module.classify_goal_kind("改进任务包语义补全流程。"),
            "implementation",
        )
        self.assertEqual(
            taskpack_module.classify_goal_kind("Improve parser latency with a benchmarked optimization."),
            "optimization",
        )


    def test_status_text_separates_overall_and_run_status(self):
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
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                _write_status_text(summary)

            output = stdout.getvalue()
            self.assertIn("overall_status: authoring", output)
            self.assertIn("run_status: idle", output)
            self.assertIn("active_phase: authoring", output)
            self.assertIn("active_authoring: follow-up", output)
            self.assertNotIn("\nstatus: idle\n", output)


    def test_canonical_run_dir_resolves_nested_low_level_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            outer = tmp_path / "runs" / "taskpack-5"
            nested = outer / "taskpack-5"
            nested.mkdir(parents=True)
            (nested / "events.jsonl").write_text("", encoding="utf-8")

            self.assertEqual(_canonical_run_dir(outer), nested.resolve())


    def test_followup_detects_reusable_repo_map_handoff_from_integration_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            baseline = tmp_path / "integration-baseline"
            handoff = baseline / taskpack_module.REPO_MAP_HANDOFF_PATH
            _write_json(handoff, {"schema_version": "repo_map_handoff.v1"})
            source_report = {
                "integration_baseline": {
                    "worktree_path": str(baseline),
                    "worktree_exists": True,
                    "branch": "agentteam/run/first-pass/integration",
                }
            }

            reuse = agentteam_module._reusable_repo_map_handoff_path(source_report)

            self.assertEqual(reuse, taskpack_module.REPO_MAP_HANDOFF_PATH)


    def _publish_pre04_run(self, work_root, release, run_id, sequence_project="pre04"):
        return publish_implementation_run(
            work_root,
            project_key=sequence_project,
            run_id=run_id,
            taskpack_id=run_id,
            release_identity=release,
        )


    def test_pre04_06_legacy_run_is_excluded_from_implicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            legacy = work_root / "runs" / "legacy"
            legacy.mkdir(parents=True)
            (legacy / "events.jsonl").write_text("", encoding="utf-8")

            with self.assertRaises(AgentTeamReleaseError):
                select_latest_implementation_run(work_root, expected_project_key="pre04")
            self.assertTrue(legacy.is_dir())


    def test_pre04_07_explicit_and_implicit_selection_share_identity_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            explicit = self._publish_pre04_run(work_root, release, "run-1")

            implicit = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(explicit["identity_sha256"], implicit["identity_sha256"])
            self.assertEqual(explicit["binding"], implicit["binding"])


    def test_pre04_08c_flat_vn_run_id_is_not_treated_as_namespace(self):
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
            write_project_profile(
                repo,
                build_project_profile(
                    repo,
                    project_key="pre04",
                    work_root=work_root,
                    author_runtime="fake",
                    default_runtime="fake",
                    one_shot=True,
                ),
            )
            draft = draft_taskpack_files(
                project_root=repo,
                goal="Exercise a flat vN run identity.",
                draft_root=work_root / "drafts",
                taskpack_id="v2",
                write_scope=["src/"],
            )
            _set_taskpack_runtime_backend(draft["taskpack_dir"], "fake")
            taskpack_path = Path(draft["taskpack_dir"]) / "taskpack.yaml"
            taskpack = json.loads(taskpack_path.read_text(encoding="utf-8"))
            taskpack["context"] = {
                "runtime_release_id": release["release_id"],
                "runtime_release_source_commit": release["source_commit"],
                "git_object_format": release["git_object_format"],
            }
            _write_json(taskpack_path, taskpack)
            frozen = Path(
                freeze_taskpack(
                    draft["taskpack_dir"],
                    work_root / "frozen",
                )["frozen_taskpack_dir"]
            )
            env = _test_env()
            env.pop("PYTHONPATH", None)
            launcher_path = str(Path(__file__).resolve().parents[4] / "agentteam")
            started = subprocess.run(
                [
                    launcher_path,
                    "run",
                    str(frozen),
                    "--run-root",
                    str(work_root / "runs"),
                    "--one-shot",
                    "--json",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            pair = validate_run_binding(
                work_root / "runs" / "v2",
                expected_project_key="pre04",
            )
            launcher = runpy.run_path(
                launcher_path
            )

            runtime_selected = select_latest_implementation_run(
                work_root,
                expected_project_key="pre04",
            )
            launcher_selected = launcher["_latest_bound_run"](
                work_root,
                "pre04",
            )

            self.assertEqual(runtime_selected["run_dir"], pair["run_dir"])
            self.assertEqual(launcher_selected["run_dir"], pair["run_dir"])
            for resume_args in (
                ["--run-dir", pair["run_dir"]],
                ["--taskpack", "v2"],
                [],
            ):
                continued = subprocess.run(
                    [
                        launcher_path,
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
                result = json.loads(continued.stdout)
                self.assertEqual(result["paths"]["run_dir"], pair["run_dir"])
                self.assertEqual(
                    result["paths"]["frozen_taskpack_dir"],
                    str(frozen),
                )
            with self.assertRaises(AgentTeamReleaseError):
                publish_implementation_run(
                    work_root,
                    project_key="pre04",
                    run_id="nested-run",
                    taskpack_id="nested-run",
                    release_identity=release,
                    run_root=work_root / "runs" / "v2",
                )


    def test_pre04_09_acceptance_evidence_does_not_replace_latest_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            implementation = self._publish_pre04_run(work_root, release, "run-1")
            publish_acceptance_run_identity(
                work_root,
                project_key="pre04",
                run_id="evidence-1",
                taskpack_id="gate-taskpack",
                implementation_run_id="run-1",
                gate_epoch=1,
            )

            latest = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(latest["run_dir"], implementation["run_dir"])


    def test_pre04_11_concurrent_creation_allocates_unique_monotonic_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            results = []
            errors = []

            def create(run_id):
                try:
                    results.append(self._publish_pre04_run(work_root, release, run_id))
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=create, args=(f"run-{index}",))
                for index in range(1, 5)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(
                sorted(item["identity"]["creation_sequence"] for item in results),
                [1, 2, 3, 4],
            )


    def test_pre04_12_mtime_and_evidence_creation_do_not_change_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            older = self._publish_pre04_run(work_root, release, "z-old")
            newer = self._publish_pre04_run(work_root, release, "a-new")
            os.utime(older["run_dir"], (2000000000, 2000000000))
            publish_acceptance_run_identity(
                work_root,
                project_key="pre04",
                run_id="evidence-new",
                taskpack_id="gate-taskpack",
                implementation_run_id="a-new",
                gate_epoch=2,
            )

            latest = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(latest["run_dir"], newer["run_dir"])


    def test_pre04_13_newest_failed_run_remains_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            self._publish_pre04_run(work_root, release, "run-old")
            newest = self._publish_pre04_run(work_root, release, "run-failed")
            _write_json(
                Path(newest["run_dir"]) / "state" / "scheduler_state.json",
                {"scheduler_status": "failed"},
            )

            selected = select_latest_implementation_run(work_root, expected_project_key="pre04")

            self.assertEqual(selected["run_dir"], newest["run_dir"])


    def test_pre04_14_explicit_older_bound_run_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            older = self._publish_pre04_run(work_root, release, "run-old")
            self._publish_pre04_run(work_root, release, "run-new")

            explicit = validate_run_binding(
                older["run_dir"], expected_project_key="pre04"
            )

            self.assertEqual(explicit["identity"]["creation_sequence"], 1)


    def test_pre04_15_legacy_run_can_be_adopted_without_blocking_new_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            release = _pre04_release_fixture(work_root, "release-1")
            legacy = work_root / "runs" / "legacy-run"
            legacy.mkdir(parents=True)
            marker = legacy / "preserved.txt"
            marker.write_text("legacy evidence\n", encoding="utf-8")

            created = self._publish_pre04_run(work_root, release, "new-run")
            with self.assertRaises(AgentTeamReleaseError):
                select_latest_implementation_run(work_root, expected_project_key="pre04")

            adopted = adopt_legacy_implementation_run(
                work_root,
                project_key="pre04",
                run_id="legacy-run",
                taskpack_id="legacy-run",
                release_identity=release,
            )

            self.assertEqual(created["identity"]["creation_sequence"], 1)
            self.assertEqual(adopted["identity"]["creation_sequence"], 2)
            self.assertEqual(marker.read_text(encoding="utf-8"), "legacy evidence\n")
            self.assertEqual(
                select_latest_implementation_run(
                    work_root,
                    expected_project_key="pre04",
                )["run_dir"],
                str(legacy.resolve()),
            )


    def test_pre04_17_unsafe_or_incomplete_direct_children_fail_closed(self):
        cases = ("regular-file", "state-symlink", "binding-without-identity")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                work_root = Path(tmp)
                runs = work_root / "runs"
                runs.mkdir(parents=True)
                child = runs / "unsafe"
                if case == "regular-file":
                    child.write_text("not a run\n", encoding="utf-8")
                elif case == "state-symlink":
                    child.mkdir()
                    external = work_root / "external-state"
                    external.mkdir()
                    (child / "state").symlink_to(external, target_is_directory=True)
                else:
                    (child / "state").mkdir(parents=True)
                    _write_json(
                        child / "state" / "runtime_release_binding.v1.json",
                        {},
                    )

                with self.assertRaises(AgentTeamReleaseError):
                    scan_run_identities(work_root, expected_project_key="pre04")


    def test_pre04_19_help_and_supervision_probe_do_not_select_a_run(self):
        launcher = runpy.run_path(str(Path(__file__).resolve().parents[4] / "agentteam"))

        self.assertIsNone(launcher["_launcher_selection"](["gate", "--help"]))
        self.assertIsNone(
            launcher["_launcher_selection"](
                ["doctor", "--invocation-supervision-probe"]
            )
        )


    def test_pre04_20_invalid_paired_identity_variants_fail_closed(self):
        cases = ("duplicate-sequence", "wrong-project", "directory-mismatch", "missing-binding")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                work_root = Path(tmp)
                release = _pre04_release_fixture(work_root, "release-1")
                first = self._publish_pre04_run(work_root, release, "run-1")
                second = self._publish_pre04_run(work_root, release, "run-2")
                identity_path = Path(second["run_dir"]) / "state" / "run_identity.v1.json"
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
                if case == "duplicate-sequence":
                    identity["creation_sequence"] = first["identity"]["creation_sequence"]
                    _write_json(identity_path, identity)
                elif case == "wrong-project":
                    identity["project_key"] = "another-project"
                    _write_json(identity_path, identity)
                elif case == "directory-mismatch":
                    identity["run_id"] = "not-run-2"
                    _write_json(identity_path, identity)
                else:
                    (
                        Path(second["run_dir"])
                        / "state"
                        / "runtime_release_binding.v1.json"
                    ).unlink()

                with self.assertRaises(AgentTeamReleaseError):
                    select_latest_implementation_run(
                        work_root,
                        expected_project_key="pre04",
                    )

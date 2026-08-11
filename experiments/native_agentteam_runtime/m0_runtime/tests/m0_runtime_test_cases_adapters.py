try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class AdaptersMixin:
    def test_fake_runtime_adapter_reports_chinese_operator_summary(self):
        message = {
            "payload": {
                "task_id": "TASK-001",
                "attempt_id": "TASK-001-ATTEMPT-001",
                "write_scope": ["generated/"],
                "required_deliverables": [
                    "repository_understanding_summary",
                    "verification_summary",
                ],
            }
        }

        result = FakeRuntimeAdapter().run(message)

        operator_summary = result["output"]["operator_summary"]
        self.assertEqual(
            operator_summary["what_changed"],
            ["Fake runtime 为任务生成了确定性的有界结果。"],
        )
        self.assertEqual(
            operator_summary["verification_summary"],
            ["fake_runtime: 已通过"],
        )
        self.assertEqual(
            operator_summary["merge_recommendation"],
            "人工审阅通过后再合并已接受的补丁。",
        )
        self.assertEqual(
            [item["deliverable"] for item in operator_summary["deliverables"]],
            [
                "repository_understanding_summary",
                "verification_summary",
            ],
        )
        self.assertEqual(
            operator_summary["deliverables"][0]["summary"],
            "Fake runtime 已满足 repository_understanding_summary。",
        )


    def test_role_runtime_profile_propagates_codex_reasoning_effort(self):
        from agentteam_runtime.m0_runtime import (
            _runtime_adapter_from_profile,
            _with_codex_reasoning_profile,
        )

        adapter = _runtime_adapter_from_profile(
            {
                "adapter": "codex",
                "model": "gpt-5.6-sol",
                "reasoning_profile": "medium",
            },
            project_root="/tmp/agentteam-reasoning-profile-test",
        )

        self.assertEqual(adapter.model, "gpt-5.6-sol")
        self.assertEqual(adapter.reasoning_profile, "medium")
        self.assertEqual(
            _with_codex_reasoning_profile(
                ["codex", "exec", "--json", "-"],
                adapter.reasoning_profile,
            ),
            [
                "codex",
                "exec",
                "--json",
                "-c",
                "model_reasoning_effort=medium",
                "-",
            ],
        )


    def test_stop_inflight_invocations_uses_exact_registered_service_stopper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[])
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            stopped = []
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                invocation_service_stopper=lambda start: stopped.append(start) or True,
            )
            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            invocation_dir = output_dir / "model_invocations" / "INV-stop-test"
            invocation_dir.mkdir(parents=True)
            (invocation_dir / "started.json").write_text(
                json.dumps(
                    {
                        "invocation_id": "INV-stop-test",
                        "attempt_id": inflight["attempt_id"],
                        "lifecycle_owner_token": inflight["lease_id"],
                        "agent_id": inflight["agent_id"],
                        "systemd_transient_unit": "agentteam-inv-stop-test.service",
                    }
                ),
                encoding="utf-8",
            )

            result = scheduler.stop_inflight_invocations()

            self.assertEqual(result["requested_count"], 1)
            self.assertEqual(result["stopped_count"], 1)
            self.assertEqual(result["results"][0]["stop_status"], "stopped")
            self.assertEqual(stopped[0]["invocation_id"], "INV-stop-test")


    def test_cli_can_run_shell_runtime_adapter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_shell_result.json")
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
                    "--project-root",
                    str(repo),
                    "--shell-command",
                    sys.executable,
                    str(script),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "cli_shell_result.json").exists()
            )


    def test_out_of_scope_runtime_result_is_rejected(self):
        class OutOfScopeRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["outside/generated.txt"],
                    "output": {"note": "intentionally outside write scope"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                tmp_path / "run",
                clock=FixedClock(),
                runtime_adapter=OutOfScopeRuntimeAdapter(),
            )

            self.assertEqual(result["validation_status"], "rejected")
            snapshot = replay_events(tmp_path / "run" / "events.jsonl")
            self.assertNotEqual(snapshot["tasks"]["TASK-001"]["task_status"], "done")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["validation_status"],
                "rejected",
            )


    def test_shell_runtime_adapter_failure_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "fail_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            script.write_text(
                "import sys\nsys.stderr.write('worker failed intentionally')\nsys.exit(17)\n",
                encoding="utf-8",
            )

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "failed")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["validation_status"], "rejected")


    def test_codex_runtime_adapter_reads_last_message_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue((worktree_path / "generated" / "codex_result.json").exists())


    def test_codex_runtime_adapter_uses_staged_provider_result_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex.py"
            _init_git_repo(repo)
            _write_fake_codex(
                fake_codex,
                changed_file="generated/provider_result.json",
            )
            provider_result = (
                repo
                / ".git"
                / "agentteam-provider-io"
                / "attempt"
                / "result.json"
            )
            provider_result.parent.mkdir(parents=True)
            provider_result.write_bytes(b"")
            message = {
                "payload": {
                    "task_id": "TASK-001",
                    "attempt_id": "ATTEMPT-001",
                    "objective": "Use provider-visible result I/O.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                    "provider_result_path": str(provider_result),
                }
            }

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)]
            ).run(message, worktree_path=repo)

            self.assertEqual(result["result_status"], "completed")
            self.assertGreater(provider_result.stat().st_size, 0)
            self.assertEqual(
                json.loads(provider_result.read_text(encoding="utf-8"))[
                    "changed_files"
                ],
                ["generated/provider_result.json"],
            )


    def test_codex_runtime_adapter_rejects_unsafe_provider_result_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            _init_git_repo(repo)
            provider_root = repo / ".git" / "agentteam-provider-io"
            provider_root.mkdir()
            outside = tmp_path / "outside.json"
            outside.write_bytes(b"")
            missing = provider_root / "missing.json"
            symlink = provider_root / "symlink.json"
            symlink.symlink_to(outside)
            invalid_layout = provider_root / "unexpected.json"
            invalid_layout.write_bytes(b"")
            hardlink = provider_root / "attempt" / "result.json"
            hardlink.parent.mkdir()
            hardlink.hardlink_to(outside)

            for candidate in (
                outside,
                missing,
                symlink,
                invalid_layout,
                hardlink,
                Path("relative-result.json"),
            ):
                with self.subTest(candidate=candidate.name):
                    result = CodexRuntimeAdapter(
                        command=["codex", "exec"]
                    ).run(
                        {
                            "payload": {
                                "task_id": "TASK-001",
                                "attempt_id": "ATTEMPT-001",
                                "objective": "Reject unsafe provider I/O.",
                                "read_scope": ["."],
                                "write_scope": [],
                                "provider_result_path": str(candidate),
                            }
                        },
                        worktree_path=repo,
                    )

                    self.assertEqual(result["result_status"], "failed")
                    self.assertEqual(
                        result["output"]["error"],
                        "invalid_provider_result_path",
                    )


    def test_model_invocation_test_provider_start_is_durable_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_start_probe.py"
            _init_git_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import sys",
                        f"authority = pathlib.Path({str(output_dir)!r})",
                        "starts = list(authority.glob('model_invocations/*/started.json'))",
                        "if len(starts) != 1:",
                        "    raise SystemExit(19)",
                        "args = sys.argv[1:]",
                        "sys.stdin.read()",
                        "result_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                        "result_path.parent.mkdir(parents=True, exist_ok=True)",
                        "result_path.write_text(json.dumps({",
                        "    'result_status': 'completed',",
                        "    'changed_files': [],",
                        "    'output': {'start_was_visible': starts[0].is_file()},",
                        "}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                output_dir=output_dir,
            ).run(
                _model_invocation_message(),
                worktree_path=repo,
            )

            invocation = result["output"]["model_invocation"]
            start = json.loads(Path(invocation["started_path"]).read_text(encoding="utf-8"))
            terminal = json.loads(
                Path(invocation["terminal_path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(result["output"]["start_was_visible"])
            self.assertEqual(start["invocation_id"], terminal["invocation_id"])
            self.assertEqual(start["coverage_class"], "not_applicable_adapter")
            self.assertEqual(terminal["usage_status"], "not_applicable")
            _validate_model_invocation_record("model_invocation_started.schema.json", start)
            _validate_model_invocation_record("model_invocation_usage.schema.json", terminal)


    def test_model_invocation_terminalizer_covers_every_controlled_outcome(self):
        statuses = [
            "completed",
            "failed",
            "blocked",
            "cancelled",
            "timed_out",
            "launch_failed",
            "missing_result",
            "invalid_result",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, status in enumerate(statuses, start=1):
                lifecycle = InvocationLifecycle(
                    Path(tmp) / f"case-{index}",
                    _model_invocation_context(
                        coverage_class="not_applicable_adapter",
                        attempt_id=f"ATTEMPT-{index:03d}",
                    ),
                    started_at="2026-07-23T00:00:00Z",
                )
                lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())

                terminal = lifecycle.finalize(
                    status,
                    finished_at="2026-07-23T00:00:01Z",
                )

                self.assertEqual(terminal["terminal_status"], status)
                self.assertEqual(terminal["usage_status"], "not_applicable")
                self.assertTrue(lifecycle.terminal_path.is_file())
                _validate_model_invocation_record(
                    "model_invocation_usage.schema.json",
                    terminal,
                )


    def test_model_invocation_resumed_session_uses_authoritative_delta(self):
        predecessor = "INV-predecessor-001"
        context = _model_invocation_context(
            coverage_class="supported_model_invocation",
            provider_resume_mode="explicit",
            requested_provider_session_id="provider-session-1",
            provider_predecessor_invocation_id=predecessor,
            provider_predecessor_turn_id="turn-1",
            provider_predecessor_usage_snapshot={
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_tokens": 5,
                "total_tokens": 130,
            },
            provider_usage_scope="session_cumulative",
            provider_session_lock_held=True,
            provider_project_binding_valid=True,
            provider_lineage_status="authoritative",
            previous_provider_session_id="provider-session-1",
            previous_invocation_id=predecessor,
            previous_provider_turn_id="turn-1",
        )
        stdout = json.dumps(
            {
                "type": "turn_completed",
                "provider_usage_scope": "session_cumulative",
                "provider_session_id": "provider-session-1",
                "provider_turn_id": "turn-2",
                "provider_predecessor_turn_id": "turn-1",
                "usage": {
                    "input_tokens": 145,
                    "cached_input_tokens": 28,
                    "output_tokens": 42,
                    "reasoning_tokens": 9,
                    "total_tokens": 187,
                },
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = InvocationLifecycle(
                tmp,
                context,
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(_supported_execution_group_identity())

            terminal = lifecycle.finalize(
                "completed",
                stdout=stdout,
                finished_at="2026-07-23T00:00:02Z",
            )

            self.assertEqual(terminal["accounting_method"], "session_delta")
            self.assertEqual(terminal["input_tokens"], 45)
            self.assertEqual(terminal["output_tokens"], 12)
            self.assertEqual(terminal["total_tokens"], 57)
            self.assertEqual(terminal["provider_turn_id"], "turn-2")
            self.assertEqual(
                terminal["provider_predecessor_invocation_id"],
                predecessor,
            )
            _validate_model_invocation_record(
                "model_invocation_usage.schema.json",
                terminal,
            )


    def test_model_invocation_terminal_is_exclusive_and_revocation_fences_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = InvocationLifecycle(
                Path(tmp) / "normal",
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                ),
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            first = lifecycle.finalize(
                "completed",
                finished_at="2026-07-23T00:00:01Z",
            )
            replay = lifecycle.finalize(
                "completed",
                finished_at="2026-07-23T00:00:01Z",
            )
            self.assertEqual(first, replay)
            with self.assertRaises(ModelInvocationIntegrityError):
                lifecycle.finalize(
                    "failed",
                    finished_at="2026-07-23T00:00:01Z",
                )

            revoked = InvocationLifecycle(
                Path(tmp) / "revoked",
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                ),
            )
            revoked.publish_start(ExecutionGroupIdentity.not_applicable())
            revocation = revoked.revoke_writer(
                "LEASE-001",
                revoked_by="recovery-controller",
                reason="worker_process_death_confirmed",
            )
            replayed_revocation = revoked.revoke_writer(
                "LEASE-001",
                revoked_by="recovery-controller",
                reason="worker_process_death_confirmed",
            )
            self.assertEqual(revocation, replayed_revocation)
            with self.assertRaises(ModelInvocationWriterRevoked):
                revoked.finalize("failed")
            self.assertFalse(revoked.terminal_path.exists())


    def test_model_invocation_systemd_resolves_provider_before_service_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = InvocationLifecycle(
                tmp,
                _model_invocation_context(
                    coverage_class="supported_model_invocation",
                ),
            )
            with mock.patch(
                "agentteam_runtime.model_invocation.shutil.which",
                return_value="/home/test/.local/bin/codex",
            ):
                execution = SystemdGatedExecution(
                    lifecycle,
                    ["codex", "exec", "--json"],
                    cwd=tmp,
                    input_text="bounded prompt",
                    timeout_seconds=30,
                )

            self.assertEqual(
                execution.command,
                ["/home/test/.local/bin/codex", "exec", "--json"],
            )


    def test_model_invocation_systemd_persists_exact_process_group_identity(self):
        commands = []

        def command_runner(command, **kwargs):
            commands.append(command)
            if command[0] == "loginctl":
                return subprocess.CompletedProcess(command, 0, "yes\n", "")
            if command[0] == "systemd-run":
                return subprocess.CompletedProcess(command, 0, "", "")
            if f"user@{os.getuid()}.service" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "InvocationID=" + ("a" * 32) + "\n"
                    "ControlGroup=/user.slice/user-service\n"
                    "KillMode=mixed\n",
                    "",
                )
            if any(part.startswith("agentteam-inv-") for part in command):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "InvocationID=" + ("b" * 32) + "\n"
                    "ControlGroup=/user.slice/transient-service\n"
                    "KillMode=control-group\n"
                    "MainPID=321\n",
                    "",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                "ManagerTimestampMonotonic=998877\n",
                "",
            )

        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = InvocationLifecycle(
                tmp,
                _model_invocation_context(
                    coverage_class="supported_model_invocation",
                ),
            )
            execution = SystemdGatedExecution(
                lifecycle,
                ["codex", "exec", "--json"],
                cwd=tmp,
                input_text="bounded prompt",
                timeout_seconds=30,
                command_runner=command_runner,
            )
            with (
                mock.patch(
                    "agentteam_runtime.model_invocation._wait_for_json",
                    return_value={"pid": 321, "pgid": 321},
                ),
                mock.patch(
                    "agentteam_runtime.model_invocation._read_boot_id",
                    return_value="12345678-1234-1234-1234-123456789abc",
                ),
                mock.patch(
                    "agentteam_runtime.model_invocation._proc_start_ticks",
                    return_value=4567,
                ),
            ):
                identity = execution.prepare()

            self.assertFalse(lifecycle.started_path.exists())
            self.assertEqual(identity.gated_supervisor_pid, 321)
            self.assertEqual(identity.gated_supervisor_pgid, 321)
            self.assertEqual(identity.systemd_transient_kill_mode, "control-group")
            self.assertEqual(
                identity.systemd_transient_control_group,
                "/user.slice/transient-service",
            )
            self.assertTrue(
                any(
                    "--no-block" in command
                    and "--property=RemainAfterExit=yes" in command
                    and "--property=KillMode=control-group" in command
                    for command in commands
                )
            )
            start = lifecycle.publish_start(identity)
            self.assertEqual(
                start["launch_nonce_sha256"],
                hashlib.sha256(execution.nonce.encode("ascii")).hexdigest(),
            )
            _validate_model_invocation_record(
                "model_invocation_started.schema.json",
                start,
            )


    def test_model_invocation_execution_fence_fails_closed_on_ambiguity(self):
        start = {
            **_model_invocation_context(
                coverage_class="supported_model_invocation",
            ),
            **{
                name: getattr(_supported_execution_group_identity(), name)
                for name in ExecutionGroupIdentity.__dataclass_fields__
            },
        }
        user_service = {
            "InvocationID": start["systemd_user_service_invocation_id"],
            "ControlGroup": start["systemd_user_service_control_group"],
            "KillMode": start["systemd_user_service_kill_mode"],
            "ActiveState": "active",
        }
        transient = {
            "InvocationID": start["systemd_transient_invocation_id"],
            "ControlGroup": start["systemd_transient_control_group"],
            "KillMode": "control-group",
        }

        changed_boot = assess_execution_group_fence(
            start,
            current_boot_id="87654321-4321-4321-4321-cba987654321",
            current_user_service=None,
            current_manager_identity=None,
            current_transient_service=None,
        )
        self.assertEqual(changed_boot["fence_status"], "death_proven")
        self.assertFalse(changed_boot["signal_allowed"])

        restarted_service = {
            **user_service,
            "InvocationID": "d" * 32,
        }
        enclosing_restart = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service=restarted_service,
            current_manager_identity="new-manager",
            current_transient_service=None,
        )
        self.assertEqual(enclosing_restart["fence_status"], "death_proven")
        self.assertEqual(
            enclosing_restart["proof"],
            "enclosing_user_service_restarted",
        )

        def esrch_pidfd_open(pid, flags):
            raise OSError(errno.ESRCH, "reaped")

        reaped_empty = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service=user_service,
            current_manager_identity=start["systemd_user_manager_identity"],
            current_transient_service=transient,
            pidfd_open=esrch_pidfd_open,
            cgroup_populated=lambda control_group: False,
        )
        self.assertEqual(reaped_empty["fence_status"], "death_proven")
        self.assertEqual(reaped_empty["proof"], "exact_transient_cgroup_empty")

        reaped_populated = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service=user_service,
            current_manager_identity=start["systemd_user_manager_identity"],
            current_transient_service=transient,
            pidfd_open=esrch_pidfd_open,
            cgroup_populated=lambda control_group: True,
        )
        self.assertEqual(
            reaped_populated["fence_status"],
            "exact_service_stop_required",
        )
        self.assertFalse(reaped_populated["signal_allowed"])

        pidfd = os.open("/dev/null", os.O_RDONLY)
        live = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service=user_service,
            current_manager_identity=start["systemd_user_manager_identity"],
            current_transient_service=transient,
            pidfd_open=lambda pid, flags: pidfd,
            process_start_ticks=lambda pid: start["gated_supervisor_start_ticks"],
        )
        self.assertEqual(live["fence_status"], "live_pinned")
        self.assertTrue(live["signal_allowed"])
        self.assertEqual(live["pidfd"], pidfd)
        os.close(pidfd)

        recycled_pidfd = os.open("/dev/null", os.O_RDONLY)
        recycled = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service=user_service,
            current_manager_identity=start["systemd_user_manager_identity"],
            current_transient_service=transient,
            pidfd_open=lambda pid, flags: recycled_pidfd,
            process_start_ticks=lambda pid: (
                start["gated_supervisor_start_ticks"] + 1
            ),
        )
        self.assertEqual(recycled["fence_status"], "open_ambiguous")
        self.assertEqual(
            recycled["proof"],
            "pinned_process_start_identity_mismatch",
        )
        self.assertFalse(recycled["signal_allowed"])
        with self.assertRaises(OSError):
            os.fstat(recycled_pidfd)

        ambiguous_manager = assess_execution_group_fence(
            start,
            current_boot_id=start["host_boot_id"],
            current_user_service=user_service,
            current_manager_identity="unexplained-new-manager",
            current_transient_service=transient,
        )
        self.assertEqual(
            ambiguous_manager["fence_status"],
            "open_ambiguous",
        )
        self.assertFalse(ambiguous_manager["signal_allowed"])


    def test_codex_runtime_adapter_calls_progress_callback_while_subprocess_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_slow.py"
            _init_git_repo(repo)
            fake_codex.write_text(
                "\n".join(
                    [
                        "import json",
                        "import pathlib",
                        "import sys",
                        "import time",
                        "args = sys.argv[1:]",
                        "sys.stdin.read()",
                        "time.sleep(0.2)",
                        "output_path = pathlib.Path(args[args.index('--output-last-message') + 1])",
                        "worktree = pathlib.Path(args[args.index('-C') + 1])",
                        "target = worktree / 'generated' / 'codex_progress_result.json'",
                        "target.parent.mkdir(parents=True, exist_ok=True)",
                        "target.write_text('{}', encoding='utf-8')",
                        "output_path.parent.mkdir(parents=True, exist_ok=True)",
                        "output_path.write_text(json.dumps({",
                        "    'result_status': 'completed',",
                        "    'changed_files': ['generated/codex_progress_result.json'],",
                        "    'output': {'adapter': 'codex'},",
                        "}), encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            message = {
                "payload": {
                    "task_id": "TASK-001",
                    "attempt_id": "ATTEMPT-001",
                    "objective": "Exercise Codex progress callback.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                }
            }
            progress_calls = []

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                progress_interval_seconds=0.05,
            ).run(
                message,
                worktree_path=repo,
                progress_callback=lambda: progress_calls.append(time.monotonic()),
            )

            self.assertEqual(result["result_status"], "completed")
            self.assertGreaterEqual(len(progress_calls), 1)


    def test_codex_runtime_adapter_converts_sandbox_failure_to_permission_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_denied.py"
            _init_git_repo(repo)
            fake_codex.write_text(
                "import sys\n"
                "sys.stdin.read()\n"
                "sys.stderr.write('Operation not permitted by sandbox\\n')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            message = {
                "message_id": "MSG-0001",
                "from_agent": "agent-scheduler",
                "to_agent": "agent-repo-map",
                "message_type": "dispatch_task",
                "correlation_id": "TASK-001:TASK-001-ATTEMPT-001",
                "created_at": "2026-06-03T00:00:00Z",
                "lease_expires_at": "2026-06-03T00:15:00Z",
                "payload": {
                    "task_id": "TASK-001",
                    "attempt_id": "TASK-001-ATTEMPT-001",
                    "lease_id": "TASK-001-LEASE-001",
                    "objective": "Need a sandbox permission.",
                    "read_scope": ["."],
                    "write_scope": ["generated/"],
                },
            }

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                sandbox="workspace-write",
            ).run(message, worktree_path=repo)

            self.assertEqual(result["result_status"], "blocked")
            self.assertEqual(result["output"]["error"], "permission_required")
            self.assertEqual(
                result["output"]["permission_request"]["requested_capability"],
                "sandbox_escalation",
            )
            self.assertEqual(result["output"]["permission_request"]["sandbox"], "workspace-write")
            self.assertIn("Operation not permitted", result["output"]["permission_request"]["reason"])


    def test_codex_runtime_adapter_includes_role_prompt_contract(self):
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-repo-map",
            "message_type": "dispatch_task",
            "correlation_id": "TASK-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "TASK-001",
                "attempt_id": "ATTEMPT-001",
                "lease_id": "LEASE-001",
                "objective": "Implement a bounded change.",
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "agent_role": "repo_map_agent",
                "role_prompt_contract": {
                    "role_summary": "Implement bounded repository edits.",
                    "instructions": ["Inspect read_scope before writing."],
                    "required_output_keys": ["evidence"],
                },
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("Role prompt contract:", prompt)
        self.assertIn("Implement bounded repository edits.", prompt)
        self.assertIn("Inspect read_scope before writing.", prompt)
        self.assertIn("required_output_keys", prompt)
        self.assertIn("operator_summary", prompt)
        self.assertIn("what_changed", prompt)
        self.assertIn("operator_summary natural-language fields must be written in Chinese", prompt)
        self.assertIn(
            "verification_additions command runs from the repository root",
            prompt,
        )


    def test_codex_runtime_adapter_includes_evidence_policy_contract(self):
        message = {
            "message_id": "MSG-0001",
            "from_agent": "agent-scheduler",
            "to_agent": "agent-repo-map",
            "message_type": "dispatch_task",
            "correlation_id": "TASK-001:ATTEMPT-001",
            "created_at": "2026-06-03T00:00:00Z",
            "lease_expires_at": "2026-06-03T00:15:00Z",
            "payload": {
                "task_id": "TASK-001",
                "attempt_id": "ATTEMPT-001",
                "lease_id": "LEASE-001",
                "objective": "Implement a bounded change.",
                "read_scope": ["."],
                "write_scope": ["generated/"],
                "evidence_policy": {
                    "evidence_level": "L2",
                    "integration_requires_complete_evidence": True,
                    "required_result_key": "evidence_summary",
                    "required_fields": [
                        "evidence_level",
                        "evidence_status",
                        "trace_carrier",
                        "missing_evidence",
                    ],
                },
            },
        }

        prompt = CodexRuntimeAdapter(command=["codex", "exec"])._build_prompt(message)

        self.assertIn("Evidence policy:", prompt)
        self.assertIn("output.evidence_summary", prompt)
        self.assertIn("trace_carrier", prompt)
        self.assertIn("trace_carrier must be a list of objects", prompt)
        self.assertIn("missing_evidence", prompt)
        self.assertIn("L2", prompt)


    def test_codex_runtime_adapter_missing_last_message_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_no_result.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            fake_codex.write_text("import sys\nsys.stdin.read()\nprint('no result file')\n", encoding="utf-8")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=CodexRuntimeAdapter(command=[sys.executable, str(fake_codex)]),
            )

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(snapshot["attempts"]["ATTEMPT-001"]["attempt_status"], "failed")


    def test_codex_runtime_adapter_default_exec_flags_match_current_cli(self):
        command = CodexRuntimeAdapter(command=["codex", "exec"])._build_command(
            "/tmp/worktree",
            "/tmp/result.json",
        )

        self.assertIn("-C", command)
        self.assertIn("-s", command)
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertNotIn("-a", command)
        self.assertNotIn("--ask-for-approval", command)


    def test_codex_runtime_adapter_can_build_explicit_resume_command(self):
        command = CodexRuntimeAdapter(
            command=["codex", "exec"],
            model="medium",
            resume_session_id="SESSION-123",
        )._build_command(
            "/tmp/worktree",
            "/tmp/result.json",
        )

        self.assertEqual(command[0:3], ["codex", "exec", "resume"])
        self.assertIn("SESSION-123", command)
        self.assertEqual(_arg_value(command, "-m"), "medium")
        self.assertIn("--json", command)
        self.assertEqual(_arg_value(command, "--output-last-message"), "/tmp/result.json")
        self.assertEqual(command[-1], "-")
        self.assertNotIn("-C", command)
        self.assertNotIn("-s", command)


    def test_cli_can_run_codex_runtime_adapter_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_cli.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_cli_result.json")
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
                    "--project-root",
                    str(repo),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "codex_cli_result.json").exists()
            )


    def test_cli_can_select_codex_runtime_with_command_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_runtime.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file="generated/codex_runtime_result.json")
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
                    "--project-root",
                    str(repo),
                    "--runtime",
                    "codex",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "CodexRuntimeAdapter",
            )
            self.assertTrue(
                (Path(summary["worktree_path"]) / "generated" / "codex_runtime_result.json").exists()
            )


    def test_cli_rejects_codex_runtime_without_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
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
                    "--runtime",
                    "codex",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("--project-root is required when --runtime codex is set", completed.stderr)


    def test_cli_passes_codex_runtime_options_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_options.py"
            target_file = "generated/codex_runtime_options.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)
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
                    "--project-root",
                    str(repo),
                    "--runtime",
                    "codex",
                    "--codex-model",
                    "gpt-test-model",
                    "--codex-sandbox",
                    "read-only",
                    "--codex-timeout-seconds",
                    "30",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            recorded = json.loads(
                (Path(summary["worktree_path"]) / target_file).read_text(encoding="utf-8")
            )

            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(recorded["model"], "gpt-test-model")
            self.assertEqual(recorded["sandbox"], "read-only")


    def test_cli_uses_agent_runtime_profile_for_codex_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_profile.py"
            target_file = "generated/codex_agent_profile.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_runtime_profile(
                agent_pool_path,
                runtime_profile={
                    "adapter": "codex",
                    "model": "agent-profile-model",
                    "sandbox": "read-only",
                    "timeout_seconds": 30,
                },
            )
            _write_fake_codex_arg_recorder(fake_codex, changed_file=target_file)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.cli",
                    "--agent-pool",
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--project-root",
                    str(repo),
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            recorded = json.loads(
                (Path(summary["worktree_path"]) / target_file).read_text(encoding="utf-8")
            )

            self.assertEqual(summary["validation_status"], "accepted")
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "CodexRuntimeAdapter",
            )
            self.assertEqual(recorded["model"], "agent-profile-model")
            self.assertEqual(recorded["sandbox"], "read-only")

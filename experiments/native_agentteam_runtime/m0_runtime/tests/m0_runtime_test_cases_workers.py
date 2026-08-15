try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class WorkersMixin:
    def test_file_daemon_tick_records_worker_registry_and_processes_one_task(self):
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

            daemon = FileSchedulerDaemon(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            summary = daemon.tick()

            registry = json.loads(
                (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["daemon_status"], "running")
            self.assertEqual(summary["tick_status"], "processed")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(
                summary["worker_registry_path"],
                str(output_dir / "state" / "worker_registry.json"),
            )
            self.assertEqual(registry["tick_count"], 1)
            self.assertEqual(registry["registry_status"], "active")
            self.assertEqual(
                [worker["agent_id"] for worker in registry["workers"]],
                ["agent-repo-map", "agent-worker-1"],
            )
            self.assertEqual(
                {worker["worker_status"] for worker in registry["workers"]},
                {"idle"},
            )
            self.assertTrue((output_dir / "steps" / "STEP-0001-TASK-001").exists())


    def test_file_daemon_run_until_idle_reuses_worker_registry_across_ticks(self):
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

            summary = run_file_daemon(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                runtime_adapter=FakeRuntimeAdapter(),
            )
            registry = json.loads(
                (output_dir / "state" / "worker_registry.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["step_count"], 2)
            self.assertEqual(summary["tick_count"], 3)
            self.assertEqual(registry["tick_count"], 3)
            self.assertEqual(registry["registry_status"], "active")


    def test_cli_can_run_file_daemon_until_idle(self):
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
                    "--daemon-run-until-idle",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue((output_dir / "state" / "worker_registry.json").exists())


    def test_two_phase_worker_verification_addition_runs_after_primary_verification(self):
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
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/two_phase_commit.json').exists()",
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "two_phase_commit.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"attempt_id": inflight["attempt_id"], "ok": True}),
                encoding="utf-8",
            )
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/two_phase_commit.json"],
                {
                    "operator_summary": {
                        "what_changed": ["写入了可由新增测试验证的集成文件。"],
                        "measured_result": ["新增验证命令应在集成 worktree 中通过。"],
                        "verification_summary": ["worker 建议运行新增的结构化验证。"],
                        "merge_recommendation": "如果固定验证和新增验证均通过，可以合并。",
                        "next_steps": ["无需额外处理。"],
                    },
                    "verification_additions": [
                        {
                            "label": "worker-added-json-check",
                            "command": [
                                sys.executable,
                                "-c",
                                "import json, pathlib; assert json.loads(pathlib.Path('generated/two_phase_commit.json').read_text())['ok'] is True",
                            ],
                            "reason": "确认 worker 生成文件的语义内容。",
                        }
                    ],
                },
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            report = _operator_task_report({"task_id": "TASK-001"}, result)
            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")
            snapshot_item = snapshot["integration_queue"][
                "TASK-001:TASK-001-ATTEMPT-001"
            ]

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_verification_additions_status"], "passed")
            self.assertEqual(
                result["integration_verification_additions"][0]["label"],
                "worker-added-json-check",
            )
            self.assertEqual(
                result["integration_verification_additions"][0][
                    "verification_addition_status"
                ],
                "passed",
            )
            self.assertEqual(
                report["integration"],
                "passed with 1 worker verification addition(s)",
            )
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertNotEqual(result["integration_commit_sha"], None)
            self.assertEqual(snapshot_item["queue_status"], "committed")
            self.assertTrue(
                (integration_worktree / "generated" / "two_phase_commit.json").exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)


    def test_two_phase_recovers_explicit_verification_deferred_patch(self):
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
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/deferred.json').exists()",
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "deferred.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ok": True}), encoding="utf-8")
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "failed",
                ["generated/deferred.json"],
                {
                    "verification_deferred": True,
                    "verification_deferred_reason": "tool_budget_exhausted",
                    "operator_summary": {
                        "what_changed": ["完成候选文件，等待 controller 验证。"],
                        "measured_result": ["worker 工具额度在验证前耗尽。"],
                        "verification_summary": ["验证已明确延后。"],
                        "merge_recommendation": "仅在 controller 验证通过后合并。",
                        "next_steps": ["运行结构化补充验证。"],
                    },
                    "verification_additions": [
                        {
                            "label": "deferred-json-check",
                            "command": [
                                sys.executable,
                                "-c",
                                "import json, pathlib; assert json.loads(pathlib.Path('generated/deferred.json').read_text())['ok'] is True",
                            ],
                            "reason": "验证延后候选的语义内容。",
                        }
                    ],
                },
            )

            result = scheduler.collect_ready_results()["results"][0]
            event_types = {
                event["event_type"]
                for event in _read_jsonl_for_test(output_dir / "events.jsonl")
            }

            self.assertEqual(result["worker_result_status"], "failed")
            self.assertEqual(
                result["controller_verification_recovery"]["recovery_status"],
                "controller_verification_required",
            )
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(
                result["integration_verification_additions_status"], "passed"
            )
            self.assertEqual(result["task_status"], "done")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertIn("worker_verification_deferred", event_types)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)


    def test_two_phase_rejects_unverifiable_deferred_failure(self):
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
                integrate_accepted_patch=True,
                integration_verification_command=[sys.executable, "-c", "pass"],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "unverified.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "failed",
                ["generated/unverified.json"],
                {
                    "verification_deferred": True,
                    "verification_deferred_reason": "tool_budget_exhausted",
                    "operator_summary": {
                        "what_changed": ["写入未验证候选。"],
                        "measured_result": ["没有补充验证命令。"],
                        "verification_summary": ["验证不完整。"],
                        "merge_recommendation": "不要合并。",
                        "next_steps": ["补充验证。"],
                    },
                    "verification_additions": [],
                },
            )

            result = scheduler.collect_ready_results()["results"][0]

            self.assertIsNone(result["controller_verification_recovery"])
            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(result["integration_status"], "not_requested")
            self.assertNotEqual(result["task_status"], "done")


    def test_two_phase_worker_verification_addition_failure_blocks_baseline(self):
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
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/failing_added_check.json').exists()",
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "failing_added_check.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ok": False}), encoding="utf-8")
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/failing_added_check.json"],
                {
                    "operator_summary": {
                        "what_changed": ["写入了一个会被新增验证拦截的文件。"],
                        "measured_result": ["新增验证命令应失败。"],
                        "verification_summary": ["固定验证通过，新增验证失败。"],
                        "merge_recommendation": "新增验证失败时不应合并。",
                        "next_steps": ["修复新增验证覆盖的问题。"],
                    },
                    "verification_additions": [
                        {
                            "label": "worker-added-failing-check",
                            "command": [
                                sys.executable,
                                "-c",
                                "import sys; sys.exit(9)",
                            ],
                            "reason": "模拟 worker 新增测试失败。",
                        }
                    ],
                },
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            report = _operator_task_report({"task_id": "TASK-001"}, result)
            baseline_worktree = Path(result["integration_baseline_worktree_path"])
            blocked = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line)["event_type"] == "integration_blocked"
            ]

            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(
                result["integration_verification_failure_reason"],
                "verification_addition_failed",
            )
            self.assertEqual(result["integration_verification_additions_status"], "failed")
            self.assertEqual(
                result["integration_verification_additions"][0][
                    "verification_addition_exit_code"
                ],
                9,
            )
            self.assertEqual(
                report["integration"],
                "failed: failed verification addition worker-added-failing-check",
            )
            self.assertEqual(result["integration_baseline_commit_status"], "skipped")
            self.assertEqual(result["integration_baseline_commit_reason"], "verification_failed")
            self.assertEqual(_git_rev_parse(repo, "agentteam/run/run/integration"), source_head)
            self.assertEqual(_git_rev_parse(baseline_worktree, "HEAD"), source_head)
            self.assertFalse((baseline_worktree / "generated" / "failing_added_check.json").exists())
            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(blocked[0]["payload"]["block_reason"], "verification_failed")
            self.assertEqual(result["task_status"], "blocked")
            self.assertFalse(result["retryable"])
            self.assertFalse(result["retry_allowed"])
            self.assertEqual(result["retry_decision"]["action"], "review_required")
            self.assertEqual(scheduler.dispatch_ready()["dispatch_count"], 0)
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertFalse(
                any(event["event_type"] == "recovery_routed" for event in events)
            )


    def test_two_phase_worker_verification_addition_rejects_unallowed_command(self):
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
                tasks=[_backlog_task("TASK-001", write_scope=["generated/"])],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/rejected_added_check.json').exists()",
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "rejected_added_check.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ok": True}), encoding="utf-8")
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/rejected_added_check.json"],
                {
                    "operator_summary": {
                        "what_changed": ["写入了一个带非法新增验证命令的文件。"],
                        "measured_result": ["调度器应拒绝 shell 命令。"],
                        "verification_summary": ["固定验证通过，新增验证被拒绝。"],
                        "merge_recommendation": "新增验证命令被拒绝时不应合并。",
                        "next_steps": ["改为结构化 Python 测试命令。"],
                    },
                    "verification_additions": [
                        {
                            "label": "worker-added-shell-check",
                            "command": ["bash", "-lc", "exit 0"],
                            "reason": "模拟不允许的 shell 命令。",
                        }
                    ],
                },
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            report = _operator_task_report({"task_id": "TASK-001"}, result)
            baseline_worktree = Path(result["integration_baseline_worktree_path"])

            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(
                result["integration_verification_failure_reason"],
                "verification_addition_rejected",
            )
            self.assertEqual(result["integration_verification_additions_status"], "rejected")
            self.assertEqual(
                result["integration_verification_additions"][0][
                    "verification_addition_status"
                ],
                "rejected",
            )
            self.assertIn(
                "not allowed",
                result["integration_verification_additions"][0][
                    "verification_addition_rejection_reason"
                ],
            )
            self.assertEqual(
                report["integration"],
                "failed: rejected verification addition worker-added-shell-check",
            )
            self.assertEqual(result["integration_baseline_commit_status"], "skipped")
            self.assertEqual(_git_rev_parse(repo, "agentteam/run/run/integration"), source_head)
            self.assertEqual(_git_rev_parse(baseline_worktree, "HEAD"), source_head)


    def test_cli_can_run_file_daemon_with_static_long_running_worker_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_ids(
                agent_pool_path,
                ["agent-repo-map", "agent-doc-map"],
            )
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
                    "--daemon-run-until-idle",
                    "--daemon-long-running-worker-pool",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            process_registry = json.loads(
                Path(summary["worker_pool"]["process_registry_path"]).read_text(
                    encoding="utf-8"
                )
            )
            repo_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
            self.assertEqual(summary["worker_pool"]["worker_count"], 2)
            self.assertEqual(process_registry["registry_status"], "stopped")
            self.assertEqual(
                {worker["worker_agent_id"] for worker in summary["worker_pool"]["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(repo_outbox.exists())


    def test_cli_can_run_two_phase_scheduler_with_static_worker_pool(self):
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
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--max-inflight",
                    "2",
                    "--max-attempts",
                    "2",
                    "--lease-timeout-seconds",
                    "900",
                    "--worker-max-restart-count",
                    "1",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertCountEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["inflight_count"], 0)
            self.assertEqual(summary["max_attempts"], 2)
            self.assertEqual(summary["lease_timeout_seconds"], 900)
            self.assertEqual(summary["worker_pool"]["pool_status"], "stopped")
            self.assertEqual(summary["worker_pool"]["worker_count"], 2)
            self.assertEqual(summary["worker_pool_health"]["pool_status"], "running")
            self.assertEqual(summary["worker_pool_health"]["max_restart_count"], 1)
            self.assertGreaterEqual(len(summary["worker_pool_supervision"]), 1)
            self.assertIn("restart_count", summary["worker_pool_health"]["workers"][0])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {"TASK-001": "done", "TASK-002": "done"},
            )


    def test_supervised_two_phase_scheduler_waits_past_max_steps_for_running_inflight_worker(self):
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

            class DelayedResultWorkerPool:
                def __init__(self):
                    self.calls = 0
                    self.result_written = False

                def supervise_once(self):
                    self.calls += 1
                    self._write_result_after_dispatch()
                    health = self.health_check()
                    return {
                        "supervision_status": health["pool_status"],
                        "restarted_count": 0,
                        "before": health,
                        "restart": {"restarted_count": 0},
                        "after": health,
                    }

                def health_check(self):
                    return {
                        "pool_status": "running",
                        "workers": [
                            {
                                "worker_agent_id": "agent-repo-map",
                                "worker_status": "running",
                            }
                        ],
                    }

                def _write_result_after_dispatch(self):
                    if self.result_written or self.calls <= 10:
                        return
                    state_path = output_dir / "state" / "two_phase_scheduler_state.json"
                    if not state_path.exists():
                        return
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    inflight = state.get("inflight_attempts", [])
                    if not inflight:
                        return
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
                max_steps=1,
            )

            result = _run_supervised_two_phase_scheduler(
                args,
                integration_verification_command=None,
                worker_pool=DelayedResultWorkerPool(),
                notification_sink=None,
            )

            self.assertEqual(result["scheduler_status"], "idle")
            self.assertGreater(result["tick_count"], 1)
            self.assertEqual(result["processed_task_ids"], ["TASK-001"])
            self.assertEqual(result["inflight_count"], 0)
            self.assertGreater(
                result["worker_pool_supervision_observation_count"],
                MAX_RETAINED_SUPERVISION_SNAPSHOTS,
            )
            self.assertEqual(
                len(result["worker_pool_supervision"]),
                MAX_RETAINED_SUPERVISION_SNAPSHOTS,
            )


    def test_cli_two_phase_worker_pool_can_auto_decompose_with_fake_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
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
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M21",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--max-steps",
                    "10",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertIn("DECOMPOSE-M21-001", summary["processed_task_ids"])
            self.assertIn("TASK-M21-GENERATED-001", summary["processed_task_ids"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {
                    "DECOMPOSE-M21-001": "done",
                    "TASK-M21-GENERATED-001": "done",
                },
            )


    def test_cli_two_phase_worker_pool_can_auto_decompose_with_fake_codex_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            fake_codex = tmp_path / "fake_codex_planner_and_worker.py"
            _init_git_repo(repo)
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
                ],
            )
            _write_fake_codex_planner_and_worker(fake_codex)
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
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M23",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            (
                                "import pathlib; assert pathlib.Path("
                                "'generated/codex_generated_worker.json').exists()"
                            ),
                        ]
                    ),
                    "--commit-verified-integration",
                    "--runtime",
                    "codex",
                    "--max-steps",
                    "10",
                    "--codex-command",
                    sys.executable,
                    str(fake_codex),
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertIn("DECOMPOSE-M23-001", summary["processed_task_ids"])
            self.assertIn("TASK-M23-CODEX-001", summary["processed_task_ids"])
            self.assertEqual(
                {task["task_id"]: task["task_status"] for task in state["tasks"]},
                {
                    "DECOMPOSE-M23-001": "done",
                    "TASK-M23-CODEX-001": "done",
                },
            )
            self.assertEqual(_git_status_short(repo), "")


    def test_cli_two_phase_worker_pool_accepts_planner_context_artifact_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            artifact = tmp_path / "roadmap.md"
            artifact.write_text(
                "\n".join(
                    [
                        "# Roadmap",
                        "Selected CLI artifact.",
                        "## M24",
                        *["bounded context line" for _ in range(20)],
                        "CLI_TAIL_MARKER_SHOULD_NOT_BE_EMBEDDED",
                    ]
                ),
                encoding="utf-8",
            )
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [
                    ("agent-planner", "task_planner"),
                    ("agent-repo-map", "repo_map_agent"),
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
                    str(agent_pool_path),
                    "--backlog",
                    str(backlog_path),
                    "--output-dir",
                    str(output_dir),
                    "--daemon-run-until-idle",
                    "--daemon-two-phase-worker-pool",
                    "--auto-decompose-backlog",
                    "--decomposition-milestone-id",
                    "M24",
                    "--decomposition-planner-role",
                    "task_planner",
                    "--decomposition-default-worker-role",
                    "repo_map_agent",
                    "--planner-context-artifact",
                    str(artifact),
                    "--planner-context-excerpt-chars",
                    "80",
                    "--max-steps",
                    "10",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            context = json.loads(
                (
                    output_dir
                    / "planner_contexts"
                    / "DECOMPOSE-M24-001.json"
                ).read_text(encoding="utf-8")
            )
            source = context["artifact_context"]["sources"][0]

            self.assertEqual(source["path"], str(artifact))
            self.assertEqual(source["headings"], ["Roadmap", "M24"])
            self.assertLessEqual(source["excerpt_chars"], 80)
            self.assertNotIn(
                "CLI_TAIL_MARKER_SHOULD_NOT_BE_EMBEDDED",
                json.dumps(context["artifact_context"], sort_keys=True),
            )


    def test_worker_invocation_inventory_routes_explicit_stage_and_session_context(self):
        from agentteam_runtime.two_phase_scheduler import (
            _worker_usage_stage,
            supported_worker_invocation_inventory,
        )

        snapshot = {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "reasoning_tokens": 5,
            "total_tokens": 130,
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            task = _backlog_task(
                "TASK-SEMANTIC-001",
                write_scope=[],
                required_role="semantic_architecture_agent",
            )
            task.update(
                {
                    "requested_provider_session_id": "provider-session-1",
                    "provider_predecessor_invocation_id": "INV-predecessor-1",
                    "provider_predecessor_turn_id": "turn-1",
                    "provider_predecessor_usage_snapshot": snapshot,
                    "experiment_controller_reference": {
                        "schema_version": (
                            "experiment_budget_controller_reference.v1"
                        )
                    },
                    "experiment_controller_required": True,
                }
            )
            backlog_path = _write_backlog(tmp_path, write_scope=[], tasks=[task])
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [("agent-semantic", "semantic_architecture_agent")],
            )
            controller = create_experiment_controller(
                output_dir,
                protocol_id="worker-invocation-inventory",
                max_total_tokens=1000,
                max_wall_time_seconds=3600,
                soft_warning_ratio=0.8,
                scored=True,
            )
            scheduler = TwoPhaseFileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                experiment_controller_reference=controller.reference,
                experiment_controller_required=True,
            )

            scheduler.dispatch_ready()

            message = _read_first_jsonl(
                output_dir
                / "steps"
                / "STEP-0001-TASK-SEMANTIC-001"
                / "mailboxes"
                / "agent-semantic"
                / "inbox.jsonl"
            )
            payload = message["payload"]
            inventory = supported_worker_invocation_inventory()
            worker_routes = [
                route for route in inventory if route["route"] == "worker"
            ]
            self.assertEqual(
                len({route["role"] for route in worker_routes}),
                len(worker_routes),
            )
            for route in worker_routes:
                self.assertEqual(
                    _worker_usage_stage(route["role"], None),
                    route["usage_stage"],
                )
            self.assertEqual(
                _worker_usage_stage("implementation_worker", "decompose_backlog"),
                "planner_or_task_slicer",
            )
            self.assertIn(
                {
                    "route": "worker",
                    "role": "semantic_architecture_agent",
                    "usage_stage": "semantic_architecture",
                },
                inventory,
            )
            self.assertEqual(payload["usage_stage"], "semantic_architecture")
            self.assertEqual(payload["agent_role"], "semantic_architecture_agent")
            self.assertEqual(
                payload["runtime_execution_session_id"],
                "SESSION-TASK-SEMANTIC-001-ATTEMPT-001",
            )
            self.assertEqual(payload["lifecycle_owner_token"], payload["lease_id"])
            self.assertEqual(payload["provider_resume_mode"], "explicit")
            self.assertTrue(payload["experiment_controller_required"])
            self.assertEqual(
                payload["experiment_controller_reference"],
                controller.reference,
            )
            self.assertEqual(
                json.dumps(
                    payload["provider_predecessor_usage_snapshot"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
            )
            step_dir = (
                output_dir / "steps" / "STEP-0001-TASK-SEMANTIC-001"
            )
            authority_files = list(
                (
                    step_dir
                    / "state"
                    / "mailbox_dispatch_authority"
                ).glob("*.json")
            )
            self.assertEqual(len(authority_files), 1)
            from agentteam_runtime.two_phase_scheduler import (
                _write_dispatch_authority,
            )

            _write_dispatch_authority(step_dir, message)
            crash_step_dir = tmp_path / "crash-step"
            crash_message = {
                "message_id": "MSG-CRASH-RECOVERY",
                "payload": {"objective": "prove atomic publication"},
            }
            with mock.patch(
                "agentteam_runtime.two_phase_scheduler.os.link",
                side_effect=OSError("simulated publication crash"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "simulated publication crash",
                ):
                    _write_dispatch_authority(
                        crash_step_dir,
                        crash_message,
                    )
            crash_authority_dir = (
                crash_step_dir
                / "state"
                / "mailbox_dispatch_authority"
            )
            self.assertEqual(list(crash_authority_dir.iterdir()), [])
            _write_dispatch_authority(crash_step_dir, crash_message)
            self.assertEqual(
                len(list(crash_authority_dir.glob("*.json"))),
                1,
            )
            fifo_step_dir = tmp_path / "fifo-step"
            fifo_message = {
                "message_id": "MSG-FIFO-AUTHORITY",
                "payload": {"objective": "reject non-regular authority"},
            }
            fifo_authority_dir = (
                fifo_step_dir
                / "state"
                / "mailbox_dispatch_authority"
            )
            fifo_authority_dir.mkdir(parents=True)
            fifo_authority_path = fifo_authority_dir / (
                hashlib.sha256(
                    fifo_message["message_id"].encode("utf-8")
                ).hexdigest()
                + ".json"
            )
            os.mkfifo(fifo_authority_path)
            with self.assertRaisesRegex(
                RuntimeError,
                "conflicts with retry",
            ):
                _write_dispatch_authority(
                    fifo_step_dir,
                    fifo_message,
                )

            message["payload"].pop("experiment_controller_reference")
            message["payload"].pop("experiment_controller_required")
            message["payload"].pop("experiment_authority_root")
            inbox_path = (
                step_dir
                / "mailboxes"
                / "agent-semantic"
                / "inbox.jsonl"
            )
            inbox_path.write_text(
                json.dumps(message, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            from agentteam_runtime.mailbox_worker import (
                MailboxDispatchIntegrityError,
            )

            with self.assertRaisesRegex(
                MailboxDispatchIntegrityError,
                "differs from scheduler authority",
            ):
                FileMailboxWorker(
                    agent_pool_path,
                    step_dir,
                    "agent-semantic",
                    runtime_adapter=FakeRuntimeAdapter(),
                    clock=FixedClock(),
                ).poll_once()


    def test_model_invocation_codex_worker_start_precedes_permit_and_terminal(self):
        observations = []

        class DeterministicGatedExecution:
            def __init__(
                self,
                lifecycle,
                command,
                *,
                cwd,
                input_text,
                timeout_seconds,
            ):
                self.lifecycle = lifecycle
                self.command = command

            def prepare(self):
                observations.append(("prepare", self.lifecycle.started_path.exists()))
                return _supported_execution_group_identity()

            def permit_and_wait(self, **kwargs):
                observations.append(("permit", self.lifecycle.started_path.exists()))
                result_path = Path(
                    self.command[
                        self.command.index("--output-last-message") + 1
                    ]
                )
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(
                    json.dumps(
                        {
                            "result_status": "completed",
                            "changed_files": [],
                            "output": {"adapter": "codex"},
                        }
                    ),
                    encoding="utf-8",
                )
                stdout = json.dumps(
                    {
                        "type": "turn_completed",
                        "usage": {
                            "input_tokens": 80,
                            "cached_input_tokens": 10,
                            "output_tokens": 20,
                            "reasoning_tokens": 3,
                            "total_tokens": 100,
                        },
                    }
                )
                return ProviderExecution(
                    list(self.command),
                    0,
                    stdout,
                    "",
                )

            def abort_before_permit(self):
                observations.append(("abort", self.lifecycle.started_path.exists()))

            def cleanup_after_terminal(self):
                observations.append(
                    ("cleanup", self.lifecycle.terminal_path.exists())
                )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            result = CodexRuntimeAdapter(
                command=["codex", "exec"],
                output_dir=output_dir,
                systemd_runner_factory=DeterministicGatedExecution,
            ).run(
                _model_invocation_message(),
                worktree_path=repo,
            )

            invocation = result["output"]["model_invocation"]
            start = json.loads(Path(invocation["started_path"]).read_text(encoding="utf-8"))
            terminal = json.loads(
                Path(invocation["terminal_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                observations,
                [
                    ("prepare", False),
                    ("permit", True),
                    ("cleanup", True),
                ],
            )
            self.assertEqual(result["result_status"], "completed")
            self.assertEqual(start["coverage_class"], "supported_model_invocation")
            self.assertEqual(terminal["usage_status"], "reported")
            self.assertEqual(terminal["total_tokens"], 100)
            self.assertEqual(
                start["lifecycle_owner_token"],
                terminal["lifecycle_owner_token"],
            )
            _validate_model_invocation_record(
                "model_invocation_started.schema.json",
                start,
            )
            _validate_model_invocation_record(
                "model_invocation_usage.schema.json",
                terminal,
            )


    def test_scheduler_collect_imports_every_worker_retry_lifecycle_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=[],
                tasks=[_backlog_task("TASK-001", write_scope=[])],
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
            for index in (1, 2):
                lifecycle = InvocationLifecycle(
                    output_dir,
                    _model_invocation_context(
                        coverage_class="not_applicable_adapter",
                        attempt_id=inflight["attempt_id"],
                        runtime_execution_session_id=inflight[
                            "runtime_session_id"
                        ],
                        lifecycle_owner_token=inflight["lease_id"],
                        lease_id=inflight["lease_id"],
                        agent_id=inflight["agent_id"],
                    ),
                    invocation_id=f"INV-worker-retry-{index:03d}",
                    started_at=f"2026-07-23T00:00:0{index}Z",
                )
                lifecycle.publish_start(
                    ExecutionGroupIdentity.not_applicable()
                )
                lifecycle.finalize(
                    "completed",
                    finished_at=f"2026-07-23T00:00:1{index}Z",
                )
            _append_test_jsonl(
                Path(inflight["outbox_path"]),
                [
                    {
                        "message_type": "runtime_result",
                        "payload": {
                            "source_message_id": inflight["message_id"],
                            "result_status": "completed",
                            "changed_files": [],
                            "output": {},
                        },
                    }
                ],
            )

            collected = scheduler.collect_ready_results()
            projection = replay_model_invocation_events(
                output_dir / "events.jsonl"
            )

            self.assertEqual(collected["collect_status"], "collected")
            self.assertEqual(projection["invocation_count"], 2)
            self.assertEqual(projection["terminal_count"], 2)
            lifecycle_events = [
                event
                for event in _read_jsonl_for_test(output_dir / "events.jsonl")
                if event["event_type"].startswith("model_invocation_")
            ]
            self.assertEqual(len(lifecycle_events), 4)
            scheduler.collect_ready_results()
            replay = replay_model_invocation_events(
                output_dir / "events.jsonl"
            )
            self.assertEqual(replay["invocation_count"], 2)


    def test_scheduler_imports_author_bootstrap_before_worker_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work_root = tmp_path / "work"
            output_dir = work_root / "runs" / "author-bootstrap-run"
            author_root = work_root / "drafts" / ".author-bootstrap"
            lifecycle = InvocationLifecycle(
                author_root,
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                    task_id=None,
                    attempt_id=None,
                    runtime_execution_session_id="AUTHOR-SESSION-001",
                    lifecycle_owner_token="AUTHOR-OWNER-001",
                    agent_id="taskpack-author",
                    agent_role="taskpack_author",
                    role="taskpack_author",
                    usage_stage="taskpack_author",
                ),
                invocation_id="INV-author-before-dispatch",
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            terminal = lifecycle.finalize(
                "completed",
                terminal_writer="taskpack_author",
                finished_at="2026-07-23T00:00:01Z",
            )
            state_dir = output_dir / "state"
            state_dir.mkdir(parents=True)
            started_relative = lifecycle.started_path.relative_to(work_root)
            terminal_relative = lifecycle.terminal_path.relative_to(work_root)
            (state_dir / "author_lifecycle_bootstrap.v1.json").write_text(
                json.dumps(
                    {
                        "bootstrap_schema_version": (
                            "author_lifecycle_bootstrap.v1"
                        ),
                        "author_context_path": str(
                            author_root.relative_to(work_root)
                        ),
                        "invocation_id": lifecycle.invocation_id,
                        "usage_event_id": terminal["usage_event_id"],
                        "started_path": str(started_relative),
                        "started_sha256": hashlib.sha256(
                            lifecycle.started_path.read_bytes()
                        ).hexdigest(),
                        "terminal_path": str(terminal_relative),
                        "terminal_sha256": hashlib.sha256(
                            lifecycle.terminal_path.read_bytes()
                        ).hexdigest(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            agent_pool_path = tmp_path / "agent_pool.json"
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=[],
                tasks=[_backlog_task("TASK-001", write_scope=[])],
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

            events = _read_jsonl_for_test(output_dir / "events.jsonl")
            start_event = next(
                event
                for event in events
                if event["event_type"] == "model_invocation_started"
            )
            usage_event = next(
                event
                for event in events
                if event["event_type"]
                == "model_invocation_usage_recorded"
            )
            dispatch_event = next(
                event
                for event in events
                if event["event_type"] == "message_dispatched"
            )
            self.assertLess(start_event["sequence"], dispatch_event["sequence"])
            self.assertLess(usage_event["sequence"], dispatch_event["sequence"])
            self.assertEqual(
                start_event["source_event_id"],
                lifecycle.invocation_id,
            )

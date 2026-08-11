try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class MailboxMixin:
    def test_file_mailbox_worker_poll_once_writes_runtime_result_to_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=FakeRuntimeAdapter(),
                clock=FixedClock(),
            )
            summary = worker.poll_once()

            result_message = _read_first_jsonl(outbox)

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["source_message_id"], "MSG-MAILBOX-001")
            self.assertEqual(result_message["message_type"], "runtime_result")
            self.assertEqual(
                result_message["payload"]["source_message_id"],
                "MSG-MAILBOX-001",
            )
            self.assertEqual(result_message["payload"]["result_status"], "completed")
            self.assertEqual(
                result_message["payload"]["changed_files"],
                ["generated/m0_generated_repo_index.json"],
            )


    def test_file_mailbox_worker_preserves_declared_runtime_artifact(self):
        class RuntimeArtifactAdapter:
            def run(self, message, worktree_path=None):
                artifact = ".agentteam/generated/repo-map.json"
                target = Path(worktree_path) / artifact
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}\n", encoding="utf-8")
                return {
                    "result_status": "completed",
                    "changed_files": [artifact],
                    "output": {"summary": "runtime artifact produced"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            inbox = (
                output_dir
                / "mailboxes"
                / "agent-repo-map"
                / "inbox.jsonl"
            )
            outbox = (
                output_dir
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            _init_git_repo(repo)
            message = _mailbox_dispatch_message(
                message_id="MSG-MAILBOX-ARTIFACT-001",
                agent_id="agent-repo-map",
                write_scope=[".agentteam/generated/"],
            )
            message["payload"]["expected_output_artifacts"] = [
                ".agentteam/generated/repo-map.json"
            ]
            _append_test_jsonl(inbox, [message])

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=RuntimeArtifactAdapter(),
                clock=FixedClock(),
            )
            summary = worker.poll_once(worktree_path=repo)
            payload = _read_first_jsonl(outbox)["payload"]

            self.assertEqual(
                summary["changed_files"],
                [".agentteam/generated/repo-map.json"],
            )
            self.assertEqual(
                payload["changed_files"],
                [".agentteam/generated/repo-map.json"],
            )


    def test_file_mailbox_worker_cli_processes_one_message_in_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-SUBPROCESS-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            _append_test_jsonl(inbox, [message])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agentteam_runtime.mailbox_worker",
                    "--agent-pool",
                    str(FIXTURES / "sample_agent_pool.json"),
                    "--output-dir",
                    str(output_dir),
                    "--agent-id",
                    "agent-repo-map",
                    "--message-id",
                    "MSG-SUBPROCESS-001",
                    "--runtime",
                    "fake",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result_message = _read_first_jsonl(outbox)

            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["source_message_id"], "MSG-SUBPROCESS-001")
            self.assertNotEqual(summary["worker_pid"], os.getpid())
            self.assertEqual(completed.stderr, "")
            self.assertEqual(result_message["message_type"], "runtime_result")


    def test_scheduler_loop_can_round_trip_runtime_result_through_mailbox_worker(self):
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
                runtime_adapter=FileMailboxRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    runtime_adapter=FakeRuntimeAdapter(),
                    clock=FixedClock(),
                ),
            )

            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())
            self.assertEqual(_read_first_jsonl(first_outbox)["message_type"], "runtime_result")
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxRuntimeAdapter"},
            )


    def test_scheduler_loop_can_run_mailbox_worker_as_one_shot_subprocess(self):
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
                runtime_adapter=FileMailboxSubprocessRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    timeout_seconds=30,
                ),
            )
            state = read_scheduler_state_index(output_dir)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxSubprocessRuntimeAdapter"},
            )


    def test_scheduler_salvages_mailbox_subprocess_timeout_with_tracked_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            worker = tmp_path / "hanging_mailbox_worker.py"
            _init_git_repo(repo)
            _write_hanging_mailbox_worker(worker, "README.md")
            backlog_path = _write_backlog(tmp_path, write_scope=["README.md"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FileMailboxSubprocessRuntimeAdapter(
                    FIXTURES / "sample_agent_pool.json",
                    command=[sys.executable, str(worker)],
                    timeout_seconds=1,
                ),
            )

            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            runtime_event = next(
                event for event in events if event["event_type"] == "runtime_output_received"
            )
            validation_event = next(
                event for event in events if event["event_type"] == "validation_rejected"
            )

            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(result["failure_category"], "timeout")
            self.assertEqual(result["integration_status"], "not_requested")
            self.assertEqual(result["integration_queue_status"], "not_queued")
            self.assertEqual(result["diff_audit"]["actual_changed_files"], ["README.md"])
            self.assertEqual(runtime_event["payload"]["changed_files"], ["README.md"])
            self.assertEqual(
                runtime_event["payload"]["output"]["salvage"]["salvage_status"],
                "patch_available",
            )
            self.assertEqual(
                runtime_event["payload"]["output"]["salvage"]["changed_files"],
                ["README.md"],
            )
            self.assertTrue(runtime_event["payload"]["output"]["salvage"]["review_required"])
            self.assertEqual(
                runtime_event["payload"]["output"]["salvage"]["integration_status"],
                "not_integrated",
            )
            self.assertEqual(validation_event["payload"]["patch_path"], result["patch_path"])
            self.assertTrue(Path(result["patch_path"]).exists())
            self.assertIn("README.md", Path(result["patch_path"]).read_text(encoding="utf-8"))


    def test_scheduler_loop_can_use_long_running_mailbox_worker_process(self):
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
            supervisor = FileMailboxWorkerProcessSupervisor(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                env=env,
                poll_interval_seconds=0.01,
            )

            start = supervisor.start()
            try:
                summary = run_scheduler_loop(
                    FIXTURES / "sample_agent_pool.json",
                    backlog_path,
                    output_dir,
                    clock=FixedClock(),
                    runtime_adapter=FileMailboxExternalRuntimeAdapter(
                        FIXTURES / "sample_agent_pool.json",
                        timeout_seconds=5,
                        poll_interval_seconds=0.01,
                    ),
                )
                self.assertIsNone(supervisor.process.poll())
            finally:
                stop = supervisor.stop()

            state = read_scheduler_state_index(output_dir)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )
            second_outbox = (
                output_dir
                / "steps"
                / "STEP-0002-TASK-002"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(start["worker_status"], "running")
            self.assertEqual(stop["worker_status"], "stopped")
            self.assertEqual(summary["scheduler_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertNotEqual(start["worker_pid"], os.getpid())
            self.assertTrue(first_outbox.exists())
            self.assertTrue(second_outbox.exists())
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxExternalRuntimeAdapter"},
            )


    def test_file_mailbox_worker_process_supervisor_reports_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            supervisor = FileMailboxWorkerProcessSupervisor(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
                env=env,
                poll_interval_seconds=0.01,
            )

            before = supervisor.health()
            start = supervisor.start()
            try:
                running = supervisor.health()
            finally:
                stop = supervisor.stop()
            stopped = supervisor.health()

            self.assertEqual(before["worker_status"], "not_started")
            self.assertEqual(running["worker_status"], "running")
            self.assertEqual(running["worker_pid"], start["worker_pid"])
            self.assertEqual(running["exit_code"], None)
            self.assertEqual(stop["worker_status"], "stopped")
            self.assertEqual(stopped["worker_status"], "exited")
            self.assertEqual(stopped["exit_code"], 0)


    def test_cli_can_run_file_daemon_with_mailbox_worker_adapter(self):
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
                    "--daemon-mailbox-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            first_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-repo-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertTrue(first_outbox.exists())


    def test_cli_can_run_file_daemon_with_mailbox_subprocess_worker(self):
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
                    "--daemon-mailbox-subprocess-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxSubprocessRuntimeAdapter"},
            )


    def test_cli_can_run_file_daemon_with_long_running_mailbox_worker(self):
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
                    "--daemon-long-running-mailbox-worker",
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            state = read_scheduler_state_index(output_dir)

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
            self.assertEqual(summary["worker_process"]["stderr"], "")
            self.assertEqual(
                {session["runtime_adapter"] for session in state["runtime_sessions"]},
                {"FileMailboxExternalRuntimeAdapter"},
            )


    def test_cli_can_run_file_daemon_with_long_running_codex_mailbox_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_long_worker.py"
            target_file = "generated/long_worker_codex_delegate.json"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_fake_codex(fake_codex, changed_file=target_file)
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
                    "--daemon-run-until-idle",
                    "--daemon-long-running-mailbox-worker",
                    "--runtime",
                    "codex",
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
            worktree_path = Path(
                summary["snapshot"]["attempts"]["TASK-001-ATTEMPT-001"]["worktree_path"]
            )

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_process"]["worker_status"], "stopped")
            self.assertEqual(summary["worker_process"]["worker_runtime"], "codex")
            self.assertEqual(summary["worker_process"]["stderr"], "")
            self.assertTrue((worktree_path / target_file).exists())
            self.assertEqual(
                summary["snapshot"]["runtime_sessions"]["SESSION-TASK-001-ATTEMPT-001"][
                    "runtime_adapter"
                ],
                "FileMailboxExternalRuntimeAdapter",
            )


    def test_cli_long_running_mailbox_worker_accepts_agent_id_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "custom_agent_pool.json"
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_id(agent_pool_path, "agent-custom-map")
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
                    "--daemon-long-running-mailbox-worker",
                    "--daemon-long-running-worker-agent-id",
                    "agent-custom-map",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            custom_outbox = (
                output_dir
                / "steps"
                / "STEP-0001-TASK-001"
                / "mailboxes"
                / "agent-custom-map"
                / "outbox.jsonl"
            )

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(summary["processed_task_ids"], ["TASK-001"])
            self.assertEqual(summary["worker_process"]["worker_agent_id"], "agent-custom-map")
            self.assertTrue(custom_outbox.exists())


    def test_file_mailbox_worker_pool_supervisor_starts_and_stops_all_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
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

            start = pool.start()
            try:
                self.assertEqual(start["pool_status"], "running")
                self.assertEqual(start["worker_count"], 2)
                self.assertEqual(
                    {worker["worker_agent_id"] for worker in start["workers"]},
                    {"agent-repo-map", "agent-doc-map"},
                )
                self.assertTrue(Path(start["process_registry_path"]).exists())
                self.assertTrue(
                    all(worker["worker_pid"] != os.getpid() for worker in start["workers"])
                )
            finally:
                stop = pool.stop()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(stop["pool_status"], "stopped")
            self.assertEqual(stop["worker_count"], 2)
            self.assertEqual(registry["registry_status"], "stopped")
            self.assertEqual(
                {worker["worker_agent_id"] for worker in stop["workers"]},
                {"agent-repo-map", "agent-doc-map"},
            )
            self.assertTrue(
                all(worker["worker_status"] == "stopped" for worker in stop["workers"])
            )


    def test_file_mailbox_worker_pool_health_classifies_exited_worker(self):
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
                command=[sys.executable, "-c", "import sys; sys.exit(0)"],
            )
            worker.start()
            try:
                worker.process.wait(timeout=5)
                pool.workers = [worker]

                health = pool.health_check()
            finally:
                worker.stop(timeout_seconds=1)

            self.assertEqual(health["pool_status"], "degraded")
            self.assertEqual(health["pool_diagnostic_status"], "attention")
            self.assertEqual(health["workers"][0]["worker_status"], "exited")
            self.assertEqual(health["workers"][0]["worker_diagnostic_state"], "exited")


    def test_file_mailbox_worker_pool_supervisor_uses_role_runtime_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            fake_codex = tmp_path / "fake_codex_worker_profile.py"
            _write_agent_pool_with_role_runtime_profiles(
                agent_pool_path,
                role_runtime_profiles={
                    "repo_map_agent": {
                        "adapter": "codex",
                        "command": [sys.executable, str(fake_codex)],
                        "model": "worker-role-profile-model",
                        "sandbox": "read-only",
                        "timeout_seconds": 45,
                        "resume_session_id": "SESSION-123",
                    }
                },
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "m0_runtime")
            pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )

            start = pool.start()
            try:
                self.assertEqual(start["pool_status"], "running")
                self.assertEqual(start["workers"][0]["worker_runtime"], "codex")
                self.assertEqual(
                    pool.workers[0].codex_command,
                    [sys.executable, str(fake_codex)],
                )
                self.assertEqual(pool.workers[0].codex_model, "worker-role-profile-model")
                self.assertEqual(pool.workers[0].codex_sandbox, "read-only")
                self.assertEqual(pool.workers[0].codex_timeout_seconds, 45)
                self.assertEqual(pool.workers[0].codex_resume_session_id, "SESSION-123")
                self.assertFalse(pool.workers[0].codex_resume_last)
            finally:
                stop = pool.stop()

            self.assertEqual(stop["workers"][0]["worker_runtime"], "codex")


    def test_file_mailbox_worker_pool_supervisor_resumes_running_workers_from_registry(self):
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

            start = pool.start()
            resumed_pool = FileMailboxWorkerPoolSupervisor(
                agent_pool_path,
                output_dir,
                env=env,
                poll_interval_seconds=0.01,
            )
            try:
                resumed = resumed_pool.resume_from_registry()
                health = resumed_pool.health_check()
                stop = resumed_pool.stop()
            finally:
                if pool.workers:
                    process = pool.workers[0].process
                    if process:
                        try:
                            if process.poll() is None:
                                pool.stop()
                        except ChildProcessError:
                            pass
                        for stream in (process.stdout, process.stderr):
                            if stream:
                                stream.close()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(resumed["pool_status"], "running")
            self.assertEqual(
                resumed["workers"][0]["worker_pid"],
                start["workers"][0]["worker_pid"],
            )
            self.assertEqual(health["workers"][0]["worker_status"], "running")
            self.assertEqual(stop["workers"][0]["worker_status"], "stopped")
            self.assertEqual(registry["registry_status"], "stopped")


    def test_file_mailbox_worker_pool_supervisor_restarts_exited_worker(self):
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

            start = pool.start()
            first_pid = start["workers"][0]["worker_pid"]
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            degraded = pool.health_check()
            restarted = pool.restart_exited_workers()
            try:
                recovered = pool.health_check()
            finally:
                stop = pool.stop()

            registry = json.loads(
                Path(stop["process_registry_path"]).read_text(encoding="utf-8")
            )

            self.assertEqual(degraded["pool_status"], "degraded")
            self.assertEqual(degraded["workers"][0]["worker_status"], "exited")
            self.assertEqual(restarted["restarted_count"], 1)
            self.assertEqual(restarted["workers"][0]["restart_status"], "restarted")
            self.assertNotEqual(
                restarted["workers"][0]["new_worker"]["worker_pid"],
                first_pid,
            )
            self.assertEqual(recovered["pool_status"], "running")
            self.assertEqual(recovered["workers"][0]["worker_status"], "running")
            self.assertEqual(registry["workers"][0]["restart_count"], 1)


    def test_file_mailbox_worker_pool_quarantines_after_restart_budget(self):
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
                max_restart_count=1,
            )

            pool.start()
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            first_restart = pool.restart_exited_workers()
            pool.workers[0].process.terminate()
            pool.workers[0].process.wait(timeout=5)
            quarantined = pool.restart_exited_workers()
            try:
                health = pool.health_check()
                registry = json.loads(
                    Path(health["process_registry_path"]).read_text(encoding="utf-8")
                )
            finally:
                pool.stop()

            self.assertEqual(first_restart["restarted_count"], 1)
            self.assertEqual(quarantined["restarted_count"], 0)
            self.assertEqual(quarantined["workers"][0]["restart_status"], "quarantined")
            self.assertEqual(health["workers"][0]["worker_status"], "quarantined")
            self.assertEqual(
                health["workers"][0]["quarantine_reason"],
                "restart_budget_exceeded",
            )
            self.assertEqual(registry["workers"][0]["worker_status"], "quarantined")
            self.assertEqual(registry["workers"][0]["restart_count"], 1)


    def test_mailbox_terminal_replay_preserves_invocation_and_usage_identity(self):
        class MustNotRunAdapter:
            def __init__(self):
                self.call_count = 0

            def run(self, message, worktree_path=None):
                self.call_count += 1
                raise AssertionError("terminal replay must not relaunch provider")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _write_agent_pool_with_agent_roles(
                agent_pool_path,
                [("agent-repo-map", "repo_map_agent")],
            )
            message = _mailbox_dispatch_message(
                "MSG-REPLAY-001",
                "agent-repo-map",
                [],
            )
            predecessor_snapshot = {
                "input_tokens": 17,
                "cached_input_tokens": 3,
                "output_tokens": 5,
                "reasoning_tokens": 2,
                "total_tokens": 22,
            }
            message["payload"].update(
                _model_invocation_context(
                    coverage_class="not_applicable_adapter",
                    attempt_id="ATTEMPT-MAILBOX-001",
                    agent_id="agent-repo-map",
                    agent_role="repo_map_agent",
                    role="repo_map_agent",
                    usage_stage="repo_map",
                    lifecycle_owner_token="LEASE-MAILBOX-001",
                    lease_id="LEASE-MAILBOX-001",
                    provider_predecessor_invocation_id="INV-before-replay",
                    provider_predecessor_turn_id="turn-before-replay",
                    provider_predecessor_usage_snapshot=predecessor_snapshot,
                )
            )
            message["payload"]["model_invocation_authority_root"] = str(output_dir)
            lifecycle = InvocationLifecycle(
                output_dir,
                message["payload"],
                invocation_id="INV-mailbox-replay-001",
                started_at="2026-07-23T00:00:00Z",
            )
            lifecycle.publish_start(ExecutionGroupIdentity.not_applicable())
            terminal = lifecycle.finalize(
                "completed",
                finished_at="2026-07-23T00:00:01Z",
            )
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            _append_test_jsonl(inbox, [message])
            adapter = MustNotRunAdapter()
            worker = FileMailboxWorker(
                agent_pool_path,
                output_dir,
                "agent-repo-map",
                runtime_adapter=adapter,
                clock=FixedClock(),
            )

            summary = worker.poll_once()

            result = _read_first_jsonl(worker.outbox_path)
            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(adapter.call_count, 0)
            self.assertEqual(
                result["payload"]["model_invocation"]["invocation_id"],
                lifecycle.invocation_id,
            )
            self.assertEqual(
                result["payload"]["model_invocation"]["usage_event_id"],
                terminal["usage_event_id"],
            )
            self.assertTrue(
                result["payload"]["model_invocation"]["replayed_from_terminal"]
            )
            self.assertEqual(
                json.dumps(
                    result["payload"]["model_invocation_context"][
                        "provider_predecessor_usage_snapshot"
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps(
                    predecessor_snapshot,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )

try:
    from .m0_runtime_test_support import *
except ImportError:
    from m0_runtime_test_support import *


class IntegrationMixin:
    def test_repo_map_rebuilds_cache_for_dirty_or_unversioned_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "runtime"
            _init_git_repo(repo)

            first = build_repository_map(repo, output_dir)
            (repo / "scratch.py").write_text("print('untracked')\n", encoding="utf-8")
            second = build_repository_map(repo, output_dir)

            files = {entry["path"]: entry for entry in second["inventory"]["files"]}
            warnings = [warning["warning"] for warning in second["manifest"]["warnings"]]
            self.assertEqual(first["manifest"]["cache_status"], "rebuilt")
            self.assertEqual(second["manifest"]["cache_status"], "rebuilt")
            self.assertEqual(second["manifest"]["working_tree_state"], "dirty_or_unversioned")
            self.assertIn("working_tree_dirty", warnings)
            self.assertNotIn("scratch.py", files)


    def test_file_mailbox_worker_uses_worktree_diff_for_changed_files(self):
        class StaleChangedFilesRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["historical/change.py"],
                    "output": {"summary": "existing implementation is complete"},
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
                message_id="MSG-MAILBOX-DIFF-001",
                agent_id="agent-repo-map",
                write_scope=["historical/"],
            )
            _append_test_jsonl(inbox, [message])

            worker = FileMailboxWorker(
                FIXTURES / "sample_agent_pool.json",
                output_dir,
                "agent-repo-map",
                runtime_adapter=StaleChangedFilesRuntimeAdapter(),
                clock=FixedClock(),
            )
            summary = worker.poll_once(worktree_path=repo)
            payload = _read_first_jsonl(outbox)["payload"]

            self.assertEqual(summary["changed_files"], [])
            self.assertEqual(payload["changed_files"], [])
            self.assertEqual(
                payload["output"]["changed_files_reconciliation"],
                {
                    "status": "reconciled_to_worktree",
                    "reported_changed_files": ["historical/change.py"],
                    "actual_changed_files": [],
                    "preserved_runtime_artifacts": [],
                },
            )


    def test_file_mailbox_worker_cli_can_use_codex_delegate_from_payload_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            fake_codex = tmp_path / "fake_codex_mailbox.py"
            target_file = "generated/mailbox_codex_delegate.json"
            _init_git_repo(repo)
            _write_fake_codex(fake_codex, changed_file=target_file)
            inbox = output_dir / "mailboxes" / "agent-repo-map" / "inbox.jsonl"
            outbox = output_dir / "mailboxes" / "agent-repo-map" / "outbox.jsonl"
            message = _mailbox_dispatch_message(
                message_id="MSG-CODEX-MAILBOX-001",
                agent_id="agent-repo-map",
                write_scope=["generated/"],
            )
            message["payload"]["worktree_path"] = str(repo)
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
                    "MSG-CODEX-MAILBOX-001",
                    "--runtime",
                    "codex",
                    "--codex-command-json",
                    json.dumps([sys.executable, str(fake_codex)]),
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

            self.assertEqual(completed.stderr, "")
            self.assertEqual(summary["poll_status"], "processed")
            self.assertEqual(summary["result_status"], "completed")
            self.assertTrue((repo / target_file).exists())
            self.assertEqual(result_message["payload"]["output"]["adapter"], "codex")


    def test_scheduler_loop_uses_task_scoped_worktree_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
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
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            branches = [step["result"]["branch"] for step in summary["steps"]]
            worktree_ids = [step["result"]["worktree_id"] for step in summary["steps"]]

            self.assertEqual(summary["processed_task_ids"], ["TASK-001", "TASK-002"])
            self.assertEqual(
                branches,
                ["agentteam/run/TASK-001-ATTEMPT-001", "agentteam/run/TASK-002-ATTEMPT-001"],
            )
            self.assertEqual(
                worktree_ids,
                ["WT-TASK-001-ATTEMPT-001", "WT-TASK-002-ATTEMPT-001"],
            )


    def test_scheduler_loop_uses_run_scoped_worktree_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir_a = tmp_path / "run-a"
            output_dir_b = tmp_path / "run-b"
            _init_git_repo(repo)
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-001", write_scope=["generated/task-001/"])],
            )

            first = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir_a,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )
            second = run_scheduler_loop(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir_b,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            self.assertEqual(first["scheduler_status"], "idle")
            self.assertEqual(second["scheduler_status"], "idle")
            self.assertEqual(first["steps"][0]["result"]["branch"], "agentteam/run-a/TASK-001-ATTEMPT-001")
            self.assertEqual(second["steps"][0]["result"]["branch"], "agentteam/run-b/TASK-001-ATTEMPT-001")


    def test_two_phase_scheduler_can_commit_verified_integration_patch(self):
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
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                ],
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
                    (
                        "import pathlib, subprocess; "
                        "path = 'generated/two_phase_commit.json'; "
                        "assert pathlib.Path(path).exists(); "
                        "subprocess.run("
                        "['git', 'ls-files', '--error-unmatch', '--', path], "
                        "check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)"
                    ),
                ],
                commit_verified_integration=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "two_phase_commit.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"attempt_id": inflight["attempt_id"]}),
                encoding="utf-8",
            )
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/two_phase_commit.json"],
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            integration_worktree = Path(result["integration_worktree_path"])
            queue = read_integration_queue(output_dir)
            queue_item = queue["items"][0]
            snapshot = replay_events(output_dir / "events.jsonl")
            snapshot_item = snapshot["integration_queue"][
                "TASK-001:TASK-001-ATTEMPT-001"
            ]

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["diff_audit"]["diff_status"], "matched")
            self.assertTrue(Path(result["patch_path"]).exists())
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertNotEqual(result["integration_commit_sha"], None)
            self.assertEqual(result["integration_queue_status"], "committed")
            self.assertEqual(queue_item["queue_status"], "committed")
            self.assertEqual(queue_item["integration_commit_sha"], result["integration_commit_sha"])
            self.assertEqual(snapshot_item["queue_status"], "committed")
            self.assertTrue(
                (integration_worktree / "generated" / "two_phase_commit.json").exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(
                snapshot["attempts"]["TASK-001-ATTEMPT-001"][
                    "integration_commit_status"
                ],
                "committed",
            )


    def test_two_phase_scheduler_blocks_l2_integration_when_evidence_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            task = _backlog_task("TASK-L2-001", write_scope=["generated/"])
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
                project_root=repo,
                integrate_accepted_patch=True,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree_path = Path(inflight["worktree_path"])
            target = worktree_path / "generated" / "l2_result.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"risk": "L2"}), encoding="utf-8")
            _append_runtime_result_with_output(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/l2_result.json"],
                {
                    "operator_summary": {
                        "what_changed": ["Implemented the L2 patch."],
                        "verification_summary": ["unit fixture: passed"],
                    },
                    "evidence_summary": {
                        "evidence_level": "L2",
                        "missing_evidence": ["review_result"],
                    },
                },
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            event_types = {event["event_type"] for event in events}

            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue(Path(result["patch_path"]).exists())
            self.assertEqual(result["evidence_status"], "incomplete")
            self.assertEqual(result["missing_evidence"], ["review_result"])
            self.assertEqual(result["integration_status"], "blocked")
            self.assertEqual(result["integration_block_reason"], "evidence_incomplete")
            self.assertEqual(result["integration_queue_status"], "not_queued")
            self.assertEqual(result["integration_verification_status"], "not_requested")
            self.assertIn("evidence_incomplete", event_types)
            self.assertIn("integration_blocked_by_evidence", event_types)
            self.assertNotIn("integration_queued", event_types)
            self.assertNotIn("patch_integrated", event_types)


    def test_two_phase_worker_attempts_start_from_updated_integration_baseline(self):
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
                    _backlog_task("TASK-001", write_scope=["generated/"]),
                    _backlog_task("TASK-002", write_scope=["generated/"]),
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
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert any(pathlib.Path('generated').glob('*.json'))",
                ],
            )

            scheduler.dispatch_ready()
            first = scheduler.state["inflight_attempts"][0]
            first_worktree = Path(first["worktree_path"])
            first_target = first_worktree / "generated" / "baseline_one.json"
            first_target.parent.mkdir(parents=True, exist_ok=True)
            first_target.write_text(json.dumps({"task": "one"}), encoding="utf-8")
            _append_runtime_result(
                first["outbox_path"],
                first["message_id"],
                first["task_id"],
                first["attempt_id"],
                first["lease_id"],
                "completed",
                ["generated/baseline_one.json"],
            )

            collected = scheduler.collect_ready_results()
            first_result = collected["results"][0]
            baseline_branch = "agentteam/run/run/integration"
            baseline_worktree = output_dir / "integration-baseline"
            baseline_commit = first_result["integration_baseline_commit_sha"]

            self.assertEqual(first["integration_base_sha"], source_head)
            self.assertEqual(first_result["integration_baseline_commit_status"], "committed")
            self.assertNotEqual(baseline_commit, source_head)
            self.assertEqual(first_result["integration_baseline_branch"], baseline_branch)
            self.assertEqual(
                first_result["integration_baseline_worktree_path"],
                str(baseline_worktree),
            )
            self.assertEqual(_git_rev_parse(repo, baseline_branch), baseline_commit)
            self.assertEqual(_git_rev_parse(baseline_worktree, "HEAD"), baseline_commit)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(repo), "")

            scheduler.dispatch_ready()
            second = scheduler.state["inflight_attempts"][0]
            second_worktree = Path(second["worktree_path"])

            self.assertEqual(second["integration_base_sha"], baseline_commit)
            self.assertEqual(_git_rev_parse(second_worktree, "HEAD"), baseline_commit)
            self.assertTrue((second_worktree / "generated" / "baseline_one.json").exists())


    def test_integration_baseline_worktree_can_start_from_initial_base_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "next-run"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            generated = repo / "generated" / "previous_round.json"
            generated.parent.mkdir(parents=True, exist_ok=True)
            generated.write_text(json.dumps({"round": 1}), encoding="utf-8")
            subprocess.run(
                ["git", "add", "generated/previous_round.json"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "previous integration baseline"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            previous_baseline_head = _git_rev_parse(repo, "HEAD")
            subprocess.run(
                ["git", "checkout", source_head],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = ensure_integration_baseline_worktree(
                repo,
                output_dir,
                base_ref=previous_baseline_head,
            )

            baseline_worktree = Path(result["integration_baseline_worktree_path"])
            self.assertEqual(
                result["integration_baseline_head_sha"],
                previous_baseline_head,
            )
            self.assertEqual(_git_rev_parse(baseline_worktree, "HEAD"), previous_baseline_head)
            self.assertTrue((baseline_worktree / "generated" / "previous_round.json").exists())
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)


    def test_versioned_run_uses_distinct_integration_and_attempt_refs(self):
        from agentteam_runtime.m0_runtime import _worktree_branch_name

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            runs_root = tmp_path / "runs"
            flat_run = runs_root / "same-taskpack"
            versioned_run = runs_root / "v3" / "same-taskpack"
            _init_git_repo(repo)

            flat = ensure_integration_baseline_worktree(repo, flat_run)
            versioned = ensure_integration_baseline_worktree(
                repo,
                versioned_run,
            )

            self.assertEqual(
                flat["integration_baseline_branch"],
                "agentteam/run/same-taskpack/integration",
            )
            self.assertEqual(
                versioned["integration_baseline_branch"],
                "agentteam/run/v3-same-taskpack/integration",
            )
            self.assertNotEqual(
                flat["integration_baseline_worktree_path"],
                versioned["integration_baseline_worktree_path"],
            )
            self.assertEqual(
                _worktree_branch_name(
                    flat_run / "steps" / "STEP-0001",
                    "TASK-001-ATTEMPT-001",
                ),
                "agentteam/same-taskpack/TASK-001-ATTEMPT-001",
            )
            self.assertEqual(
                _worktree_branch_name(
                    versioned_run / "steps" / "STEP-0001",
                    "TASK-001-ATTEMPT-001",
                ),
                "agentteam/v3-same-taskpack/TASK-001-ATTEMPT-001",
            )


    def test_two_phase_failed_integration_verification_does_not_advance_baseline(self):
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
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            sink = RecordingNotificationSink()
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
                max_inflight=1,
                integrate_accepted_patch=True,
                notification_sink=sink,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            worktree = Path(inflight["worktree_path"])
            target = worktree / "generated" / "failing.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"status": "will-fail"}), encoding="utf-8")
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/failing.json"],
            )

            collected = scheduler.collect_ready_results()
            result = collected["results"][0]
            baseline_worktree = Path(result["integration_baseline_worktree_path"])

            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(result["integration_baseline_commit_status"], "skipped")
            self.assertEqual(result["integration_baseline_commit_reason"], "verification_failed")
            self.assertEqual(_git_rev_parse(repo, "agentteam/run/run/integration"), source_head)
            self.assertEqual(_git_rev_parse(baseline_worktree, "HEAD"), source_head)
            self.assertFalse((baseline_worktree / "generated" / "failing.json").exists())
            self.assertEqual(_git_status_short(baseline_worktree), "")
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            events = [
                json.loads(line)
                for line in (output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            blocked = [event for event in events if event["event_type"] == "integration_blocked"]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]["payload"]["block_reason"], "verification_failed")
            self.assertEqual(blocked[0]["payload"]["task_id"], "TASK-001")
            self.assertEqual(blocked[0]["payload"]["attempt_id"], "TASK-001-ATTEMPT-001")
            self.assertEqual(
                [call["event"]["event_type"] for call in sink.calls],
                ["integration_blocked"],
            )


    def test_runtime_artifact_is_published_only_after_retry_integration_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            counter = tmp_path / "verification-count.txt"
            _init_git_repo(repo)
            (repo / ".git" / "info" / "exclude").write_text(
                ".agentteam/\n",
                encoding="utf-8",
            )
            artifact_path = ".agentteam/generated/repo_map_handoff.json"
            task = _backlog_task(
                "TASK-REPO-MAP",
                write_scope=["generated/"],
            )
            task["expected_output_artifacts"] = [artifact_path]
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[task],
            )
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
            verification_code = (
                "from pathlib import Path; import sys; "
                f"p=Path({str(counter)!r}); "
                "n=int(p.read_text()) if p.exists() else 0; "
                "p.write_text(str(n+1)); sys.exit(1 if n == 0 else 0)"
            )
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
                    verification_code,
                ],
                commit_verified_integration=True,
            )

            first_dispatch = scheduler.dispatch_ready()
            first = scheduler.state["inflight_attempts"][0]
            self._write_runtime_artifact_attempt(
                first,
                artifact_path,
                "same handoff",
            )
            first_result = scheduler.collect_ready_results()["results"][0]

            self.assertEqual(first_dispatch["dispatch_count"], 1)
            self.assertEqual(first_result["task_status"], "ready")
            self.assertFalse(
                (output_dir / "runtime_artifacts" / "manifest.json").exists()
            )

            scheduler.dispatch_ready()
            second = scheduler.state["inflight_attempts"][0]
            self._write_runtime_artifact_attempt(
                second,
                artifact_path,
                "same handoff",
            )
            second_result = scheduler.collect_ready_results()["results"][0]
            manifest = json.loads(
                (output_dir / "runtime_artifacts" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(second_result["task_status"], "done")
            self.assertEqual(
                manifest["artifacts"][artifact_path]["attempt_id"],
                second["attempt_id"],
            )


    def test_file_scheduler_does_not_publish_artifact_when_integration_fails(self):
        class ObservingFakeRuntime(FakeRuntimeAdapter):
            def __init__(self, artifact_path):
                self.artifact_path = artifact_path
                self.consumer_dispatched = False

            def run(self, message, worktree_path=None):
                if message["payload"]["task_id"] == "TASK-REPO-MAP":
                    artifact = Path(worktree_path) / self.artifact_path
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text('{"repo_map":"ready"}\n', encoding="utf-8")
                    changed = Path(worktree_path) / "generated" / "change.json"
                    changed.parent.mkdir(parents=True, exist_ok=True)
                    changed.write_text('{"code":"changed"}\n', encoding="utf-8")
                    return {
                        "result_status": "completed",
                        "changed_files": [
                            self.artifact_path,
                            "generated/change.json",
                        ],
                        "output": {"adapter": "artifact-plus-patch"},
                    }
                if message["payload"]["task_id"] == "TASK-IMPLEMENT":
                    self.consumer_dispatched = True
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
            runtime = ObservingFakeRuntime(artifact_path)
            scheduler = FileScheduler(
                agent_pool_path,
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=runtime,
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
            )

            result = scheduler.run_until_idle(max_steps=3)
            producer_result = result["steps"][0]["result"]
            producer_state = next(
                item
                for item in scheduler.state["backlog"]["items"]
                if item["task_id"] == "TASK-REPO-MAP"
            )

            self.assertEqual(result["scheduler_status"], "idle")
            self.assertEqual(producer_result["validation_status"], "rejected")
            self.assertEqual(
                producer_result["failure_category"],
                "artifact_integration_not_verified",
            )
            self.assertEqual(
                producer_result["integration_verification_status"],
                "failed",
            )
            self.assertEqual(producer_state["backlog_status"], "blocked")
            self.assertFalse(runtime.consumer_dispatched)
            self.assertFalse(
                (output_dir / "runtime_artifacts" / "manifest.json").exists()
            )


    def test_verified_dependency_dispatch_patch_requires_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
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
                integrate_accepted_patch=False,
            )

            scheduler.dispatch_ready()
            inflight = scheduler.state["inflight_attempts"][0]
            target = Path(inflight["worktree_path"]) / "generated" / "not-integrated.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"accepted": True}), encoding="utf-8")
            _append_runtime_result(
                inflight["outbox_path"],
                inflight["message_id"],
                inflight["task_id"],
                inflight["attempt_id"],
                inflight["lease_id"],
                "completed",
                ["generated/not-integrated.json"],
            )

            result = scheduler.collect_ready_results()["results"][0]
            dispatch = scheduler.dispatch_ready()
            status_by_id = {
                task["task_id"]: task["backlog_status"]
                for task in scheduler.state["backlog"]["items"]
            }

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["task_status"], "blocked")
            self.assertEqual(
                result["completion_policy"],
                "verified_integration_required",
            )
            self.assertEqual(
                result["failure_category"],
                "integration_not_requested",
            )
            self.assertEqual(status_by_id["TASK-SOURCE"], "blocked")
            self.assertEqual(status_by_id["TASK-DEPENDENT"], "ready")
            self.assertEqual(dispatch["dispatch_count"], 0)


    def test_cli_two_phase_worker_pool_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            agent_pool_path = tmp_path / "agent_pool.json"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_agent_pool_with_agent_ids(agent_pool_path, ["agent-repo-map"])
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
                    "--max-inflight",
                    "1",
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib; assert pathlib.Path('generated/m0_generated_repo_index.json').exists()",
                        ]
                    ),
                    "--commit-verified-integration",
                ],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            result = summary["steps"][0]["result"]
            integration_worktree = Path(result["integration_worktree_path"])

            self.assertEqual(summary["daemon_status"], "idle")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertTrue(
                (
                    integration_worktree
                    / "generated"
                    / "m0_generated_repo_index.json"
                ).exists()
            )
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)


    def test_cli_can_create_git_worktree_when_project_root_is_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
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
                    "--project-root",
                    str(repo),
                ],
                check=True,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            summary = json.loads(completed.stdout)
            self.assertEqual(summary["validation_status"], "accepted")
            self.assertTrue(Path(summary["worktree_path"]).exists())
            self.assertTrue((Path(summary["worktree_path"]) / "generated").is_dir())


    def test_cli_can_apply_accepted_patch_to_integration_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_integration_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_integration_result.json")
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
                    "--integrate-accepted-patch",
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
            integration_worktree = Path(summary["integration_worktree_path"])

            self.assertEqual(summary["integration_status"], "applied")
            self.assertTrue(
                (integration_worktree / "generated" / "cli_integration_result.json").exists()
            )


    def test_cli_can_commit_verified_integration_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "cli_commit_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/cli_commit_result.json")
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
                    "--integrate-accepted-patch",
                    "--integration-verification-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-c",
                            "import pathlib; assert pathlib.Path('generated/cli_commit_result.json').exists()",
                        ]
                    ),
                    "--commit-verified-integration",
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
            integration_worktree = Path(summary["integration_worktree_path"])

            self.assertEqual(summary["integration_verification_status"], "passed")
            self.assertEqual(summary["integration_commit_status"], "committed")
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(integration_worktree), "")


    def test_project_root_creates_real_git_worktree_for_writable_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertTrue(worktree_path.exists())
            completed = subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--is-inside-work-tree"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(completed.stdout.strip(), "true")
            self.assertTrue((worktree_path / "generated" / "m0_generated_repo_index.json").exists())

            snapshot = replay_events(output_dir / "events.jsonl")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["worktree_path"],
                str(worktree_path),
            )


    def test_expected_runtime_artifact_requires_worktree_verification(self):
        artifact_path = ".agentteam/generated/repo_map_handoff.json"
        task = {
            "write_scope": [".agentteam/generated/"],
            "expected_output_artifacts": [artifact_path],
        }
        result = {
            "result_status": "completed",
            "changed_files": [artifact_path],
            "output": {},
        }

        outcome = classify_attempt_outcome(result, task, diff_audit=None)

        self.assertEqual(outcome["validation_status"], "rejected")
        self.assertEqual(
            outcome["failure_category"],
            "artifact_verification_unavailable",
        )


    def test_worktree_diff_audit_detects_declared_file_missing_from_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)

            audit = audit_worktree_diff(repo, ["generated/missing.json"])

            self.assertEqual(audit["diff_status"], "mismatch")
            self.assertEqual(audit["declared_changed_files"], ["generated/missing.json"])
            self.assertEqual(audit["actual_changed_files"], [])
            self.assertEqual(audit["missing_declared_files"], ["generated/missing.json"])
            self.assertEqual(audit["undeclared_changed_files"], [])


    def test_worktree_diff_audit_matches_declared_file_in_git_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)
            target = repo / "generated" / "actual.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"created": True}), encoding="utf-8")

            audit = audit_worktree_diff(repo, ["generated/actual.json"])

            self.assertEqual(audit["diff_status"], "matched")
            self.assertEqual(audit["declared_changed_files"], ["generated/actual.json"])
            self.assertEqual(audit["actual_changed_files"], ["generated/actual.json"])
            self.assertEqual(audit["missing_declared_files"], [])
            self.assertEqual(audit["undeclared_changed_files"], [])


    def test_worktree_diff_audit_accepts_declared_runtime_artifact_ignored_by_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)
            (repo / ".git" / "info" / "exclude").write_text(
                ".agentteam/\n",
                encoding="utf-8",
            )
            artifact_path = ".agentteam/generated/repo_map_handoff.json"
            target = repo / artifact_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                '{"schema_version":"repo_map_handoff.v1"}\n',
                encoding="utf-8",
            )

            audit = audit_worktree_diff(
                repo,
                [artifact_path],
                runtime_artifact_paths=[artifact_path],
                runtime_artifact_baseline={artifact_path: None},
            )

            self.assertEqual(audit["diff_status"], "matched")
            self.assertEqual(audit["actual_changed_files"], [])
            self.assertEqual(
                audit["materialized_runtime_artifacts"],
                [artifact_path],
            )
            self.assertEqual(audit["missing_declared_files"], [])


    def test_worktree_diff_audit_rejects_unchanged_tracked_runtime_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)
            artifact_path = "tracked_handoff.json"
            target = repo / artifact_path
            target.write_text('{"existing":true}\n', encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", artifact_path],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "add tracked handoff"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            audit = audit_worktree_diff(
                repo,
                [artifact_path],
                runtime_artifact_paths=[artifact_path],
                runtime_artifact_baseline={
                    artifact_path: hashlib.sha256(target.read_bytes()).hexdigest()
                },
            )

            self.assertEqual(audit["diff_status"], "mismatch")
            self.assertEqual(audit["materialized_runtime_artifacts"], [])
            self.assertEqual(audit["missing_declared_files"], [artifact_path])


    def test_worktree_diff_audit_rejects_missing_required_runtime_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_git_repo(repo)
            artifact_path = ".agentteam/generated/repo_map_handoff.json"

            audit = audit_worktree_diff(
                repo,
                [],
                runtime_artifact_paths=[artifact_path],
                runtime_artifact_baseline={artifact_path: None},
                required_changed_files=[artifact_path],
            )

            self.assertEqual(audit["diff_status"], "mismatch")
            self.assertEqual(
                audit["missing_required_changed_files"],
                [artifact_path],
            )


    def test_shell_runtime_adapter_executes_command_in_worktree_and_parses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/shell_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            worktree_path = Path(result["worktree_path"])
            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue((worktree_path / "generated" / "shell_result.json").exists())


    def test_worktree_attempt_writes_patch_artifact_for_actual_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "patch_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/patch_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            patch_path = Path(result["patch_path"])

            self.assertTrue(patch_path.exists())
            self.assertEqual(result["attempts"][0]["patch_path"], str(patch_path))
            self.assertIn("generated/patch_result.json", patch_path.read_text(encoding="utf-8"))


    def test_accepted_patch_is_queued_without_auto_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "queued_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/queued_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            queue = read_integration_queue(output_dir)
            item = queue["items"][0]
            snapshot = replay_events(output_dir / "events.jsonl")
            snapshot_item = snapshot["integration_queue"]["TASK-001:ATTEMPT-001"]

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["integration_status"], "not_requested")
            self.assertEqual(result["integration_queue_status"], "pending")
            self.assertEqual(result["integration_queue_item_id"], "TASK-001:ATTEMPT-001")
            self.assertEqual(item["queue_status"], "pending")
            self.assertEqual(item["patch_path"], result["patch_path"])
            self.assertEqual(item["integration_status"], "not_requested")
            self.assertEqual(snapshot_item["queue_status"], "pending")
            self.assertEqual(snapshot_item["patch_path"], result["patch_path"])


    def test_integration_batch_verifies_two_queued_patches_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script_a = tmp_path / "queued_worker_a.py"
            script_b = tmp_path / "queued_worker_b.py"
            _init_git_repo(repo)
            _write_success_worker(script_a, "generated/a.json")
            _write_success_worker(script_b, "generated/b.json")

            backlog_a = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-A", write_scope=["generated/"])],
            )
            result_a = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_a,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script_a)]),
                attempt_id_prefix="A",
            )
            backlog_b = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-B", write_scope=["generated/"])],
            )
            result_b = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_b,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script_b)]),
                attempt_id_prefix="B",
            )

            batch = verify_integration_batch(
                repo,
                output_dir,
                "BATCH-001",
                [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib; "
                        "assert pathlib.Path('generated/a.json').exists(); "
                        "assert pathlib.Path('generated/b.json').exists()"
                    ),
                ],
            )
            registry = read_integration_batches(output_dir)
            batch_worktree = Path(batch["batch_worktree_path"])

            self.assertEqual(result_a["integration_queue_status"], "pending")
            self.assertEqual(result_b["integration_queue_status"], "pending")
            self.assertEqual(batch["batch_status"], "verified")
            self.assertEqual(batch["verification_status"], "passed")
            self.assertEqual(
                batch["queue_item_ids"],
                ["TASK-A:A-ATTEMPT-001", "TASK-B:B-ATTEMPT-001"],
            )
            self.assertTrue((batch_worktree / "generated" / "a.json").exists())
            self.assertTrue((batch_worktree / "generated" / "b.json").exists())
            self.assertEqual(registry["items"][0]["batch_status"], "verified")
            self.assertEqual(
                registry["items"][0]["applied_queue_item_ids"],
                ["TASK-A:A-ATTEMPT-001", "TASK-B:B-ATTEMPT-001"],
            )


    def test_verified_integration_batch_can_merge_back_to_source_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "merge_batch_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            _write_success_worker(script, "generated/merge_batch.json")
            backlog_path = _write_backlog(
                tmp_path,
                write_scope=["generated/"],
                tasks=[_backlog_task("TASK-MERGE", write_scope=["generated/"])],
            )
            run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
            )

            batch = verify_integration_batch(
                repo,
                output_dir,
                "BATCH-MERGE",
                [
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/merge_batch.json').exists()",
                ],
                merge_verified_batch=True,
            )
            registry = read_integration_batches(output_dir)

            self.assertEqual(batch["batch_status"], "verified")
            self.assertEqual(batch["merge_status"], "merged")
            self.assertNotEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertTrue((repo / "generated" / "merge_batch.json").exists())
            self.assertEqual(_git_status_short(repo), "")
            self.assertEqual(registry["items"][0]["merge_status"], "merged")
            self.assertEqual(registry["items"][0]["merge_commit_sha"], _git_rev_parse(repo, "HEAD"))


    def test_accepted_patch_applies_to_integration_worktree_without_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "integration_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_branch"], "agentteam/integration/TASK-001")
            self.assertTrue(
                (integration_worktree / "generated" / "integration_result.json").exists()
            )
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_status"],
                "applied",
            )


    def test_integration_worktree_apply_is_idempotent_after_interrupted_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            patch_path = tmp_path / "worktree.patch"
            _init_git_repo(repo)
            patch_path.write_text(
                "\n".join(
                    [
                        "diff --git a/generated/recovered.json b/generated/recovered.json",
                        "new file mode 100644",
                        "index 0000000..6132295",
                        "--- /dev/null",
                        "+++ b/generated/recovered.json",
                        "@@ -0,0 +1 @@",
                        '+{"status":"recovered"}',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            first = apply_patch_to_integration_worktree(
                repo,
                output_dir,
                "TASK-001",
                patch_path,
            )
            second = apply_patch_to_integration_worktree(
                repo,
                output_dir,
                "TASK-001",
                patch_path,
            )

            integration_worktree = Path(second["integration_worktree_path"])
            self.assertEqual(first["integration_status"], "applied")
            self.assertEqual(second["integration_status"], "applied")
            self.assertEqual(second["integration_recovery_status"], "reused_existing")
            self.assertTrue((integration_worktree / "generated" / "recovered.json").exists())
            self.assertEqual(
                _git_status_short(integration_worktree),
                "A generated/recovered.json",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(integration_worktree),
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    "generated/recovered.json",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )


    def test_integration_verification_command_passes_in_integration_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "verify_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/integration_result.json').exists()",
                ],
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_verification_exit_code"], 0)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_verification_status"],
                "passed",
            )


    def test_integration_verification_adds_native_runtime_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "repo"
            runtime_root = worktree / "experiments" / "native_agentteam_runtime" / "m0_runtime"
            package_root = runtime_root / "agentteam_runtime"
            package_root.mkdir(parents=True)
            (package_root / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {
                    "AGENTTEAM_LAUNCHER_SELECTION": (
                        '{"selection_version":"launcher_runtime_selection.v1"}'
                    )
                },
            ):
                result = run_integration_verification(
                    [
                        "python3",
                        "-c",
                        (
                            "import agentteam_runtime, os, sys; "
                            "expected = sys.argv[1]; "
                            "assert os.environ.get('PYTHONPATH', '').split(os.pathsep)[0] == expected; "
                            "assert 'AGENTTEAM_LAUNCHER_SELECTION' not in os.environ; "
                            "assert agentteam_runtime.VALUE == 7"
                        ),
                        str(runtime_root),
                    ],
                    worktree,
                )

            self.assertEqual(result["integration_verification_status"], "passed")
            self.assertEqual(result["integration_verification_exit_code"], 0)


    def test_integration_verification_addition_missing_executable_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "repo"
            worktree.mkdir()

            result = run_integration_verification_additions(
                [
                    {
                        "label": "missing-python",
                        "command": [
                            "python3.999",
                            "-c",
                            "print('never runs')",
                        ],
                        "reason": "exercise missing executable handling",
                    }
                ],
                worktree,
            )

            self.assertEqual(result["integration_verification_additions_status"], "failed")
            self.assertEqual(
                result["integration_verification_additions"][0][
                    "verification_addition_status"
                ],
                "failed",
            )
            self.assertIn(
                "python3.999",
                result["integration_verification_additions"][0][
                    "verification_addition_stderr"
                ],
            )


    def test_integration_verification_command_failure_is_recorded_without_rejecting_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "verify_fail_worker.py"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/integration_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertEqual(result["integration_status"], "applied")
            self.assertEqual(result["integration_verification_status"], "failed")
            self.assertEqual(result["integration_verification_exit_code"], 7)
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_verification_status"],
                "failed",
            )


    def test_verified_integration_patch_can_be_committed_to_integration_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('generated/commit_result.json').exists()",
                ],
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_commit_status"], "committed")
            self.assertIsNotNone(result["integration_commit_sha"])
            self.assertEqual(result["integration_commit_reason"], None)
            self.assertNotEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertEqual(_git_rev_parse(repo, "HEAD"), source_head)
            self.assertEqual(_git_status_short(integration_worktree), "")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_commit_status"],
                "committed",
            )


    def test_integration_commit_is_skipped_when_verification_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_skip_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_skip_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                integration_verification_command=[
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(7)",
                ],
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])
            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(result["integration_commit_reason"], "verification_failed")
            self.assertEqual(result["integration_commit_sha"], None)
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)
            self.assertNotEqual(_git_status_short(integration_worktree), "")
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["integration_commit_status"],
                "skipped",
            )


    def test_integration_commit_is_skipped_without_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            script = tmp_path / "commit_no_verify_worker.py"
            _init_git_repo(repo)
            source_head = _git_rev_parse(repo, "HEAD")
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])
            _write_success_worker(script, "generated/commit_no_verify_result.json")

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=ShellRuntimeAdapter([sys.executable, str(script)]),
                integrate_accepted_patch=True,
                commit_verified_integration=True,
            )

            integration_worktree = Path(result["integration_worktree_path"])

            self.assertEqual(result["integration_commit_status"], "skipped")
            self.assertEqual(result["integration_commit_reason"], "verification_not_requested")
            self.assertEqual(result["integration_commit_sha"], None)
            self.assertEqual(_git_rev_parse(integration_worktree, "HEAD"), source_head)


    def test_declared_changed_file_without_worktree_diff_is_rejected(self):
        class PhantomRuntimeAdapter:
            def run(self, message, worktree_path=None):
                return {
                    "result_status": "completed",
                    "changed_files": ["generated/phantom.json"],
                    "output": {"adapter": "phantom"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=PhantomRuntimeAdapter(),
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "rejected")
            self.assertEqual(result["failure_category"], "diff_mismatch")
            self.assertEqual(
                result["diff_audit"]["missing_declared_files"],
                ["generated/phantom.json"],
            )
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["failure_category"],
                "diff_mismatch",
            )


    def test_accepted_attempt_can_remove_git_worktree_when_cleanup_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            output_dir = tmp_path / "run"
            _init_git_repo(repo)
            backlog_path = _write_backlog(tmp_path, write_scope=["generated/"])

            result = run_simulation(
                FIXTURES / "sample_agent_pool.json",
                backlog_path,
                output_dir,
                clock=FixedClock(),
                project_root=repo,
                runtime_adapter=FakeRuntimeAdapter(),
                cleanup_accepted_worktrees=True,
            )

            snapshot = replay_events(output_dir / "events.jsonl")

            self.assertEqual(result["validation_status"], "accepted")
            self.assertTrue(result["worktree_removed"])
            self.assertFalse(Path(result["worktree_path"]).exists())
            self.assertEqual(
                snapshot["attempts"]["ATTEMPT-001"]["worktree_status"],
                "removed",
            )


    def test_codex_runtime_adapter_runs_planner_with_fallback_worktree_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_planner.py"
            _init_git_repo(repo)
            _write_fake_codex_planner(fake_codex)

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                fallback_worktree_path=repo,
            ).run(_planner_message(tmp_path), worktree_path=None)

            self.assertEqual(result["result_status"], "completed")
            self.assertEqual(result["changed_files"], [])
            self.assertEqual(
                result["output"]["task_proposal"]["tasks"][0]["task_id"],
                "TASK-M23-CODEX-001",
            )
            self.assertEqual(_git_status_short(repo), "")


    def test_codex_runtime_adapter_rejects_dirty_fallback_worktree_after_planner_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            fake_codex = tmp_path / "fake_codex_dirty_planner.py"
            _init_git_repo(repo)
            _write_fake_codex_planner(fake_codex, dirty_file="generated/dirty.json")

            result = CodexRuntimeAdapter(
                command=[sys.executable, str(fake_codex)],
                fallback_worktree_path=repo,
            ).run(_planner_message(tmp_path), worktree_path=None)

            self.assertEqual(result["result_status"], "failed")
            self.assertEqual(
                result["output"]["error"],
                "fallback_worktree_modified",
            )
            self.assertIn("generated/dirty.json", result["output"]["changed_files"])
